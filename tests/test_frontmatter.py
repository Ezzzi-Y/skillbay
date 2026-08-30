import logging

from skillbay.frontmatter import coerce_description, parse_boolean, parse_frontmatter


def test_markdown_without_header_passes_through():
    parsed = parse_frontmatter("just text\nno header")
    assert parsed == {"frontmatter": {}, "content": "just text\nno header"}


def test_basic_header_is_split():
    md = "---\nname: demo\ndescription: A test skill\n---\nBody line\n"
    parsed = parse_frontmatter(md)
    assert parsed["frontmatter"]["name"] == "demo"
    assert parsed["frontmatter"]["description"] == "A test skill"
    assert parsed["content"] == "Body line\n"


def test_special_characters_are_repaired_by_quoting():
    # A bare glob value is invalid YAML; the repair pass quotes it and retries.
    md = "---\npaths: **/*.{ts,tsx}\n---\nBody"
    parsed = parse_frontmatter(md)
    assert parsed["frontmatter"]["paths"] == "**/*.{ts,tsx}"
    assert parsed["content"] == "Body"


def test_quoted_values_are_not_double_escaped():
    md = '---\ndescription: "has: colon"\n---\nBody'
    parsed = parse_frontmatter(md)
    assert parsed["frontmatter"]["description"] == "has: colon"


def test_unparseable_header_degrades_to_empty(caplog):
    md = "---\nkey:\n\t- tab indentation is invalid yaml\n---\nBody"
    with caplog.at_level(logging.WARNING, logger="skillbay.frontmatter"):
        parsed = parse_frontmatter(md, "fake/SKILL.md")
    assert parsed["frontmatter"] == {}
    assert parsed["content"] == "Body"
    assert any("Failed to parse frontmatter" in r.message for r in caplog.records)


def test_coerce_description():
    assert coerce_description("text ", "s") == "text"
    assert coerce_description(None, "s") is None
    assert coerce_description(42, "s") == "42"
    assert coerce_description(True, "s") == "True"
    assert coerce_description(["a"], "s") is None
    assert coerce_description("   ", "s") is None


def test_parse_boolean():
    assert parse_boolean(True) is True
    assert parse_boolean("true") is True
    assert parse_boolean("True") is False
    assert parse_boolean(1) is False
    assert parse_boolean(None) is False
