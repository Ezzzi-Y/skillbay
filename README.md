<div align="center">

# skillbay

**Pluggable skill middleware for LangChain — a skill system designed for business services.**

[English](./README.md) | [简体中文](./README.zh-CN.md)

![Python](https://img.shields.io/badge/python-3.12+-3776AB?logo=python&logoColor=white)
![LangChain](https://img.shields.io/badge/langchain-1.x-1C3C3C?logo=langchain&logoColor=white)
![uv](https://img.shields.io/badge/uv-managed-DE5FE9)
![Ruff](https://img.shields.io/badge/ruff-checked-261230?logo=ruff)

</div>

## Why skillbay

Anthropic proposes a remarkably good skill model: a single `SKILL.md` file turns a
directory of procedures, references and scripts into capabilities the model can discover
on demand, load lazily, and execute precisely. It is one of the standout contributions of
Anthropic's Harness team in this area, solving a classic problem — *how does one agent
carry many specialties without blowing its context window?*

The mechanism itself is general, but the mainstream implementation was built for one
specific scenario: a developer's coding and workbench. skillbay moves it into another —
**business services**: a consumer app's support entrance, a company's finance/HR
assistant, an ops copilot wired into internal systems. The context-window problem to
solve is the same, but the runtime environment and rules are completely different.

| | Claude Code: coding / workbench | skillbay: business services |
|---|---|---|
| Who talks to the agent | A developer, able to judge "should this command run?" | An end customer or an employee — nobody able to review a tool call, and no conversation that can hang on a confirmation prompt |
| What a skill is | A coding workflow, installed ad hoc by its user | A business capability — refund handling, reimbursement policy, leave inquiry — owned by the company and governed like any business code |
| Whose authority the agent acts with | The developer's own account | The company's — customers never opted into the agent's internals, so blast radius must be bounded by design |
| Session shape | One developer, one terminal session | Thousands of concurrent conversations, checkpointed and resumable |

**skillbay** therefore ports the skill system 1:1 in semantics onto LangChain's middleware
API, then deliberately diverges wherever the scenario — not the mechanics — demands it.
These divergences are the real content of this project.

## Business transformation

### 1. Skills are deploy artifacts, not runtime discoveries

In Claude Code, the person who installs a skill is the person affected by it — runtime
discovery and skill marketplaces are reasonable. A business service breaks that symmetry:
the company operates the agent, while customers and employees bear the consequences. The
skills also differ in kind — refund rules, reimbursement workflows and HR policies are
business capabilities, not developer conveniences. A skill "appearing" in a mounted
directory would put unreviewed behavior in front of customers: no review, no version, no
rollback.

In a coding agent, the person who installs a skill is the person who uses it, so runtime
discovery is reasonable. In a business service, developers provide the skills, customers
use them, and the company bears the consequences. Skills must therefore be supplied by the
backend team — there is no runtime discovery.

skillbay therefore loads the skill set **once, at construction, and freezes it**:

- Skills live in git, go through code review, and ship with the release.
- Runtime file changes have no effect until the process restarts.
- Skill identity is the directory name; the frontmatter `name` is display-only — renames on
  disk cannot silently re-route behavior.

### 2. The allowed-tools gate

An agent in a business service acts as a customer-service/smart assistant — a
customer-service skill must never reach tools beyond its charter. A skill's frontmatter can
declare `allowed-tools`; while that skill is active, the middleware's `wrap_tool_call` hook
**enforces** the whitelist on every single tool call — the model is not trusted to comply,
it is prevented.

The enforcement window is derived purely from the message history (no extra state to
corrupt):

- **Opens** when a skill call succeeds — its ToolMessage starts with `Launching skill:`.
- **Closes** at the next real user message; `<system-reminder>` injections don't count as a
  new task and don't close the window.
- **Union**: when several skills are active at once, their whitelists are unioned, or
  multiple skills are disallowed from being active simultaneously (a subagent mechanism can
  be used to invoke multiple skills) — and it **only tightens**: a skill without
  `allowed-tools` can never loosen an active restriction.
- **The `skill` tool itself is blocked inside the window**: this closes the escalation path
  of calling a restricted skill and then chaining into an unrestricted one.

### 3. Accounting lives in agent state, not process globals

The reference implementation records announced/invoked skills in module-level dicts — fine
for one developer's single-session process; wrong for a customer-service system serving
thousands of resumable, checkpointed conversations at once. skillbay puts both ledgers in
`SkillState` (an `AgentState` extension), which gives three things:

- **Persistence** — the ledgers survive checkpoints; no duplicate announcements after a
  process resumes.
- **Isolation** — per-thread state; concurrent conversations don't interfere with each
  other.
- **Summarization survival** — when a `SummarizationMiddleware` compresses away the turn
  that invoked the skill, the invocation record stays in state. `before_model` notices the
  `tool_call_id` is gone from the message list, re-expands the body, and injects it as a
  system-reminder. The skill's guidance survives the compaction that killed its transcript.

### 4. Skill dismissal — the model can release skills it no longer needs

Other skill systems (Claude Code, Codex, Cursor, and every LangChain skill middleware we
have seen) treat skill activation as fire-and-once: once a skill's body is injected, it
occupies the context window until the conversation ends or the context is compacted away.
That is fine for a developer session — the human knows when the task is done — but wrong
for a service agent handling multi-turn conversations, which naturally drift across topics.

skillbay introduces **skill dismissal**: the model can call `skill_dismiss` to release a
skill whose scope no longer matches the conversation. After dismissal:

- The skill's full body is **no longer re-injected** into the post-compaction context.
- The dismissal is **persisted in agent state** (survives checkpoint/resume).
- An **audit event** (`skill_dismissed`) records which skill was dismissed and why.

The model decides when to dismiss based on the conversation — a customer who asks about
refund policy and then pivots to shipping times does not need the refund skill's 12-step
procedure consuming context for the rest of the session.

It is a small mechanism, but it matters for long-lived service conversations, for two
reasons: it reduces the number of tokens a backend service consumes, and it helps avoid
topic drift.

## Robustness design

- **One broken skill never breaks startup.** Frontmatter parsing never throws: it tries the
  raw text first, retries with auto-quoting on failure (guarding against the classic
  `paths: **/*.{ts,tsx}` mistake), and degrades to an empty header as a last resort. A
  skill missing its `description` is skipped with a warning, not fatal.
- **Audit built in.** Five audit events (`skill_invoked`, `skill_reinjected`,
  `skills_announced`, `tool_call_blocked`, `skill_dismissed`) flow through one callback
  seam; wire it to your logging/metrics stack. Audit failures never take down the agent.
- **Context budget with graceful degradation.** When the listing exceeds its 1% budget,
  each description is truncated to an equal share of what remains; in the extreme case only
  names are announced. The discovery layer never crowds out the task itself.

### LangChain integration

Wire it into any LangChain `create_agent` agent:

```python
from skillbay import SkillMiddleware

mw = SkillMiddleware(
    skills_dirs=["skills"],
    audit=lambda event: print(event),  # route to your observability stack
)
agent = create_agent(model, tools=[...], middleware=[mw])
```

Each `skills_dirs` entry accepts three shapes: a single skill directory itself
(containing `SKILL.md`), a parent of skill directories
(`<dir>/<name>/SKILL.md`), or a root one level higher whose subdirectories are
category folders — scanned at most two levels down.

### Frontmatter reference

| Field | Required | Effect |
|---|---|---|
| `description` | yes | Listing text; drives model selection. Missing → skill skipped. |
| `allowed-tools` | no | Tool whitelist enforced while the skill's window is open. |
| `arguments` | no | Declares named arguments (`$foo`), mapped positionally. |
| `argument-hint` | no | Human-facing hint for the argument string. |
| `when_to_use` | no | Extra trigger guidance, appended to the listing description. |
| `disable-model-invocation` | no | Kept out of the listing; user-triggered only. |
| `shell` | no | Interpreter for `` !`...` `` blocks (feature is off by default). |
| `model` / `paths` / `version` | no | Parsed and carried; enforcement is on the roadmap. |
