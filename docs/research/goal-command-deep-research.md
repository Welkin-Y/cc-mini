# Deep Research: `/goal` Command Implementation for AI Coding Assistants

**Date:** 2026-06-16
**Scope:** How do leading AI coding assistants implement goal/objective tracking? What UX patterns, system prompt strategies, and Python libraries are proven?
**Stats:** 5 search angles → 21 sources → 98 claims → 25 verified → 13 confirmed, 12 killed → 4 synthesized findings. 103 subagent calls.

---

## Executive Summary

Three distinct architectural patterns exist for implementing a `/goal` command in an AI coding assistant CLI tool:

1. **Stop-hook-based evaluation** (Claude Code) — A session-scoped Stop hook runs a separate small fast model against the full conversation transcript after each turn, returning a yes/no verdict with a reason. `decision: "block"` forces continuation, and the reason guides the next turn. Self-bounding clauses (`"or stop after 20 turns"`) prevent infinite loops.

2. **Thread-scoped persistent state** (Codex) — Goals are SQLite-backed thread-scoped state (not global, not project-level), with event-driven continuation gated on thread-idle, turn-complete, no-pending-work, and never in plan-only mode.

3. **MCP-tool-based task tracking** (CAS) — Tasks are modeled as SQLite-persisted work items with dependencies and priorities, exposed to agents exclusively through MCP tools — no slash-command interface at all.

For dynamic prompt injection that carries goal context across turns, two proven patterns exist: **GSD Pi's layered context stack** (preamble → static → semi-static → dynamic) and **Prompt Poet's Jinja2+YAML two-stage pipeline** with support for calling arbitrary Python functions at render time.

---

## Confirmed Findings

### Finding 1: Claude Code's Stop-Hook Evaluator Architecture

**Confidence:** HIGH (3-0 unanimous verification)

Claude Code implements `/goal` as a wrapper around a session-scoped prompt-based Stop hook. The mechanism:

- After each turn, the **conversation transcript** and **goal condition** are sent to a **separate small fast model** (default Haiku) on the session's configured provider.
- The evaluator returns a **yes/no verdict** with a short **reason**.
- `decision: "block"` in the Stop hook's JSON output prevents the agent from ending its turn, forcing it to continue working toward the goal.
- The `reason` field is fed back as guidance for the next turn — consumed as an instruction by the main agent, **not shown to the user**.
- Goals can include **self-bounding clauses** (e.g., "or stop after 20 turns") to prevent infinite loops; the agent reports progress against the clause each turn and the evaluator judges it from the transcript.
- The evaluator has **no tool access** — it can only judge what the agent has surfaced in the conversation.

**Evidence:** Official Anthropic documentation states verbatim: "/goal is a wrapper around a session-scoped prompt-based Stop hook." Multiple independent community implementations (secemp9/goal, groundtruth, claude-focus, persistent-mode) all use `{"decision": "block", "reason": "..."}` in Stop hooks for auto-continuation. The Rust SDK protocol (docs.rs/turboclaude-protocol) confirms `reason` is "Reason/feedback for Claude (not shown to user)."

**Sources:**
- <https://code.claude.com/docs/en/goal>
- <https://code.claude.com/docs/en/hooks>
- <https://github.com/secemp9/goal>
- <https://egghead.io/force-claude-to-ask-whats-next-with-a-continuous-stop-hook-workflow~oiqzj>

---

### Finding 2: Codex Thread-Scoped Persistent Goals

**Confidence:** HIGH (3-0 unanimous verification)

Codex goals are implemented as **persisted thread state** (SQLite-backed), not as global memory and not as project-level instructions. Key properties:

- **Scope is the thread, not the project** — one active goal per thread.
- Continuation is **event-driven**, not loop-based: `GoalExtension::on_thread_idle`.
- **Safety gates:** the turn must be finished, no other work pending, no user input queued, the thread must be idle, and plan-only work does not trigger continuation.
- **Failure handling:** goals stop auto-continuing after terminal turn failures (Codex v0.138.0).

**Evidence:** The official OpenAI Cookbook (May 2026) states: "Goals are implemented as persisted thread state, not as global memory and not as project-level instructions. The scope is the thread, not the project." PR #18073 creates a `thread_goals` SQLite table; PR #18074 exposes `thread/goal/{get,set,clear}` RPCs; PR #25060 wires continuation to idle events; PR #26147 adds Plan-mode rejection.

