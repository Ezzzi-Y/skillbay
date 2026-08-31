import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from skillbay import (
    AUDIT_ANNOUNCED,
    AUDIT_DENIED,
    AUDIT_DISMISSED,
    AUDIT_INVOKED,
    AuditEvent,
    Skill,
    SkillMiddleware,
    check_allowed_tools,
    expand_skill,
    wrap_in_system_reminder,
)


def write_skill(root: Path, name: str, body: str, frontmatter: str = "description: d") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return d


def make_skills_dir(tmp_path: Path) -> Path:
    write_skill(tmp_path, "greet", "Hello $ARGUMENTS")
    write_skill(
        tmp_path,
        "restricted",
        "Use only whitelisted tools.",
        "description: d\nallowed-tools: Read, Grep",
    )
    write_skill(
        tmp_path,
        "manual",
        "Human hands only.",
        "description: d\ndisable-model-invocation: true",
    )
    return tmp_path


def skill_call(call_id: str, name: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "skill", "args": {"skill": name}, "id": call_id, "type": "tool_call"}],
    )


def launch_message(call_id: str, name: str) -> ToolMessage:
    return ToolMessage(
        content=f"Launching skill: {name}\n\nbody",
        tool_call_id=call_id,
        name="skill",
    )


def window_messages() -> list:
    return [
        HumanMessage("clean up the repo"),
        skill_call("t1", "restricted"),
        launch_message("t1", "restricted"),
    ]


RESTRICTED = {
    "restricted": Skill(name="restricted", description="d", allowed_tools=["Read", "Grep"])
}


def test_whitelisted_tool_passes_inside_window():
    assert check_allowed_tools(window_messages(), "Read", RESTRICTED) is None


def test_non_whitelisted_tool_is_blocked_inside_window():
    reason = check_allowed_tools(window_messages(), "Write", RESTRICTED)
    assert reason is not None and "allowed-tools" in reason and "Write" in reason


def test_skill_tool_itself_is_blocked_inside_window():
    reason = check_allowed_tools(window_messages(), "skill", RESTRICTED)
    assert reason is not None


def test_window_closes_on_real_user_message_only():
    msgs = window_messages() + [
        wrap_in_system_reminder("announced skills: restricted"),
    ]
    assert check_allowed_tools(msgs, "Write", RESTRICTED) is not None
    msgs.append(HumanMessage("next task"))
    assert check_allowed_tools(msgs, "Write", RESTRICTED) is None


def test_denied_skill_never_activates_its_whitelist():
    # The skill call was rejected, so there is no "Launching skill:" marker.
    msgs = [
        HumanMessage("task"),
        skill_call("t1", "restricted"),
        ToolMessage(
            content="Skill restricted execution blocked by permission policy.", tool_call_id="t1"
        ),
    ]
    assert check_allowed_tools(msgs, "Write", RESTRICTED) is None


def test_skill_without_allowed_tools_imposes_no_restriction():
    skills = {"free": Skill(name="free", description="d", allowed_tools=[])}
    msgs = [HumanMessage("task"), skill_call("t1", "free"), launch_message("t1", "free")]
    assert check_allowed_tools(msgs, "Write", skills) is None


def test_restrictions_only_tighten():
    # A second skill without allowed-tools must not widen the first one's list.
    skills = {
        "restricted": Skill(name="restricted", description="d", allowed_tools=["Read"]),
        "free": Skill(name="free", description="d", allowed_tools=[]),
    }
    msgs = [
        HumanMessage("task"),
        skill_call("t1", "restricted"),
        launch_message("t1", "restricted"),
        skill_call("t2", "free"),
        launch_message("t2", "free"),
    ]
    assert check_allowed_tools(msgs, "Write", skills) is not None


def test_multiple_skills_union_their_whitelists():
    skills = {
        "a": Skill(name="a", description="d", allowed_tools=["Read"]),
        "b": Skill(name="b", description="d", allowed_tools=["Grep"]),
    }
    msgs = [
        HumanMessage("task"),
        skill_call("t1", "a"),
        launch_message("t1", "a"),
        skill_call("t2", "b"),
        launch_message("t2", "b"),
    ]
    assert check_allowed_tools(msgs, "Read", skills) is None
    assert check_allowed_tools(msgs, "Grep", skills) is None
    assert check_allowed_tools(msgs, "Write", skills) is not None


def test_expand_skill_headers_and_placeholders(tmp_path: Path):
    skill_dir = write_skill(
        tmp_path, "demo", "run ${SKILL_DIR}/x with ${SESSION_ID} and $ARGUMENTS"
    )
    skill = Skill(
        name="demo",
        description="d",
        base_dir=str(skill_dir),
        content="run ${SKILL_DIR}/x with ${SESSION_ID} and $ARGUMENTS",
    )
    out = expand_skill(skill, "args-here", session_id="s-123")
    assert out.startswith(f"Base directory for this skill: {skill_dir}")
    assert "${SKILL_DIR}" not in out and "\\" not in out.split("\n", 1)[1]
    assert "${SESSION_ID}" not in out and "s-123" in out
    assert "args-here" in out


