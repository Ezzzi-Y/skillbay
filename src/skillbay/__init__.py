"""skillbay: LangChain 可插拔技能中间件 / pluggable skill middleware for LangChain agents.

面向后端业务服务的 Agent 技能系统：技能在部署时加载并锁定，权限策略只有
allow/deny 两态，每个激活技能受 allowed-tools 闸门约束。

Backend-oriented agent skill system: skills are loaded and frozen at deploy
time, the permission policy is two-state (allow/deny), and every active
skill is bounded by an allowed-tools gate.

Quick start / 快速上手:
    from skillbay import SkillMiddleware

    mw = SkillMiddleware(skills_dirs=["app/skills"])
    agent = create_agent(model, tools=[...], middleware=[mw])
    agent.invoke({"messages": [...]})
"""

from .core import (
    DEFAULT_CHAR_BUDGET,
    MAX_LISTING_DESC_CHARS,
    SKILL_BUDGET_CONTEXT_PERCENT,
    Skill,
    format_skills_within_budget,
    load_skills,
)
from .expansion import execute_shell_blocks, substitute_arguments
from .frontmatter import parse_frontmatter
from .middleware import (
    AUDIT_ANNOUNCED,
    AUDIT_DENIED,
    AUDIT_DISMISSED,
    AUDIT_INVOKED,
    AUDIT_REINJECTED,
    AUDIT_TOOL_BLOCKED,
    AuditEvent,
    SkillMiddleware,
    SkillState,
    check_allowed_tools,
    expand_skill,
    wrap_in_system_reminder,
)

__all__ = [
    # Middleware (main entry point)
    "SkillMiddleware",
    "SkillState",
    # Audit
    "AuditEvent",
    "AUDIT_INVOKED",
    "AUDIT_DENIED",
    "AUDIT_REINJECTED",
    "AUDIT_ANNOUNCED",
    "AUDIT_TOOL_BLOCKED",
    "AUDIT_DISMISSED",
    # allowed-tools gate (pure function, unit-testable)
    "check_allowed_tools",
    # Skill model and loading
    "Skill",
    "load_skills",
    "format_skills_within_budget",
    # Expansion pipeline
    "expand_skill",
    "substitute_arguments",
    "execute_shell_blocks",
    "wrap_in_system_reminder",
    "parse_frontmatter",
    # Budget constants
    "DEFAULT_CHAR_BUDGET",
    "MAX_LISTING_DESC_CHARS",
    "SKILL_BUDGET_CONTEXT_PERCENT",
]