**Sources:**
- <https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex>
- <https://github.com/openai/codex> (PRs #18073, #18074, #25060, #26147)

---

### Finding 3: CAS MCP-Tool-Based Task Tracking

**Confidence:** HIGH (2-1 verification)

CAS uses SQLite as the local-first backing store for structured task persistence:

- Tasks modeled as work items with **5 dependency types** (Blocks, Related, ParentChild, DiscoveredFrom, ExtractedFrom)
- **5 priority levels** (CRITICAL through BACKLOG)
- Status tracking: Open, InProgress, Blocked, Closed
- Exposed to agents **exclusively through MCP tools** (`mcp__cas__task`, `mcp__cas__memory`) — no slash commands
- CAS's own CLAUDE.md explicitly instructs: "DO NOT USE BUILT-IN TOOLS (TodoWrite, EnterPlanMode) FOR TASK TRACKING. Use CAS MCP tools instead."

**Evidence:** Source code inspection confirms 25-column `tasks` table, 5-column `dependencies` table, 8-column `task_leases` table. `list_ready()` filters via NOT EXISTS subquery for unclosed blockers; `add_dependency()` includes cycle detection via DFS. CAS has no slash commands — CLI subcommands are operational only (`cas serve`, `cas attach`).

**Sources:**
- <https://github.com/codingagentsystem/cas>
- `crates/cas-store/src/task_store.rs`, `crates/cas-types/src/task.rs`, `crates/cas-types/src/dependency.rs`

---

### Finding 4: Proven Dynamic Prompt Injection Patterns

**Confidence:** HIGH (2-1 to 3-0 verification)

Two concrete, production-tested patterns for dynamically injecting goal context into LLM system prompts:

**GSD Pi's Layered Context Stack:**
- Assembles prompts per unit type in fixed order: preamble (rules) → static (PROJECT.md, REQUIREMENTS.md) → semi-static (memories) → dynamic (active plan, carry-forward captures, gate list)
- Per-category token budgets with section-boundary-aware truncation
- Goal context sits in the "dynamic" layer, clearly separated from static instructions

**Prompt Poet's Jinja2+YAML Pipeline:**
- Two-stage: Jinja2 renders template data into YAML → YAML parsed into structured `PromptPart` objects (name, content, role, truncation_priority)
- Supports calling **arbitrary Python functions** inside Jinja2 templates at render time (e.g., `extract_user_query_topic()`, `fetch_few_shot_examples()`)
- Jinja2 is explicitly not sandboxed — this is a design choice enabling runtime data retrieval and conditional section injection

**Sources:**
- <https://github.com/open-gsd/gsd-pi>
- <https://github.com/character-ai/prompt-poet>
- <https://pypi.org/project/prompt-poet/0.0.51/>

---

## Refuted Claims

These claims were verified and rejected (0-3 or 1-2 votes):

| Claim | Vote | Source |
|---|---|---|
| GSD Pi uses a SQLite-backed state machine with 29 dispatch rules for cross-session goal tracking | 0-3 | gsd-pi |
| GSD Pi's execute-task prompt enforces a Completion Contract requiring `gsd_task_complete` | 0-3 | gsd-pi |
| Effective goal specs define 6 components (outcome, verification surface, constraints, boundaries, iteration policy, stop-condition) | 0-3 | OpenAI Codex Cookbook |
| Evidence-based completion auditing required before marking goal complete | 1-2 | OpenAI Codex Cookbook |
| Three-hook pipeline (UserPromptSubmit, PostToolUse, Stop) for full lifecycle goal tracking | 0-3 | secemp9/goal |
| Kanban-style todo tool suite sufficient for session-scoped objectives without separate /goal | 0-3 | arxiv:2603.05344 |
| Planner subagent with fixed 7-section template generalizes across all coding tasks | 0-3 | arxiv:2603.05344 |
| Event-driven user-role reminders counter LLM instruction fade-out | 0-3 | arxiv:2603.05344 |
| Persistent goals in `self/goals.md` re-injected via warm context block every 10 turns | 0-3 | Aries-cli |
| Warm context block (~2K tokens) survives context compaction | 0-3 | Aries-cli |
| CAS supervisor-worker architecture for epics→tasks decomposition | 0-3 | CAS |
| Kaban as external MCP server with 20+ Kanban tools | 0-3 | kaban-board |