def test_middleware_invokes_skill(tmp_path: Path):
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    result = mw.tools[0].invoke({"skill": "greet", "args": "world"})
    assert result.startswith("Launching skill: greet")
    assert "Hello world" in result


def test_middleware_rejects_unknown_skill(tmp_path: Path):
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    result = mw.tools[0].invoke({"skill": "nope"})
    assert "Unknown skill: nope" in result and "greet" in result


def test_middleware_tolerates_slash_invocation(tmp_path: Path):
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    assert "Launching skill: greet" in mw.tools[0].invoke({"skill": "/greet", "args": "x"})


def test_disable_model_invocation_is_rejected(tmp_path: Path):
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    result = mw.tools[0].invoke({"skill": "manual"})
    assert "disable-model-invocation" in result


def test_permission_deny(tmp_path: Path):
    events: list[AuditEvent] = []
    mw = SkillMiddleware(
        skills_dirs=[str(make_skills_dir(tmp_path))],
        permission_policy=lambda skill, args: "deny",
        audit=events.append,
    )
    result = mw.tools[0].invoke({"skill": "greet", "args": "x"})
    assert "blocked by permission policy" in result
    assert [e.event for e in events] == [AUDIT_DENIED]


def test_unexpected_policy_return_is_denied(tmp_path: Path):
    """Any return value other than "allow" is denied."""
    events: list[AuditEvent] = []
    mw = SkillMiddleware(
        skills_dirs=[str(make_skills_dir(tmp_path))],
        permission_policy=lambda skill, args: "oops",
        audit=events.append,
    )
    result = mw.tools[0].invoke({"skill": "greet", "args": "x"})
    assert "blocked by permission policy" in result
    assert events[0].event == AUDIT_DENIED
    assert "oops" in events[0].detail


def test_audit_and_invocation_events(tmp_path: Path):
    events: list[AuditEvent] = []
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))], audit=events.append)
    mw.tools[0].invoke({"skill": "greet", "args": "x"})
    assert [e.event for e in events] == [AUDIT_INVOKED]
    assert events[0].skill == "greet" and events[0].args == "x"


def test_audit_callback_failure_is_swallowed(tmp_path: Path):
    def broken_audit(event: AuditEvent) -> None:
        raise RuntimeError("boom")

    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))], audit=broken_audit)
    result = mw.tools[0].invoke({"skill": "greet", "args": "x"})
    assert result.startswith("Launching skill: greet")


def test_before_model_announces_listing_once(tmp_path: Path):
    from langchain_core.messages import SystemMessage

    events: list[AuditEvent] = []
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))], audit=events.append)
    state = {"messages": [], "announced_skills": [], "skill_invocations": {}}

    updates = mw.before_model(state, None)
    assert updates is not None
    msg = updates["messages"][0]
    assert isinstance(msg, SystemMessage)
    content = msg.content
    assert "greet" in content
    # "manual" is hidden from the listing entirely.
    assert "manual" not in content
    assert updates["announced_skills"] == ["greet", "restricted"]
    assert updates["skill_invocations"] == {}
    assert [e.event for e in events] == [AUDIT_ANNOUNCED]

    # Second round with everything announced: no updates at all.
    state2 = {
        "messages": [],
        "announced_skills": ["greet", "restricted"],
        "skill_invocations": {},
    }
    assert mw.before_model(state2, None) is None


def test_before_model_reinjects_summarized_invocations(tmp_path: Path):
    from langchain_core.messages import SystemMessage

    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    state = {
        "messages": [HumanMessage("task")],
        "announced_skills": ["greet", "restricted", "manual"],
        "skill_invocations": {"t1": {"skill": "greet", "args": "world"}},
    }
    updates = mw.before_model(state, None)
    assert updates is not None
    msg = updates["messages"][0]
    assert isinstance(msg, SystemMessage)
    content = msg.content
    assert "Hello world" in content
    assert updates["skill_invocations"] == {"t1": {"skill": "greet", "args": "world"}}


def test_before_model_records_invocations_from_messages(tmp_path: Path):
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    state = {
        "messages": [skill_call("t9", "greet")],
        "announced_skills": ["greet", "restricted", "manual"],
        "skill_invocations": {},
    }
    updates = mw.before_model(state, None)
    assert updates is not None
    assert updates["skill_invocations"] == {"t9": {"skill": "greet", "args": None}}


def test_session_id_is_a_stable_uuid(tmp_path: Path):
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    uuid.UUID(mw.session_id)  # raises if not a valid UUID
    assert mw.session_id == mw.session_id


