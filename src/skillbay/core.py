"""技能数据模型、目录加载与清单格式化。
Skill data model, directory loading, and the listing formatter.

技能遵循 `<skills_dir>/<name>/SKILL.md` 约定。靠后的目录在同名冲突时
覆盖靠前的；通过符号链接或重复父目录加载的副本按 realpath 去重。

Skills follow the `<skills_dir>/<name>/SKILL.md` convention. Later skill
directories override earlier ones on name conflicts; duplicates loaded
through symlinks or repeated parents are removed by realpath.

清单格式化器是「发现层」的菜单文本：必须控制在小预算内（上下文窗口的 1%），
超出时分三级降级。

The listing formatter is the "discovery layer" menu text: it must fit in a
small budget (1% of the context window) and degrades in three stages when
it does not.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from .expansion import parse_argument_names
from .frontmatter import coerce_description, parse_boolean, parse_frontmatter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 清单预算常量 / Listing budget constants
# ---------------------------------------------------------------------------

# The listing may use 1% of the context window (SKILL_BUDGET_CONTEXT_PERCENT).
SKILL_BUDGET_CONTEXT_PERCENT = 0.01
CHARS_PER_TOKEN = 4
# Fallback budget: 1% x 200k tokens x 4 chars/token.
DEFAULT_CHAR_BUDGET = 8_000
# Hard cap per description: the listing only serves discovery — full bodies
# load on invocation, and verbose descriptions just burn cache-building
# tokens on the first turn.
MAX_LISTING_DESC_CHARS = 250
MIN_DESC_LENGTH = 20  # below this the budget is exhausted; keep names only


def get_char_budget(context_window_tokens: int | None = None) -> int:
    """Listing character budget = tokens x 4 chars x 1%; falls back to the default."""
    if context_window_tokens:
        return int(context_window_tokens * CHARS_PER_TOKEN * SKILL_BUDGET_CONTEXT_PERCENT)
    return DEFAULT_CHAR_BUDGET


def _truncate(text: str, max_chars: int) -> str:
    """用省略号截断 / Truncate with an ellipsis.
    使用 len() 而非按显示宽度截断——对 CJK 字符来说宽度约等于字符数，足够精确。
    Uses len() rather than width-aware truncation; close enough for CJK
    where display width roughly equals character count."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def format_skills_within_budget(
    skills: list[Skill],
    context_window_tokens: int | None = None,
) -> str:
    """Format skills as menu text with three-stage degradation when over budget.

    Stages:
        1. Full descriptions fit -> output as-is.
        2. Otherwise -> truncate each description to an equal share of the
           remaining budget (names are always kept).
        3. Fewer than MIN_DESC_LENGTH chars each -> names only ("- name").
    """
    if len(skills) == 0:
        return ""

    def entry(skill: Skill, desc: str) -> str:
        return f"- {skill.name}: {desc}"

    budget = get_char_budget(context_window_tokens)

    full_entries = []
    for s in skills:
        desc = s.listing_description
        if len(desc) > MAX_LISTING_DESC_CHARS:
            desc = desc[: MAX_LISTING_DESC_CHARS - 1] + "…"
        full_entries.append(entry(s, desc))
    full_total = sum(len(e) for e in full_entries) + len(full_entries) - 1
    if full_total <= budget:
        return "\n".join(full_entries)

    # Stage 2: split the remaining budget evenly across descriptions.
    name_overhead = sum(len(s.name) + 4 for s in skills) + (len(skills) - 1)
    available_for_descs = budget - name_overhead
    max_desc_len = available_for_descs // len(skills)

    if max_desc_len < MIN_DESC_LENGTH:
        # Stage 3: extreme case, announce names only.
        return "\n".join(f"- {s.name}" for s in skills)

    return "\n".join(entry(s, _truncate(s.listing_description, max_desc_len)) for s in skills)


# ---------------------------------------------------------------------------
# 技能数据模型 / Skill data model
# ---------------------------------------------------------------------------


@dataclass
class Skill:
    """A loaded skill. Fields map to SKILL.md frontmatter."""

    name: str
    description: str
    base_dir: str | None = None  # skill directory, used for the "Base directory" header
    content: str = ""  # SKILL.md body without the frontmatter
    allowed_tools: list[str] = field(default_factory=list)
    argument_hint: str | None = None  # human-facing hint, e.g. "name"
    argument_names: list[str] = field(default_factory=list)  # named arguments
    when_to_use: str | None = None  # supplemental trigger guidance
    version: str | None = None
    model: str | None = None  # model override (growth point)
    disable_model_invocation: bool = False  # True = user-triggered only
    paths: list[str] | None = None  # conditional activation patterns (growth point)
    source: str = ""  # which skills directory this came from
    shell: str | None = None  # interpreter for shell blocks

    @property
    def listing_description(self) -> str:
        """清单中展示的描述；若设置了 when_to_use 则拼接在后面。
        Description shown in the listing; when_to_use is appended when set."""
        if self.when_to_use:
            return f"{self.description} - {self.when_to_use}"
        return self.description