---

## All Sources

| # | URL | Quality | Angle |
|---|---|---|---|
| 1 | <https://github.com/secemp9/goal> | secondary | Broad landscape survey |
| 2 | <https://export.arxiv.org/html/2603.05344> | secondary | Broad landscape survey |
| 3 | <https://github.com/open-gsd/gsd-pi> | primary | Broad landscape survey |
| 4 | <https://github.com/NguyenSiTrung/Conductor-Beads> | blog | Broad landscape survey |
| 5 | <https://github.com/aayoawoyemi/Aries-cli> | secondary | Broad landscape survey |
| 6 | <https://github.com/codingagentsystem/cas> | secondary | Broad landscape survey |
| 7 | <https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex> | primary | UX patterns and stop-hooks |
| 8 | <https://dev.to/evan-dong/codex-v01280-goal-keeps-working-until-its-done-even-across-sessions-5d85> | blog | UX patterns and stop-hooks |
| 9 | <https://egghead.io/force-claude-to-ask-whats-next-with-a-continuous-stop-hook-workflow~oiqzj> | secondary | UX patterns and stop-hooks |
| 10 | <https://www.smashingmagazine.com/2026/02/designing-agentic-ai-practical-ux-patterns/> | unreliable | UX patterns and stop-hooks |
| 11 | <https://apidog.com/blog/goal-command-codex-claude-code-autonomous-agents/> | blog | UX patterns and stop-hooks |
| 12 | <https://futureagi.com/blog/agent-cli-developer-experience-2026/> | blog | UX patterns and stop-hooks |
| 13 | <https://code.claude.com/docs/en/goal> | primary | System prompt engineering |
| 14 | <https://dev.to/frank_brsrk/why-your-llm-agent-drifts-off-task-by-step-4-and-why-prompts-cant-fix-it-5ha6> | blog | System prompt engineering |
| 15 | <https://github.com/repowise-dev/claude-code-prompts> | blog | System prompt engineering |
| 16 | <https://github.com/kaban-board/kaban> | secondary | State of the art goal tracking |
| 17 | <https://github.com/snowtema/drift> | secondary | State of the art goal tracking |
| 18 | <https://socket.dev/npm/package/%40clawui%2Fcli> | blog | State of the art goal tracking |
| 19 | <https://pypi.org/project/prompt-poet/0.0.51/> | primary | Python prompt injection |
| 20 | <https://pypi.org/project/dynaprompt/> | secondary | Python prompt injection |
| 21 | <https://pypi.org/project/promptfw/> | secondary | Python prompt injection |

---

## Open Questions

1. How does the Stop hook evaluator handle context compaction during long sessions — is the compacted summary sufficient for the evaluator to judge goal completion accurately, or does this cause false negatives/positives?

2. What is the practical reliability of separate-small-model evaluation for goal completion? Are there failure modes (false positives where the goal is declared done prematurely, false negatives causing infinite loops)?

3. How do these architectures handle multi-session goals? All three approaches are scoped to a single session/thread — none addressed cross-session goal persistence or resumption.

4. What is the interaction between goal auto-continuation and cost/token budgets? The verified sources mention budget gating but none provide a detailed model.

---

## Caveats

- Claude Code's `/goal` requires v2.1.139+ (May 2026) — architecture is current but may evolve. The Stop hook evaluator's input may be a compacted summary after session compaction, not the literal full transcript.
- Codex goals shipped in v0.128.0 (April 2026) and may still be under active development.
- CAS (v1.0, March 2026) has zero GitHub issues — limited real-world usage data.
- Prompt Poet v0.0.51 is stable but the "arbitrary Python callables" capability is inherently unrestricted — Jinja2 is not sandboxed.
- GSD Pi's documented file paths in `prompt-map.md` are stale relative to actual source locations.
- All patterns are from 2025-2026 — the space is evolving rapidly.

---

## Stats

| Metric | Value |
|---|---|
| Search angles | 5 |
| Sources fetched | 21 |
| Claims extracted | 98 |
| Claims verified (adversarial 3-vote) | 25 |
| Confirmed | 13 |
| Killed | 12 |
| Synthesized findings | 4 |
| URL dedupes | 1 |
| Budget-dropped | 8 |
| Subagent calls | 103 |
