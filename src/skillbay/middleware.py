"""SkillMiddleware: LangChain create_agent 可插拔技能中间件 / pluggable skill middleware.

面向后端业务服务的技能系统，核心设计：
Backend-oriented skill system, core design:

- 权限策略只有 allow/deny 两态，无需人工审批（后端没有可交互的人）。
  Permission policy is two-state (allow/deny); no human approval needed
  (there is no human to confirm in a backend service).
- 技能是部署产物：构造时一次性加载并锁定，不做运行时动态发现。
  Skills are deploy artifacts: loaded once at construction, then frozen —
  no runtime discovery.
- 爆炸半径受工具闸门约束：激活中的技能其 allowed-tools 在每次工具调用
  时强制执行（见 check_allowed_tools）。
  Blast radius is bounded by a tool gate: a skill's allowed-tools are
  enforced on every tool call while that skill is active (see
  check_allowed_tools).

技能记账（announced_skills / skill_invocations）放在 AgentState 中而非
进程级全局变量，以便随 checkpointer 持久化、按 thread 隔离、resume 后
不重复播报。
Skill accounting (announced_skills / skill_invocations) lives in AgentState
instead of process globals, so it persists with the checkpointer, stays
isolated per thread, and is not re-announced after a resume.

三层渐进式披露：
Three-layer progressive disclosure:
    1. 发现  - before_model 将预算化的清单注入为 user 消息。
       Discover  - before_model injects a budgeted listing as a user message.
    2. 调用  - 模型调用 skill 工具；正文展开后作为 ToolMessage 返回。
       Invoke    - the model calls the `skill` tool; the body is expanded and
                   returned as a ToolMessage.
    3. 引用  - 正文中的相对路径通过 "Base directory" 头解析，由 agent 自己的
               文件工具按需读取（本中间件不提供文件工具）。
       Reference - relative paths in the body resolve via the "Base directory"
                   header and the agent's own file tools (none provided here).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolCallRequest
from pydantic import BaseModel, Field

from .core import Skill, format_skills_within_budget, load_skills
from .expansion import execute_shell_blocks, substitute_arguments

logger = logging.getLogger(__name__)
PACKAGE_LOGGER = logging.getLogger("skillbay")


def _install_verbose_handler() -> None:
    """verbose=True 时将 INFO 级别日志输出到 stderr（幂等，不会重复安装）。
    Emit package INFO records to stderr when verbose=True. Idempotent: never
    installs a second handler."""
    if not any(isinstance(h, logging.StreamHandler) for h in PACKAGE_LOGGER.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[skillbay] %(message)s"))
        PACKAGE_LOGGER.addHandler(handler)
    PACKAGE_LOGGER.setLevel(logging.INFO)


# 权限策略接口：(skill, args) -> "allow" | "deny"。
# 后端服务在部署时确定权限，契约只有两个值。
# Permission policy seam: (skill, args) -> "allow" | "deny".
# Backend services resolve permissions at deploy time; the contract has
# exactly two values.
PermissionPolicy = Callable[[Skill, str | None], str]

# 审计事件类型（技能系统中安全相关的节点）。
# Audit event types (the security-relevant moments of the skill system).
AUDIT_INVOKED = "skill_invoked"  # 技能展开成功 / skill expanded successfully
AUDIT_DENIED = "skill_denied"  # 被权限策略拒绝 / rejected by the permission policy
AUDIT_REINJECTED = "skill_reinjected"  # 摘要压缩后重注入 / body re-injected after summarization
AUDIT_ANNOUNCED = (
    "skills_announced"  # 清单播报（首轮或增量）/ listing announced (first round or delta)
)
AUDIT_TOOL_BLOCKED = (
    "tool_call_blocked"  # 被 allowed-tools 闸门拦截 / blocked by the allowed-tools gate
)
AUDIT_DISMISSED = "skill_dismissed"  # 技能退场 / skill dismissed by model


@dataclass
class AuditEvent:
    """一条安全相关的技能系统事件，交给审计回调处理。
    One security-relevant skill-system event, handed to the audit callback.

    通过 SkillMiddleware(audit=...) 接入；回调自行决定记录日志、
    发送指标还是告警。没有回调时仅保留 logging。
    Wire it via SkillMiddleware(audit=...); the callback decides whether to
    log, emit metrics, or raise alerts. Without a callback only logging
    remains.
    """

    event: str  # AUDIT_* 常量之一 / one of the AUDIT_* constants
    skill: str | None = (
        None  # 涉及的技能名（清单播报时为 None）/ involved skill name (None for listing announcements)
    )
    args: str | None = None  # 调用参数 / invocation arguments
    tool_call_id: str | None = None  # 关联的 tool_call id / related tool_call id
    detail: str = (
        ""  # 额外上下文（被拦截的工具名/原因）/ extra context (blocked tool name / reason)
    )
    timestamp: float = field(default_factory=time.time)


class SkillState(AgentState):
    """扩展的 agent 状态：技能记账字段（随 checkpointer 持久化）。
    Extended agent state: skill accounting fields (persisted by the checkpointer)."""

    announced_skills: NotRequired[list[str]]
    """已在清单中播报过的技能名（增量播报账本）。
    Skill names already announced in the listing (delta-announcement ledger)."""

    skill_invocations: NotRequired[dict[str, dict[str, str]]]
    """已记录的调用：tool_call_id -> {"skill": name, "args": args}。
    摘要压缩存活机制的基础：若记录存在但其 tool_call_id 不再出现在消息列表中，
    说明该轮被摘要压缩掉了，需要重新展开并注入正文。
    Recorded invocations: tool_call_id -> {"skill": name, "args": args}.
    Basis of the summarization-survival mechanism: if a record exists but its
    tool_call_id is gone from the message list, that turn was summarized away
    and the body must be re-injected."""

    active_skills: NotRequired[dict[str, bool]]
    """当前激活的技能：skill_name -> True。
    模型调用 skill_dismiss 退场技能后，该技能不再重注入。
    Currently active skills: skill_name -> True.
    After the model calls skill_dismiss to dismiss a skill, it is no longer
    re-injected."""


def wrap_in_system_reminder(content: str) -> SystemMessage:
    """将内容包装为系统消息 / Wrap content as a SystemMessage.

    技能清单是系统提供的上下文，不是用户输入。使用 SystemMessage 而非
    HumanMessage 可以：
    1. 语义准确：明确标识这是系统指令
    2. 角色清晰：模型知道这是系统提供的能力描述
    3. 标准兼容：符合 LLM API 的消息角色约定

    The skill listing is system-provided context, not user input. Using
    SystemMessage instead of HumanMessage:
    1. Semantic accuracy: clearly identifies this as system instructions
    2. Role clarity: model knows this is system-provided capability description
    3. Standard compliance: aligns with LLM API message role conventions
    """
    return SystemMessage(content=content)


def expand_skill(
    skill: Skill,
    args: str | None,
    *,
    session_id: str,
    enable_shell_blocks: bool = False,
) -> str:
    """将 SKILL.md 展开为最终指令，步骤顺序固定不可调换。
    Turn a SKILL.md into final instructions (step order is fixed):

    base-directory header -> argument substitution -> ${SKILL_DIR} ->
    ${SESSION_ID} -> optional shell blocks.
    """
    final_content = (
        f"Base directory for this skill: {skill.base_dir}\n\n{skill.content}"
        if skill.base_dir
        else skill.content
    )
    final_content = substitute_arguments(final_content, args, argument_names=skill.argument_names)
    if skill.base_dir:
        # Windows 路径中的反斜杠会吞掉后续字符，统一替换为正斜杠。
        # Backslashes in Windows paths swallow following characters; normalize.
        normalized = skill.base_dir.replace("\\", "/")
        final_content = final_content.replace("${SKILL_DIR}", normalized)
    final_content = final_content.replace("${SESSION_ID}", session_id)
    if enable_shell_blocks:
        final_content = execute_shell_blocks(final_content, shell=skill.shell)
    return final_content


SKILL_TOOL_DESCRIPTION = """Execute a skill within the main conversation