# ---------------------------------------------------------------------------
# 目录加载 / Directory loading
# ---------------------------------------------------------------------------


def _parse_paths(frontmatter: dict) -> list[str] | None:
    """解析 paths 字段：去掉 /** 后缀；全 ** 值表示无条件激活。
    Parse the paths field: strip the /** suffix; an all-** value means unconditional."""
    if not frontmatter.get("paths"):
        return None
    raw = frontmatter["paths"]
    patterns = raw if isinstance(raw, list) else [raw]
    cleaned = [
        p[:-3] if isinstance(p, str) and p.endswith("/**") else p
        for p in patterns
        if isinstance(p, str) and p
    ]
    if len(cleaned) == 0 or all(p == "**" for p in cleaned):
        return None
    return cleaned


def _parse_tools(value: object) -> list[str]:
    """解析 allowed-tools 字段：接受逗号/空格分隔的字符串或列表；
    "Read, Bash" -> ["Read", "Bash"]。
    Parse the allowed-tools field: accepts a comma/space-separated string
    or a list; "Read, Bash" -> ["Read", "Bash"]."""
    if not value:
        return []
    if isinstance(value, str):
        return [t for t in re.split(r"[,\s]+", value) if t]
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                result.extend(t for t in re.split(r"[,\s]+", item.strip()) if t)
        return result
    return []


def _load_skill_dir(base_path: str) -> list[Skill]:
    """加载单个技能目录：每个 <name>/SKILL.md 子目录即为一个技能。
    Load one skills directory: each subdirectory <name>/SKILL.md is a skill."""
    if not os.path.isdir(base_path):
        return []
    skills: list[Skill] = []
    for entry_name in sorted(os.listdir(base_path)):
        entry_path = os.path.join(base_path, entry_name)
        # 只有目录形态的技能才计入，散落的 .md 文件被忽略。
        # Only directory-shaped skills count; loose .md files are ignored.
        if not os.path.isdir(entry_path) and not os.path.islink(entry_path):
            continue
        skill_file = os.path.join(entry_path, "SKILL.md")
        if not os.path.isfile(skill_file):
            continue
        try:
            with open(skill_file, encoding="utf-8") as f:
                parsed = parse_frontmatter(f.read(), skill_file)
        except OSError as e:
            logger.warning("Failed to read %s: %s", skill_file, e)
            continue

        fm = parsed["frontmatter"]
        markdown_content = parsed["content"]
        # 目录名即技能名；frontmatter 中的 name 仅用于展示。
        # The directory name is the skill name; the frontmatter name is display-only.
        description = coerce_description(fm.get("description"), entry_name)
        if description is None:
            logger.warning("Skill %r has no description; skipped", entry_name)
            continue

        skills.append(
            Skill(
                name=entry_name,
                description=description,
                # 绝对路径：展开后的 "Base directory" 头是模型解析正文中
                # 相对路径的句柄。
                # Absolute path: the expanded "Base directory" header is the
                # model's handle for resolving relative paths in the body.
                base_dir=os.path.abspath(entry_path),
                content=markdown_content,
                allowed_tools=_parse_tools(fm.get("allowed-tools")),
                argument_hint=fm.get("argument-hint"),
                argument_names=parse_argument_names(fm.get("arguments")),
                when_to_use=fm.get("when_to_use"),
                version=fm.get("version"),
                model=fm.get("model") if fm.get("model") not in (None, "inherit") else None,
                disable_model_invocation=parse_boolean(fm.get("disable-model-invocation")),
                paths=_parse_paths(fm),
                source=base_path,
                shell=fm.get("shell"),
            )
        )
    return skills


def load_skills(skills_dirs: list[str]) -> list[Skill]:
    """从多个目录加载技能（公共 API）。
    Load skills from multiple directories (public API).

    覆盖语义：靠后的目录优先，同名技能覆盖靠前的。结果按 realpath
    去重，通过符号链接或重复父目录到达的文件只加载一次。
    Override semantics: later directories take priority and same-named
    skills shadow earlier ones. Results are then deduplicated by realpath,
    so a file reached through a symlink or a repeated parent directory
    loads only once.
    """
    by_name: dict[str, Skill] = {}
    for d in skills_dirs:
        for skill in _load_skill_dir(d):
            by_name[skill.name] = skill  # 后加载的覆盖先加载的 / later loads override earlier ones

    seen_realpaths: dict[str, str] = {}
    result: list[Skill] = []
    for skill in by_name.values():
        if skill.base_dir:
            real = os.path.realpath(skill.base_dir)
            if real in seen_realpaths:
                continue  # 同一底层文件 / same underlying file
            seen_realpaths[real] = skill.name
        result.append(skill)
    return sorted(result, key=lambda s: s.name)
