"""SKILL.md frontmatter（YAML 头）解析。
SKILL.md frontmatter (YAML header) parsing.

优先使用 PyYAML，未安装时回退到平铺的 key/value 子集解析器。
解析失败的值会自动加引号重试，一个写坏的技能不会拖垮 agent 启动。
Prefers PyYAML and falls back to a flat key/value subset parser when it is
not installed. Unparseable values are repaired (auto-quoted) and retried,
so one malformed skill can never break agent startup.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 使裸 YAML 值产生歧义的字符：{} [] 是流指示符，* & ! 是锚点/标签，
# | > 是块标量，# 开始注释，": " 分隔键值，` @ 是保留字符。
# Characters that make a bare YAML value ambiguous: {} [] are flow indicators,
# * & ! are anchors/tags, | > are block scalars, # starts a comment,
# ": " separates key from value, ` @ are reserved.
_YAML_SPECIAL_CHARS = re.compile(r"[{}[\]*&#!|>%@`]|: ")

# 匹配开头的 --- ... --- 块（尾部换行可选）。
# Matches a leading --- ... --- block (trailing newline optional).
FRONTMATTER_REGEX = re.compile(r"^---\s*\n([\s\S]*?)---\s*\n?")


def _parse_yaml(text: str) -> Any:
    """有 PyYAML 时用它解析，否则用平铺子集解析器。
    Parse YAML with PyYAML when available; otherwise use the flat subset parser."""
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(text)
    except ImportError:
        return _parse_flat_yaml(text)


def _parse_scalar(raw: str) -> Any:
    """将 YAML 标量转换为 Python 值（仅限平铺解析器）。
    Convert a YAML scalar token to a Python value (flat parser only)."""
    value = raw.strip()
    if value == "" or value == "~" or value.lower() == "null":
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    if value == "true":
        return True
    if value == "false":
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _parse_flat_yaml(text: str) -> Any:
    """无 PyYAML 时的回退解析：支持 `key: value`、行内 [a, b] 列表和
    `- item` 块列表。嵌套结构需要 PyYAML。
    Fallback without PyYAML: supports `key: value`, inline [a, b] lists and
    `- item` block lists. Nested structures require PyYAML."""
    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in text.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and current_list_key is not None:
            result[current_list_key].append(_parse_scalar(line.lstrip()[2:]))
            continue
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if value == "":
            # 裸 "key:" 开启一个块列表
            # Bare "key:" opens a block list
            result[key] = []
            current_list_key = key
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            result[key] = [] if not inner else [_parse_scalar(v) for v in inner.split(",")]
            current_list_key = None
        else:
            result[key] = _parse_scalar(value)
            current_list_key = None
    return result


def _quote_problematic_values(frontmatter_text: str) -> str:
    """给包含 YAML 特殊字符的裸值加引号，使 glob 风格的值
    （如 `paths: **/*.{ts,tsx}`）能正确解析。已加引号的行保持不变。
    Quote bare values containing YAML special characters so glob-style
    values (e.g. `paths: **/*.{ts,tsx}`) parse correctly. Already-quoted
    lines are left untouched."""
    lines = frontmatter_text.split("\n")
    result: list[str] = []
    for line in lines:
        match = re.match(r"^([a-zA-Z_-]+):\s+(.+)$", line)
        if match:
            key, value = match.group(1), match.group(2)
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                result.append(line)
                continue
            if _YAML_SPECIAL_CHARS.search(value):
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                result.append(f'{key}: "{escaped}"')
                continue
        result.append(line)
    return "\n".join(result)


def parse_frontmatter(markdown: str, source_path: str | None = None) -> dict[str, Any]:
    """将 markdown 拆分为 {"frontmatter": dict, "content": body}。
    Split markdown into {"frontmatter": dict, "content": body}.

    没有头的 markdown 返回空 frontmatter 和原样正文，调用方可以传入任意
    .md 文件。永不抛出异常：先试原始文本，再试自动加引号的版本，最后
    回退到空头。
    Markdown without a header is returned with an empty frontmatter and the
    body untouched, so callers can feed any .md file. Never raises: the raw
    text is tried first, the auto-quoted variant second, and an empty
    header is the last resort.
    """
    match = FRONTMATTER_REGEX.match(markdown)
    if not match:
        return {"frontmatter": {}, "content": markdown}

    frontmatter_text = match.group(1) or ""
    content = markdown[match.end() :]

    frontmatter: dict[str, Any] = {}
    try:
        parsed = _parse_yaml(frontmatter_text)
        if parsed and isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        # 首次失败：自动加引号后重试（覆盖手写描述中的冒号/花括号，最常见的错误）。
        # First failure: retry with auto-quoting (covers colons/braces in
        # hand-written descriptions, the most common mistake).
        try:
            parsed = _parse_yaml(_quote_problematic_values(frontmatter_text))
            if parsed and isinstance(parsed, dict):
                frontmatter = parsed
        except Exception as retry_error:
            location = f" in {source_path}" if source_path else ""
            logger.warning("Failed to parse frontmatter%s: %s", location, retry_error)

    return {"frontmatter": frontmatter, "content": content}


def coerce_description(value: Any, component_name: str) -> str | None:
    """校验 description 字段：字符串直接通过，数字/布尔转字符串，
    数组/对象拒绝（返回 None 让调用方跳过）。
    Validate a description field: strings pass through, numbers/bools are
    stringified, arrays/objects are rejected (None lets the caller skip)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    logger.warning("%s: description is not a scalar; ignored", component_name)
    return None


def parse_boolean(value: Any) -> bool:
    """布尔字段：仅 True 或 "true" 视为真值（YAML/TS 语义）。
    Boolean field: only True or "true" count as truthy (YAML/TS semantics)."""
    return value is True or value == "true"
