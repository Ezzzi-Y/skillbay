"""Expand a SKILL.md body into its final instructions at invocation time.

The pipeline order is fixed and must not be reordered: base-directory header,
argument substitution ($ARGUMENTS / $0 / $foo), ${SKILL_DIR}, ${SESSION_ID},
and — only if the caller opts in — inline shell blocks.
"""

from __future__ import annotations

import re
import subprocess


def parse_argument_names(argument_names: str | list[str] | None) -> list[str]:
    """Parse the frontmatter `arguments` field (declares named parameters).
    Accepts a whitespace-separated string or a list: "foo bar" -> ["foo", "bar"]."""
    if not argument_names:
        return []

    def valid(name: str) -> bool:
        return bool(name.strip()) and not re.fullmatch(r"\d+", name)

    if isinstance(argument_names, list):
        return [n for n in argument_names if isinstance(n, str) and valid(n)]
    if isinstance(argument_names, str):
        return [n for n in re.split(r"\s+", argument_names) if valid(n)]
    return []


def _parse_arguments(args: str) -> list[str]:
    """Split an argument string into a list. Whitespace-separated, with
    double-quoted segments kept whole: 'foo "hello world" baz' ->
    ["foo", "hello world", "baz"]."""
    if not args or not args.strip():
        return []
    # Match quoted segments or bare non-whitespace runs; keep whichever matched.
    tokens = re.findall(r'"([^"]*)"|(\S+)', args)
    return [quoted if quoted else bare for quoted, bare in tokens]


def substitute_arguments(
    content: str,
    args: str | None,
    *,
    append_if_no_placeholder: bool = True,
    argument_names: list[str] | None = None,
) -> str:
    """Replace argument placeholders in the skill body.

    Supported forms:
        $ARGUMENTS       -> the full argument string
        $ARGUMENTS[0]    -> the first argument
        $0, $1, ...      -> shorthand for the indexed form
        $foo             -> named argument (declared via frontmatter `arguments`)

    If the body has no placeholder at all and args is non-empty, the
    arguments are appended, so user input is never silently dropped by a
    template that forgot to reference it.
    """
    argument_names = argument_names or []
    if args is None:
        return content

    parsed = _parse_arguments(args)
    original = content

    # Named arguments map positionally onto parsed[0..n]. The negative
    # lookahead (?![\[\w]) keeps $foo from matching $foobar or $foo[1].
    for i, name in enumerate(argument_names):
        if not name:
            continue
        replacement = parsed[i] if i < len(parsed) else ""
        content = re.sub(rf"\${name}(?![\[\w])", replacement, content)

    def _by_index(match: re.Match[str]) -> str:
        index = int(match.group(1), 10)
        return parsed[index] if index < len(parsed) else ""

    content = re.sub(r"\$ARGUMENTS\[(\d+)\]", _by_index, content)
    content = re.sub(r"\$(\d+)(?!\w)", _by_index, content)
    content = content.replace("$ARGUMENTS", args)

    if content == original and append_if_no_placeholder and args:
        content = content + f"\n\nARGUMENTS: {args}"
    return content


# Single-line !`...` blocks and fenced ```! multi-line blocks.
_SHELL_BLOCK_RE = re.compile(r"!`([^`]*)`")
_FENCED_SHELL_RE = re.compile(r"```!\s*\n([\s\S]*?)```")


def execute_shell_blocks(
    content: str,
    *,
    shell: str | None = None,
    timeout: float = 30.0,
) -> str:
    """Execute inline shell blocks and replace each with its stdout.

    Security: this runs arbitrary commands written by the skill author.
    Callers must enable it explicitly (enable_shell_blocks); it is off by
    default. Enable only for skills from trusted sources.

    `shell` picks the interpreter: None means bash (Git Bash on Windows).
    On failure the block text is kept as-is, so one bad command does not
    destroy the whole skill body.
    """
    interpreter = shell or "bash"

    def _run(code: str) -> str:
        try:
            result = subprocess.run(
                [interpreter, "-c", code],
                capture_output=True,
                # Git Bash emits UTF-8 regardless of the Windows locale;
                # the locale default (e.g. GBK) would crash decoding.
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            output = result.stdout.rstrip("\n")
            if result.returncode != 0 and result.stderr:
                output = f"{output}\n[stderr] {result.stderr.rstrip()}"
            return output
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"[shell execution failed: {e}]"

    # Fenced blocks first: they are the more specific pattern.
    content = _FENCED_SHELL_RE.sub(lambda m: _run(m.group(1)), content)
    content = _SHELL_BLOCK_RE.sub(lambda m: _run(m.group(1)), content)
    return content
