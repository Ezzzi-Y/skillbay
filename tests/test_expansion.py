import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from skillbay.expansion import (
    execute_shell_blocks,
    parse_argument_names,
    substitute_arguments,
)


def _working_bash() -> str | None:
    """Return a bash that actually runs. On Windows, PATH often resolves to
    WSL's bash stub, which fails when no distro is installed — probe
    candidates (PATH first, then the usual Git for Windows locations)."""
    candidates = [p for p in (shutil.which("bash"),) if p]
    if sys.platform == "win32":
        candidates += [
            str(Path("C:/Program Files/Git/bin/bash.exe")),
            str(Path("C:/Program Files (x86)/Git/bin/bash.exe")),
        ]
    for path in candidates:
        try:
            result = subprocess.run(
                [path, "-c", "echo ok"], capture_output=True, text=True, timeout=10
            )
            if result.stdout.strip() == "ok":
                return path
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


BASH = _working_bash()
requires_bash = pytest.mark.skipif(BASH is None, reason="no working bash available")


def test_parse_argument_names():
    assert parse_argument_names("foo bar") == ["foo", "bar"]
    assert parse_argument_names(["foo", "42", "  "]) == ["foo"]
    assert parse_argument_names(None) == []
    assert parse_argument_names("") == []


def test_full_arguments_placeholder():
    assert substitute_arguments("do $ARGUMENTS now", "a b") == "do a b now"


def test_positional_placeholders():
    body = "first $0, second $1, missing $2"
    assert substitute_arguments(body, "x y") == "first x, second y, missing "


def test_indexed_placeholder_form():
    assert substitute_arguments("pick $ARGUMENTS[1]", "a b") == "pick b"


def test_named_arguments():
    body = "deploy $env to $region"
    assert substitute_arguments(body, "prod eu", argument_names=["env", "region"]) == (
        "deploy prod to eu"
    )


def test_named_arguments_do_not_match_prefixes():
    body = "$foosball and $foo"
    assert substitute_arguments(body, "x", argument_names=["foo"]) == "$foosball and x"


def test_arguments_appended_when_no_placeholder():
    assert substitute_arguments("no placeholder", "keep me") == (
        "no placeholder\n\nARGUMENTS: keep me"
    )


def test_none_arguments_is_noop():
    assert substitute_arguments("body $ARGUMENTS", None) == "body $ARGUMENTS"


def test_quoted_arguments_kept_whole():
    # $0 is the first positional argument; a quoted segment stays one argument ($1).
    assert substitute_arguments("hello $0", 'world "a b"') == "hello world"
    assert substitute_arguments("hello $1", 'world "a b"') == "hello a b"


@requires_bash
def test_shell_block_substitution():
    out = execute_shell_blocks("before !`echo hi` after", shell=BASH)
    assert out == "before hi after"


@requires_bash
def test_fenced_shell_block_substitution():
    out = execute_shell_blocks("```!\necho line1\necho line2\n```", shell=BASH)
    assert "line1" in out and "line2" in out


@requires_bash
def test_failing_command_appends_stderr():
    # Nonzero exit: stdout is kept (if any) and stderr is appended.
    out = execute_shell_blocks("!`echo boom >&2; exit 1`", shell=BASH)
    assert out.endswith("[stderr] boom")


@requires_bash
def test_missing_interpreter_reports_failure():
    out = execute_shell_blocks("before !`echo hi`", shell="definitely-not-a-shell-xyz")
    assert "shell execution failed" in out