def test_state_schema_is_skill_state(tmp_path: Path):
    from skillbay import SkillState

    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    assert mw.state_schema is SkillState


def test_wrap_in_system_reminder_returns_system_message():
    """wrap_in_system_reminder should return a SystemMessage, not a string."""
    from langchain_core.messages import SystemMessage

    result = wrap_in_system_reminder("test content")
    assert isinstance(result, SystemMessage)
    assert result.content == "test content"


def test_system_message_does_not_reset_allowed_tools_window():
    """SystemMessage should not close the allowed-tools window."""
    msgs = window_messages() + [
        wrap_in_system_reminder("skill listing"),
    ]
    # Window should still be open (SystemMessage doesn't close it)
    assert check_allowed_tools(msgs, "Write", RESTRICTED) is not None

    # Add a real user message to close the window
    msgs.append(HumanMessage("next task"))
    assert check_allowed_tools(msgs, "Write", RESTRICTED) is None


def test_before_model_injects_system_message_for_listing(tmp_path: Path):
    """before_model should inject skill listing as SystemMessage."""
    from langchain_core.messages import SystemMessage

    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    state = {"messages": [], "announced_skills": [], "skill_invocations": {}}

    updates = mw.before_model(state, None)
    assert updates is not None

    # Should be a SystemMessage, not HumanMessage
    msg = updates["messages"][0]
    assert isinstance(msg, SystemMessage)
    assert "greet" in msg.content
    assert "restricted" in msg.content


def test_before_model_reinjects_as_system_message(tmp_path: Path):
    """Re-injection after summarization should use SystemMessage."""
    from langchain_core.messages import SystemMessage

    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    state = {
        "messages": [HumanMessage("task")],
        "announced_skills": ["greet", "restricted", "manual"],
        "skill_invocations": {"t1": {"skill": "greet", "args": "world"}},
    }

    updates = mw.before_model(state, None)
    assert updates is not None

    # Re-injected content should be SystemMessage
    msg = updates["messages"][0]
    assert isinstance(msg, SystemMessage)
    assert "Hello world" in msg.content


def test_skill_dismiss_tool_exists(tmp_path: Path):
    """SkillMiddleware should have a skill_dismiss tool."""
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    tool_names = [t.name for t in mw.tools]
    assert "skill_dismiss" in tool_names


def test_skill_dismiss_unknown_skill(tmp_path: Path):
    """skill_dismiss should reject unknown skills."""
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])
    dismiss_tool = next(t for t in mw.tools if t.name == "skill_dismiss")
    result = dismiss_tool.invoke({"skill": "nonexistent"})
    assert "Unknown skill" in result


def test_skill_dismiss_valid_skill(tmp_path: Path):
    """skill_dismiss should accept valid skills."""
    events: list[AuditEvent] = []
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))], audit=events.append)
    dismiss_tool = next(t for t in mw.tools if t.name == "skill_dismiss")
    result = dismiss_tool.invoke({"skill": "greet", "reason": "no longer needed"})
    assert "dismissed" in result.lower()
    assert events[-1].event == AUDIT_DISMISSED
    assert events[-1].skill == "greet"


def test_before_model_tracks_dismissed_skills(tmp_path: Path):
    """before_model should track dismissed skills in state."""
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])

    # Simulate a conversation where skill was invoked then dismissed
    state = {
        "messages": [
            HumanMessage("task"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "skill",
                        "args": {"skill": "greet", "args": "world"},
                        "id": "t1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "skill_dismiss",
                        "args": {"skill": "greet", "reason": "task complete"},
                        "id": "t2",
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        "announced_skills": ["greet", "restricted"],
        "skill_invocations": {"t1": {"skill": "greet", "args": "world"}},
        "dismissed_skills": {},
    }

    updates = mw.before_model(state, None)
    assert updates is not None
    assert "dismissed_skills" in updates
    assert updates["dismissed_skills"].get("greet") is True


def test_before_model_skips_reinjection_of_dismissed_skill(tmp_path: Path):
    """before_model should not re-inject dismissed skills."""
    mw = SkillMiddleware(skills_dirs=[str(make_skills_dir(tmp_path))])

    # State with a dismissed skill
    state = {
        "messages": [HumanMessage("task")],
        "announced_skills": ["greet", "restricted"],
        "skill_invocations": {"t1": {"skill": "greet", "args": "world"}},
        "dismissed_skills": {"greet": True},
    }

    updates = mw.before_model(state, None)
    # Should have no new messages (dismissed skill not re-injected)
    # and invocation record should be removed
    if updates:
        assert "messages" not in updates or len(updates["messages"]) == 0
        assert "t1" not in updates.get("skill_invocations", {})
    else:
        # No updates means nothing to re-inject
        assert updates is None
