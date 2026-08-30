"""skillbay: pluggable skill middleware for LangChain agents.

Inspired by Claude Code's skill system and reworked for backend services:
no human-approval state, skills frozen at deploy time, and a hard
allowed-tools gate around every active skill.

Quick start:
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
    AUDIT_ASK_AS_DENY,
    AUDIT_DENIED,
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
    "AUDIT_ASK_AS_DENY",
    "AUDIT_REINJECTED",
    "AUDIT_ANNOUNCED",
    "AUDIT_TOOL_BLOCKED",
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