When users ask you to perform tasks, check if any of the available skills match. Skills provide specialized capabilities and domain knowledge.

When users reference a "slash command" or "/<something>", they are referring to a skill. Use this tool to invoke it.

How to invoke:
- Use this tool with the skill name and optional arguments
- Examples:
  - skill: "greeting" - invoke the greeting skill
  - skill: "echo", args: "hello world" - invoke with arguments

Important:
- Available skills are listed in system-reminder messages in the conversation
- When a skill matches the user's request, this is a BLOCKING REQUIREMENT: invoke the relevant skill tool BEFORE generating any other response about the task
- NEVER mention a skill without actually calling this tool
- Do not invoke a skill that is already running
- If the skill's instructions have already appeared in the conversation, follow them directly instead of calling this tool again
"""


def _default_permission_policy(skill: Skill, args: str | None) -> str:
    """默认策略：全部允许——契合「技能是经 review 的部署产物」的后端场景。
    传入自定义策略可收紧权限。
    Default policy: allow everything — fits the "skills are reviewed deploy
    artifacts" backend scenario. Pass a custom policy to tighten it."""
    return "allow"


# ---------------------------------------------------------------------------
# allowed-tools 闸门（纯函数，无需中间件实例，可独立单测）
# allowed-tools gate (pure function, no middleware instance, unit-testable)
# ---------------------------------------------------------------------------

