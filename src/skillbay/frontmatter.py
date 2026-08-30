"""SKILL.md frontmatter (YAML header) parsing.

Prefers PyYAML and falls back to a flat key/value subset parser when it is
not installed. Unparseable values are repaired (auto-quoted) and retried, so
one malformed skill can never break agent startup.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Characters that make a bare YAML value ambiguous: {} [] are flow indicators,
# * & ! are anchors/tags, | > are block scalars, # starts a comment,
# ": " separates key from value, ` @ are reserved.
_YAML_SPECIAL_CHARS = re.compile(r"[{}[\]*&#!|>%@`]|: ")

# Matches a leading --- ... --- block (trailing newline optional).
FRONTMATTER_REGEX = re.compile(r"^---\s*\n([\s\S]*?)---\s*\n?")


def _parse_yaml(text: str) -> Any:
    """Parse YAML with PyYAML when available; otherwise use the flat subset parser."""
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(text)
    except ImportError:
        return _parse_flat_yaml(text)


def _parse_scalar(raw: str) -> Any:
    """Convert a YAML scalar token to a Python value (flat parser only)."""
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
    """Fallback without PyYAML: supports `key: value`, inline [a, b] lists and
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
    """Quote bare values containing YAML special characters so glob-style
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
    """Split markdown into {"frontmatter": dict, "content": body}.

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
    """Validate a description field: strings pass through, numbers/bools are
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
    """Boolean field: only True or "true" count as truthy (YAML/TS semantics)."""
    return value is True or value == "true"
