"""SkillMiddleware: pluggable skill middleware for LangChain's create_agent.

Ported from Claude Code's skill system and reshaped for backend services:

- No human approval: the permission policy is two-state (allow/deny); an
  "ask" return value is treated as deny and audited.
- Skills are deploy artifacts: loaded once at construction, then frozen —
  no runtime discovery.
- Blast radius is bounded by a tool gate: a skill's allowed-tools are
  enforced on every tool call while that skill is active (see
  check_allowed_tools).

Skill accounting (announced_skills / skill_invocations) lives in AgentState
instead of process globals, so it persists with the checkpointer, stays
isolated per thread, and is not re-announced after a resume.

Three-layer progressive disclosure happens here:
    1. Discover  - before_model injects a budgeted listing as a user message.
    2. Invoke    - the model calls the `skill` tool; the body is expanded and
                   returned as a ToolMessage.
    3. Reference - relative paths in the body resolve via the "Base directory"
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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolCallRequest
from pydantic import BaseModel, Field

from .core import Skill, format_skills_within_budget, load_skills
from .expansion import execute_shell_blocks, substitute_arguments

logger = logging.getLogger(__name__)
PACKAGE_LOGGER = logging.getLogger("skillbay")


def _install_verbose_handler() -> None:
    """Emit package INFO records to stderr when verbose=True (replaces the
    old print-based tracing). Idempotent: never installs a second handler."""
    if not any(isinstance(h, logging.StreamHandler) for h in PACKAGE_LOGGER.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[skillbay] %(message)s"))
        PACKAGE_LOGGER.addHandler(handler)
    PACKAGE_LOGGER.setLevel(logging.INFO)


# Permission policy seam: (skill, args) -> "allow" | "deny". Backend services
# resolve permissions at deploy time, so there is no "ask" state at runtime;
# a legacy "ask" return value is treated as deny and audited.
PermissionPolicy = Callable[[Skill, str | None], str]

# Audit event types (the security-relevant moments of the skill system).
AUDIT_INVOKED = "skill_invoked"  # skill expanded successfully
AUDIT_DENIED = "skill_denied"  # rejected by the permission policy
AUDIT_ASK_AS_DENY = "skill_ask_as_deny"  # policy returned "ask"; treated as deny
AUDIT_REINJECTED = "skill_reinjected"  # body re-injected after summarization
AUDIT_ANNOUNCED = "skills_announced"  # listing announced (first round or delta)
AUDIT_TOOL_BLOCKED = "tool_call_blocked"  # blocked by the allowed-tools gate


@dataclass
class AuditEvent:
    """One security-relevant skill-system event, handed to the audit callback.

    Wire it via SkillMiddleware(audit=...); the callback decides whether to
    log, emit metrics, or raise alerts. Without a callback only logging
    remains.
    """

    event: str  # one of the AUDIT_* constants
    skill: str | None = None  # involved skill name (None for listing announcements)
    args: str | None = None  # invocation arguments
    tool_call_id: str | None = None  # related tool_call id
    detail: str = ""  # extra context (blocked tool name / reason)
    timestamp: float = field(default_factory=time.time)


class SkillState(AgentState):
    """Extended agent state: skill accounting fields (persisted by the checkpointer)."""

    announced_skills: NotRequired[list[str]]
    """Skill names already announced in the listing (delta-announcement ledger)."""

    skill_invocations: NotRequired[dict[str, dict[str, str]]]
    """Recorded invocations: tool_call_id -> {"skill": name, "args": args}.
    Basis of the summarization-survival mechanism: if a record exists but its
    tool_call_id is gone from the message list, that turn was summarized away
    and the body must be re-injected."""


def wrap_in_system_reminder(content: str) -> str:
    """Wrap content in a <system-reminder> tag.

    The listing rides on a user message instead of the system prompt —
    Claude Code's original choice: the system prompt is the standing persona
    while the skill listing is a dynamic catalog. The tag itself signals
    system-provided context, not something the user said.
    """
    return f"<system-reminder>\n{content}\n</system-reminder>"


def expand_skill(
    skill: Skill,
    args: str | None,
    *,
    session_id: str,
    enable_shell_blocks: bool = False,
) -> str:
    """Turn a SKILL.md into final instructions (step order mirrors the reference):

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
    """Default policy: allow everything — fits the "skills are reviewed deploy
    artifacts" backend scenario. Pass a custom policy to tighten it."""
    return "allow"