_LAUNCH_MARKER = "Launching skill:"


def check_allowed_tools(
    messages: list[Any],
    tool_name: str,
    skills_by_name: dict[str, Skill],
) -> str | None:
    """若此工具调用违反当前激活的 allowed-tools 窗口，返回拦截原因。
    Return a block reason if this tool call violates an active allowed-tools window.

    窗口完全从消息历史推导（无额外状态）：
    The window is derived from the message history alone (no extra state):
    - 技能调用成功时打开（ToolMessage 以 "Launching skill:" 开头）。
      Opens when a skill call succeeds (its ToolMessage starts with
      "Launching skill:").
    - 遇到下一条真正的 user 消息时关闭（system-reminder 注入不算）。
      Closes at the next real user message (system-reminder injections do
      not close it).
    - 窗口内，所有已成功调用且声明了 allowed-tools 的技能取并集构成
      可用工具集；未声明 allowed-tools 的技能不会放宽已有限制（只紧不松）。
      While open, the union of allowed-tools of all successfully invoked
      skills that declare one forms the usable tool set; skills without
      allowed-tools never loosen an existing restriction (restrictions only
      tighten).

    为什么必须是成功的配对：权限策略拒绝技能调用时不会产生
    "Launching skill:" ToolMessage，因此被拒绝的技能永远不会激活其
    白名单——拒绝不能成为提权通道。
    Why a successful pairing matters: when the policy denies a skill call
    there is no "Launching skill:" ToolMessage, so a denied skill never
    activates its whitelist — denial must not become a privilege-escalation
    path.

    返回 None 表示放行，返回原因字符串则交给模型。
    Returns None to allow, or a reason string to be returned to the model.
    """
    active_allowed: set[str] | None = None  # None = 不受限 / unrestricted
    restricting: set[str] = set()  # 当前生效的技能白名单 / which skills' allowed-tools are in force
    pending: dict[
        str, str
    ] = {}  # tool_call_id -> 技能名（等待 ToolMessage）/ tool_call_id -> skill name (awaiting ToolMessage)

    for m in messages:
        if isinstance(m, HumanMessage):
            # 真正的用户消息：重置窗口
            # Real user message: reset the window
            active_allowed = None
            restricting = set()
            pending = {}
        elif isinstance(m, SystemMessage):
            # 系统消息（技能清单注入）：不算新任务，窗口保持打开
            # System message (skill listing injection): not a new task, keep window open
            pass
        elif isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                if tc.get("name") == "skill":
                    pending[tc["id"]] = str(tc.get("args", {}).get("skill", ""))
        elif isinstance(m, ToolMessage) and m.tool_call_id in pending:
            name = pending.pop(m.tool_call_id)
            # 只有成功的展开才激活白名单（拒绝不算）。
            # Only a successful expansion activates the whitelist (denials do not).
            if str(m.content).startswith(_LAUNCH_MARKER):
                skill = skills_by_name.get(name)
                if skill is not None and skill.allowed_tools:
                    if active_allowed is None:
                        active_allowed = set()
                    active_allowed.update(skill.allowed_tools)
                    restricting.add(name)

    if active_allowed is None:
        return None
    if tool_name in active_allowed:
        return None
    # 窗口内连 skill 工具本身也拦截，防止受限技能串调到非受限技能。
    # The skill tool itself is blocked inside the window as well, so a
    # restricted skill cannot chain into an unrestricted one mid-turn.
    return (
        f"Blocked by skill allowed-tools: skills [{', '.join(sorted(restricting))}] "
        f"restricted this turn to [{', '.join(sorted(active_allowed))}], "
        f"but '{tool_name}' is not in the list. "
        "Finish the task with the allowed tools only."
    )


