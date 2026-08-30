import logging

from skillbay.core import Skill, format_skills_within_budget, get_char_budget, load_skills


def write_skill(root, name, body, frontmatter="description: A test skill"):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return d


def test_load_skills_from_directory(tmp_path):
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir, "alpha", "Alpha body.")
    write_skill(skills_dir, "beta", "Beta body.")

    skills = load_skills([str(skills_dir)])
    assert [s.name for s in skills] == ["alpha", "beta"]
    assert all(s.base_dir and s.base_dir.endswith(s.name) for s in skills)
    assert skills[0].content.strip() == "Alpha body."
    assert skills[0].description == "A test skill"


def test_missing_description_is_skipped(tmp_path, caplog):
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir, "good", "ok")
    write_skill(skills_dir, "bad", "no desc", frontmatter="version: 1")
    with caplog.at_level(logging.WARNING, logger="skillbay.core"):
        skills = load_skills([str(skills_dir)])
    assert [s.name for s in skills] == ["good"]
    assert any("no description" in r.message for r in caplog.records)


def test_later_directory_overrides_earlier(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    write_skill(first, "shared", "first version")
    write_skill(second, "shared", "second version")
    write_skill(second, "extra", "extra")

    skills = load_skills([str(first), str(second)])
    assert [s.name for s in skills] == ["extra", "shared"]
    shared = next(s for s in skills if s.name == "shared")
    assert "second version" in shared.content


def test_loose_markdown_files_are_ignored(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "stray.md").write_text("---\ndescription: x\n---\nbody", encoding="utf-8")
    assert load_skills([str(skills_dir)]) == []


def test_frontmatter_fields_are_parsed(tmp_path):
    fm = (
        "description: d\n"
        "allowed-tools: Read, Bash\n"
        "arguments: pr repo\n"
        "when_to_use: whenever\n"
        "disable-model-invocation: true\n"
    )
    write_skill(tmp_path, "full", "body", fm)
    skills = load_skills([str(tmp_path)])
    s = skills[0]
    assert s.allowed_tools == ["Read", "Bash"]
    assert s.argument_names == ["pr", "repo"]
    assert s.when_to_use == "whenever"
    assert s.disable_model_invocation is True
    assert s.listing_description == "d - whenever"


def test_get_char_budget():
    assert get_char_budget(None) == 8_000
    assert get_char_budget(200_000) == 8_000  # 1% of 200k tokens x 4 chars
    assert get_char_budget(50_000) == 2_000


def test_listing_fits_when_under_budget():
    skills = [Skill(name="a", description="short"), Skill(name="b", description="tiny")]
    out = format_skills_within_budget(skills)
    assert out == "- a: short\n- b: tiny"


def test_listing_degrades_to_names_when_budget_exhausted():
    skills = [Skill(name=f"s{i}", description="x" * 300) for i in range(10)]
    # budget = 1 x 4 x 0.01 = 0 chars -> names only
    out = format_skills_within_budget(skills, context_window_tokens=1)
    assert all(line == f"- s{i}" for i, line in enumerate(out.splitlines()))


def test_listing_truncates_descriptions_in_stage_two():
    skills = [Skill(name="one", description="d" * 100), Skill(name="two", description="e" * 100)]
    # budget 100 chars: full entries (~216) exceed it, but names-only would
    # waste it -> each description gets an equal share >= MIN_DESC_LENGTH.
    out = format_skills_within_budget(skills, context_window_tokens=2500)
    lines = out.splitlines()
    assert len(lines) == 2
    assert all(line.startswith(("- one: ", "- two: ")) for line in lines)
    assert all("…" in line for line in lines)


def test_empty_skill_list_formats_to_empty_string():
    assert format_skills_within_budget([]) == ""