# ---------------------------------------------------------------------------
# allowed-tools gate (pure function, no middleware instance, unit-testable)
# ---------------------------------------------------------------------------

_REMINDER_PREFIX = "<system-reminder>"
_LAUNCH_MARKER = "Launching skill:"


def check_allowed_tools(
    messages: list[Any],
    tool_name: str,
    skills_by_name: dict[str, Skill],
) -> str | None:
    """Return a block reason if this tool call violates an active allowed-tools window.

    The window is derived from the message history alone (no extra state):
    - opens when a skill call succeeds (its ToolMessage starts with
      "Launching skill:"),
    - closes at the next real user message (system-reminder injections do
      not close it),
    - while open, the union of allowed-tools of all successfully invoked
      skills that declare one forms the usable tool set; skills without
      allowed-tools never loosen an existing restriction (restrictions only
      tighten).

    Why a successful pairing matters: when the policy denies a skill call
    there is no "Launching skill:" ToolMessage, so a denied skill never
    activates its whitelist — denial must not become a privilege-escalation
    path.

    Returns None to allow, or a reason string to be returned to the model.
    """
    active_allowed: set[str] | None = None  # None = unrestricted
    restricting: set[str] = set()  # which skills' allowed-tools are in force
    pending: dict[str, str] = {}  # tool_call_id -> skill name (awaiting ToolMessage)

    for m in messages:
        if isinstance(m, HumanMessage):
            content = m.content if isinstance(m.content, str) else ""
            # system-reminder injections are not new tasks; they keep the window open
            if not content.startswith(_REMINDER_PREFIX):
                active_allowed = None
                restricting = set()
                pending = {}
        elif isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                if tc.get("name") == "skill":
                    pending[tc["id"]] = str(tc.get("args", {}).get("skill", ""))
        elif isinstance(m, ToolMessage) and m.tool_call_id in pending:
            name = pending.pop(m.tool_call_id)
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
    # The skill tool itself is blocked inside the window as well, so a
    # restricted skill cannot chain into an unrestricted one mid-turn.
    return (
        f"Blocked by skill allowed-tools: skills [{', '.join(sorted(restricting))}] "
        f"restricted this turn to [{', '.join(sorted(active_allowed))}], "
        f"but '{tool_name}' is not in the list. "
        "Finish the task with the allowed tools only."
    )