class SkillMiddleware(AgentMiddleware):
    """可插拔技能中间件 / Pluggable skill middleware:
    create_agent(model, middleware=[SkillMiddleware(...)]).

    Args:
        skills_dirs: 技能目录列表，靠后的目录在同名冲突时优先。集合在构造时
            一次性加载并锁定——部署时决定，运行时不变。
            Skill directories; later entries take priority on name conflicts.
            The set is loaded once at construction and frozen — decided at
            deploy time, unchanged at runtime.
        context_window_tokens: 模型上下文窗口大小，用于计算 1% 的清单预算；
            默认回退到 8000 字符。
            Model context window size, used for the 1% listing budget;
            defaults to an 8000-char fallback.
        permission_policy: 权限策略接口，(skill, args) -> "allow" | "deny"。
            默认全部允许（技能经代码 review 后才上线）。非 "allow" 的返回值
            一律视为拒绝并记录审计。
            Policy seam, (skill, args) -> "allow" | "deny". Defaults to
            allow-all (skills ship through code review). Any non-"allow"
            return value is treated as "deny" and audited.
        audit: 审计回调，(AuditEvent) -> None。调用、拒绝、工具拦截、
            重注入和播报均会触发事件。
            Audit callback, (AuditEvent) -> None. Invocations, denials,
            tool blocks, re-injections and announcements all emit events.
        enable_shell_blocks: 是否执行技能正文中的 !`...` 块。默认 False——
            这是安全开关，不是偏好设置。
            Whether to execute !`...` blocks in skill bodies. Default
            False — a safety switch, not a preference.
        verbose: 将关键事件（清单注入、调用、重注入）输出到 stderr 供观察。
            Emit key events (listing injection, invocations, re-injections)
            to stderr for observation.
    """

    state_schema = SkillState

    def __init__(
        self,
        skills_dirs: list[str],
        *,
        context_window_tokens: int | None = None,
        permission_policy: PermissionPolicy | None = None,
        audit: Callable[[AuditEvent], None] | None = None,
        enable_shell_blocks: bool = False,
        verbose: bool = False,
    ) -> None:
        self.skills = load_skills(skills_dirs)
        self.context_window_tokens = context_window_tokens
        self.permission_policy = permission_policy or _default_permission_policy
        self.audit = audit
        self.enable_shell_blocks = enable_shell_blocks
        self.verbose = verbose
        self.session_id = str(uuid.uuid4())
        if verbose:
            _install_verbose_handler()
        # The skill tools this middleware registers (closure over self).
        self.tools = [self._make_skill_tool(), self._make_skill_dismiss_tool()]

    def _audit(self, event: str, **kwargs: Any) -> None:
        """发出审计事件；没有回调时为空操作。审计失败绝不能影响主流程。
        Emit an audit event; without a callback this is a no-op. Audit
        failures must never take down the main flow."""
        if self.audit is not None:
            try:
                self.audit(AuditEvent(event=event, **kwargs))
            except Exception as e:  # noqa: BLE001 - callback errors are not business errors
                logger.warning("Audit callback raised: %s", e)

    # ------------------------------------------------------------------
    # 第二层：skill 工具 / Layer 2: the skill tool
    # ------------------------------------------------------------------

    def _make_skill_tool(self):
        """构建 skill 工具的闭包，使其可访问实例状态（技能表、权限策略、会话 ID）。
        Build the `skill` tool as a closure so it can reach instance state
        (skill table, permission policy, session id).

        输入 schema 通过 args_schema 显式声明，而非依赖函数签名——因为
        与 BaseTool 内部字段同名的参数会被 langchain-core 自动重命名，
        args_schema 可确保模型侧看到的字段名始终是 skill / args。
        The input schema is declared explicitly via args_schema instead of
        the function signature: parameter names that clash with BaseTool
        internals get renamed by langchain-core, and args_schema keeps the
        model-facing field names as skill / args.
        """
        mw = self

        class SkillInput(BaseModel):
            """skill 工具的输入 schema / Input schema for the skill tool."""

            skill: str = Field(description='The skill name. E.g. "greeting", "echo".')
            args: str | None = Field(
                default=None,
                description="Optional arguments for the skill, e.g. the user's name.",
            )

        @tool(SKILL_TOOL_DESCRIPTION, args_schema=SkillInput)
        def skill(**kwargs) -> str:
            """Invoke a skill by name with optional arguments."""

            name = (kwargs.get("skill") or "").strip()
            raw_args = kwargs.get("args")
            # 兼容斜杠调用（"/greeting" -> "greeting"）
            # Tolerate a slash invocation ("/greeting" -> "greeting")
            name = name[1:] if name.startswith("/") else name
            found = next((s for s in mw.skills if s.name == name), None)

            if not name:
                return f"Invalid skill format: {name!r}"
            if found is None:
                known = ", ".join(s.name for s in mw.skills) or "(none)"
                return f"Unknown skill: {name}. Available: {known}"
            if found.disable_model_invocation:
                return f"Skill {name} has disable-model-invocation: it can only be invoked by the user."

            # -- 权限检查：两态策略（allow/deny）/ permission check: two-state policy --
            decision = mw.permission_policy(found, raw_args)
            if decision != "allow":
                mw._audit(
                    AUDIT_DENIED,
                    skill=name,
                    args=raw_args,
                    detail=f"permission policy returned {decision!r}",
                )
                return f"Skill {name} execution blocked by permission policy."

            # -- 展开正文并作为 ToolMessage 返回 / expand the body and return it as a ToolMessage --
            content = expand_skill(
                found,
                raw_args,
                session_id=mw.session_id,
                enable_shell_blocks=mw.enable_shell_blocks,
            )
            mw._audit(AUDIT_INVOKED, skill=name, args=raw_args)
            logger.info("Skill %r expanded (%d chars)", name, len(content))
            return (
                f"Launching skill: {name}\n\n"
                "The following is the skill's full instructions. Follow them now:\n\n"
                f"{content}"
            )

        # 规范工具名：模型在 tool_calls 中使用此名称。
        # Canonical tool name: models write exactly this in tool_calls.
        skill.name = "skill"
        return skill

    def _make_skill_dismiss_tool(self):
        """构建 skill_dismiss 工具：模型调用以退场不再需要的技能。
        Build the `skill_dismiss` tool: model calls it to dismiss a skill
        that is no longer needed.

        退场后，该技能的全文不再被重注入到后续对话中。
        After dismissal, the skill's full body is no longer re-injected
        into subsequent turns.
        """
        mw = self

        class SkillDismissInput(BaseModel):
            """skill_dismiss 工具的输入 schema / Input schema for the skill_dismiss tool."""

            skill: str = Field(description='The skill name to dismiss. E.g. "greeting", "echo".')
            reason: str | None = Field(
                default=None,
                description="Optional reason for dismissal, for audit purposes.",
            )

        @tool(
            "Dismiss a skill that is no longer needed. "
            "Use this when the conversation has moved away from the skill's scope. "
            "After dismissal, the skill's full instructions will no longer be injected into the context.",
            args_schema=SkillDismissInput,
        )
        def skill_dismiss(**kwargs) -> str:
            """Dismiss a skill so its body is no longer re-injected."""

            name = (kwargs.get("skill") or "").strip()
            reason = kwargs.get("reason")

            if not name:
                return "Invalid skill name: empty."

            # Check if the skill exists
            found = next((s for s in mw.skills if s.name == name), None)
            if found is None:
                known = ", ".join(s.name for s in mw.skills) or "(none)"
                return f"Unknown skill: {name}. Available: {known}"

            # Record the dismissal in the audit log
            mw._audit(
                AUDIT_DISMISSED,
                skill=name,
                detail=reason or "dismissed by model",
            )
            logger.info("Skill %r dismissed by model%s", name, f": {reason}" if reason else "")

            # Return a marker that before_model can detect
            return (
                f"Skill '{name}' has been dismissed. Its instructions will no longer be injected."
            )

        # 规范工具名
        # Canonical tool name
        skill_dismiss.name = "skill_dismiss"
        return skill_dismiss

    # ------------------------------------------------------------------
    # allowed-tools 闸门：wrap_tool_call（窗口的执行侧）
    # allowed-tools gate: wrap_tool_call (enforcement side of the window)
    # ------------------------------------------------------------------

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        """每次工具执行前检查 allowed-tools 窗口，拦截违规调用。
        Check the allowed-tools window before every tool execution and block violations.

        拦截意味着 handler 不会执行：模型收到一条 ToolMessage 说明该工具
        本轮不可用，并被要求用白名单内的工具完成任务。
        Blocking means the handler never runs: the model receives a
        ToolMessage explaining that the tool is off-limits this turn, and is
        expected to finish the task with whitelisted tools.
        """
        state = request.state
        messages = (
            state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        )
        block_reason = check_allowed_tools(
            messages,
            request.tool_call["name"],
            {s.name: s for s in self.skills},
        )
        if block_reason is not None:
            self._audit(
                AUDIT_TOOL_BLOCKED,
                tool_call_id=request.tool_call.get("id"),
                detail=f"tool {request.tool_call['name']!r} blocked by allowed-tools",
            )
            logger.info("Blocked tool call %r: %s", request.tool_call["name"], block_reason)
            return ToolMessage(
                content=block_reason,
                tool_call_id=request.tool_call["id"],
                name=request.tool_call["name"],
            )
        return handler(request)

    # ------------------------------------------------------------------
    # 第一层 + 摘要压缩存活：before_model
    # Layer 1 + summarization survival: before_model
    # ------------------------------------------------------------------

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """每次模型调用前执行，四项工作均以 state 更新形式生效：
        Runs before every model call. Four jobs, all applied as state updates:
        1. 记录新的技能调用 / record new skill invocations
        2. 处理技能退场 / process skill dismissals
        3. 播报技能清单（首轮或增量）/ announce the skill listing (first round or delta)
        4. 重注入被摘要压缩掉的正文（跳过已退场的技能）/ re-inject bodies that
           summarization dropped (skip dismissed skills)
        """
        messages = state.get("messages", [])
        announced = list(state.get("announced_skills", []))
        invocations = dict(state.get("skill_invocations", {}))
        dismissed = dict(state.get("dismissed_skills", {}))
        initial_invocation_count = len(invocations)
        updates: dict[str, Any] = {}
        new_messages: list[SystemMessage] = []

        # 1. 账本：扫描消息中的技能工具调用。记录在模型发出调用时就写入——
        #    早于工具实际执行——因此记录一定先于任何摘要压缩。
        # 1. Ledger: scan messages for skill tool calls. Recording happens as
        #    soon as the model calls the tool — even before the tool runs —
        #    so the record always predates any summarization.
        for m in messages:
            if isinstance(m, AIMessage) and m.tool_calls:
                for tc in m.tool_calls:
                    if tc["name"] == "skill" and tc["id"] not in invocations:
                        invocations[tc["id"]] = {
                            "skill": tc["args"].get("skill", ""),
                            "args": tc["args"].get("args"),
                        }

        # 2. 退场：扫描消息中的 skill_dismiss 工具调用。
        # 2. Dismissal: scan messages for skill_dismiss tool calls.
        for m in messages:
            if isinstance(m, AIMessage) and m.tool_calls:
                for tc in m.tool_calls:
                    if tc["name"] == "skill_dismiss":
                        skill_name = tc["args"].get("skill", "")
                        if skill_name and skill_name not in dismissed:
                            dismissed[skill_name] = True
                            logger.info("Skill %r marked as dismissed", skill_name)

        # 4. 摘要压缩存活：已记录的调用若其 tool_call_id 不再出现在消息中，
        #    说明该轮（AIMessage + ToolMessage）被压缩掉了——重新展开并注入正文。
        #    跳过已退场的技能。
        # 4. Summarization survival: a recorded invocation whose tool_call_id
        #    no longer appears in the messages had its turn (AIMessage +
        #    ToolMessage) compressed away — re-expand and re-inject the body.
        #    Skip dismissed skills.
        if invocations:
            live_ids = set()
            for m in messages:
                if isinstance(m, AIMessage):
                    for tc in m.tool_calls or []:
                        live_ids.add(tc["id"])
            by_name = {s.name: s for s in self.skills}
            for tc_id in list(invocations):
                record = invocations[tc_id]
                if tc_id in live_ids:
                    continue
                # 跳过已退场的技能：删除记录，不再重注入。
                # Skip dismissed skills: drop the record, no re-injection.
                if record["skill"] in dismissed:
                    del invocations[tc_id]
                    logger.info("Skipping re-injection of dismissed skill %r", record["skill"])
                    continue
                target = by_name.get(record["skill"])
                if target is None:
                    # 技能已被移除：删除记录，避免无限重试。
                    # Skill was removed: drop the record instead of retrying forever.
                    del invocations[tc_id]
                    continue
                # 重注入前再次检查权限策略——被拒绝的技能不能通过此路径绕过。
                # Re-check the permission policy before re-injection — a
                # denied skill must not bypass the policy through this path.
                if self.permission_policy(target, record["args"]) != "allow":
                    del invocations[tc_id]
                    continue
                try:
                    content = expand_skill(
                        target,
                        record["args"],
                        session_id=self.session_id,
                        enable_shell_blocks=self.enable_shell_blocks,
                    )
                except Exception as e:  # noqa: BLE001 - 单个技能出错不能拖垮 agent / one bad skill must not kill the agent
                    logger.warning("Failed to re-inject skill %r: %s", record["skill"], e)
                    continue
                new_messages.append(
                    wrap_in_system_reminder(
                        "The following skills were invoked in this session. "
                        "Continue to follow these guidelines:\n\n"
                        f"### Skill: {record['skill']}\n\n{content}"
                    )
                )
                self._audit(
                    AUDIT_REINJECTED,
                    skill=record["skill"],
                    args=record["args"],
                    tool_call_id=tc_id,
                )
                logger.info(
                    "Re-injected skill %r (its body was dropped by summarization)",
                    record["skill"],
                )

        # 2. 增量播报：仅播报之前未播报过的技能。
        # 2. Delta announcement: only skills not announced before.
        listable = [s for s in self.skills if not s.disable_model_invocation]
        new_skills = [s for s in listable if s.name not in announced]
        if new_skills:
            listing = format_skills_within_budget(new_skills, self.context_window_tokens)
            new_messages.append(
                wrap_in_system_reminder(
                    f"The following skills are available for use with the skill tool:\n\n{listing}"
                )
            )
            announced.extend(s.name for s in new_skills)
            updates["announced_skills"] = announced
            self._audit(
                AUDIT_ANNOUNCED,
                detail="newly announced: " + ", ".join(s.name for s in new_skills),
            )
            logger.info(
                "Announced %d skill(s) (%s)",
                len(new_skills),
                "first round" if len(announced) == len(new_skills) else "delta",
            )

        # 新消息（清单/重注入）或账本变更都需要写入 state。账本不能跳过：
        # 技能调用发生在两次模型调用之间，若本轮不写入记录，后续摘要压缩
        # 工具结果时存活机制将失去证据。
        # New messages (listing / re-injection) or ledger changes both need to
        # land in state. The ledger must not be skipped: skill invocations
        # happen between two model calls, and if the record is not written
        # this turn, a later summarization of the tool result leaves the
        # survival mechanism without evidence.
        if (
            not new_messages
            and len(invocations) == initial_invocation_count
            and len(dismissed) == len(state.get("dismissed_skills", {}))
        ):
            return None

        updates["messages"] = new_messages
        updates["skill_invocations"] = invocations
        updates["dismissed_skills"] = dismissed
        return updates