class SkillMiddleware(AgentMiddleware):
    """Pluggable skill middleware: create_agent(model, middleware=[SkillMiddleware(...)]).

    Args:
        skills_dirs: Skill directories; later entries take priority on name
            conflicts. The set is loaded once at construction and frozen —
            decided at deploy time, unchanged at runtime.
        context_window_tokens: Model context window size, used for the 1%
            listing budget; defaults to an 8000-char fallback.
        permission_policy: Policy seam, (skill, args) -> "allow" | "deny".
            Defaults to allow-all (skills ship through code review). An
            "ask" return value is treated as "deny" and audited: backend
            services have no human to confirm.
        audit: Audit callback, (AuditEvent) -> None. Invocations, denials,
            tool blocks, re-injections and announcements all emit events.
        enable_shell_blocks: Whether to execute !`...` blocks in skill
            bodies. Default False — a safety switch, not a preference.
        verbose: Emit key events (listing injection, invocations,
            re-injections) to stderr for observation.
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
        # The skill tool this middleware registers (closure over self).
        self.tools = [self._make_skill_tool()]

    def _audit(self, event: str, **kwargs: Any) -> None:
        """Emit an audit event; without a callback this is a no-op. Audit
        failures must never take down the main flow."""
        if self.audit is not None:
            try:
                self.audit(AuditEvent(event=event, **kwargs))
            except Exception as e:  # noqa: BLE001 - callback errors are not business errors
                logger.warning("Audit callback raised: %s", e)

    # ------------------------------------------------------------------
    # Layer 2: the skill tool (mirrors SkillTool.validate_input / call)
    # ------------------------------------------------------------------

    def _make_skill_tool(self):
        """Build the `skill` tool as a closure so it can reach instance state
        (skill table, permission policy, session id).

        The input schema is declared explicitly via args_schema instead of
        the function signature: parameter names that clash with BaseTool
        internals get renamed by langchain-core, and args_schema keeps the
        model-facing field names as skill / args, identical to Claude Code.
        """
        mw = self

        class SkillInput(BaseModel):
            """Input schema for the skill tool (fields mirror Claude Code's SkillTool)."""

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

            # -- checkPermissions: two-state policy (allow/deny) --
            decision = mw.permission_policy(found, raw_args)
            if decision == "deny":
                mw._audit(
                    AUDIT_DENIED,
                    skill=name,
                    args=raw_args,
                    detail="permission policy returned deny",
                )
                return f"Skill {name} execution blocked by permission policy."
            if decision != "allow":
                # No human in the loop: "ask" is treated as deny, with a
                # distinct audit event so policy authors notice the
                # unsupported return value.
                mw._audit(
                    AUDIT_ASK_AS_DENY,
                    skill=name,
                    args=raw_args,
                    detail=f"permission policy returned {decision!r}; treated as deny",
                )
                return (
                    f"Skill {name} execution blocked by permission policy. "
                    "(Note: 'ask' is not supported in service mode; "
                    "the policy must return allow or deny.)"
                )

            # -- call: expand the body and return it as a ToolMessage --
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

        # Canonical tool name: models write exactly this in tool_calls.
        skill.name = "skill"
        return skill

    # ------------------------------------------------------------------
    # allowed-tools gate: wrap_tool_call (enforcement side of the window)
    # ------------------------------------------------------------------

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage],
    ) -> ToolMessage:
        """Check the allowed-tools window before every tool execution and
        block violations.

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
    # Layer 1 + summarization survival: before_model
    # ------------------------------------------------------------------

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Runs before every model call. Three jobs, all applied as state updates:
        1. record new skill invocations
        2. announce the skill listing (first round or delta)
        3. re-inject bodies that summarization dropped
        """
        messages = state.get("messages", [])
        announced = list(state.get("announced_skills", []))
        invocations = dict(state.get("skill_invocations", {}))
        initial_invocation_count = len(invocations)
        updates: dict[str, Any] = {}
        new_messages: list[HumanMessage] = []

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

        # 3. Summarization survival: a recorded invocation whose tool_call_id
        #    no longer appears in the messages had its turn (AIMessage +
        #    ToolMessage) compressed away — re-expand and re-inject the body.
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
                target = by_name.get(record["skill"])
                if target is None:
                    # Skill was removed: drop the record instead of retrying forever.
                    del invocations[tc_id]
                    continue
                # Re-check the permission policy before re-injection — a
                # denied skill must not bypass approval through this path.
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
                except Exception as e:  # noqa: BLE001 - one bad skill must not kill the agent
                    logger.warning("Failed to re-inject skill %r: %s", record["skill"], e)
                    continue
                new_messages.append(
                    HumanMessage(
                        content=wrap_in_system_reminder(
                            "The following skills were invoked in this session. "
                            "Continue to follow these guidelines:\n\n"
                            f"### Skill: {record['skill']}\n\n{content}"
                        )
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

        # 2. Delta announcement: only skills not announced before.
        listable = [s for s in self.skills if not s.disable_model_invocation]
        new_skills = [s for s in listable if s.name not in announced]
        if new_skills:
            listing = format_skills_within_budget(new_skills, self.context_window_tokens)
            new_messages.append(
                HumanMessage(
                    content=wrap_in_system_reminder(
                        "The following skills are available for use with the skill tool:"
                        f"\n\n{listing}"
                    )
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

        # New messages (listing / re-injection) or ledger changes both need to
        # land in state. The ledger must not be skipped: skill invocations
        # happen between two model calls, and if the record is not written
        # this turn, a later summarization of the tool result leaves the
        # survival mechanism without evidence.
        if not new_messages and len(invocations) == initial_invocation_count:
            return None

        updates["messages"] = new_messages
        updates["skill_invocations"] = invocations
        return updates
