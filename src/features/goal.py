"""Session-scoped /goal command — GoalManager + system prompt injection.

Provides a lightweight goal-setting mechanism for cc-mini sessions.
Goals are injected into the system prompt so the LLM works toward them,
and a stop-hook prevents accidental exit before the goal is met.

Architecture (post deep-research):
  v1 (current): Static stop-hook + self-bounding clauses + turn tracking
                + completion auto-detection from assistant output.
  v2 (future):  Separate evaluator model (Haiku) judges goal completion
                from transcript, auto-continues with ``decision: "block"``.

Pattern follows PlanModeManager (``src/features/plan.py``):
stateful manager constructed in app.py, bound to engine, injected into
system prompt via string manipulation (not full rebuild) to preserve
other injected sections.

Integration surface (the 3 things other modules need):
  - GoalManager.set_goal / clear_goal / show_goal  → slash command handler
  - GoalManager.on_post_turn / on_prompt_rebuilt   → lifecycle hooks
  - GoalManager.is_active / goal_text / status_line_text → stop-hook + status bar
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from core.engine import Engine
    from core.llm import LLMClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GOAL_SECTION_HEADER = "# Session Goal"

# Completion signals: the model announces "Goal met: <summary>" when done.
_COMPLETION_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:Goal met|Goal complete|Goal accomplished)\s*:?\s*(.+?)(?:\n|$)",
    re.IGNORECASE,
)

# Self-bounding clauses: "[stop after N turns]", "(max N turns)", etc.
_TURN_LIMIT_PATTERN = re.compile(
    r"\s*[\[(]\s*(?:stop\s+after|max)\s+(\d{1,3})\s+turns?\s*[\])]\s*$",
    re.IGNORECASE,
)


@dataclass
class GoalState:
    """Immutable snapshot of the current goal."""

    text: str
    turn_limit: Optional[int] = None
    turns_elapsed: int = 0
    completed: bool = False
    completed_summary: str = ""


# ---------------------------------------------------------------------------
# System prompt helpers
# ---------------------------------------------------------------------------

_GOAL_SECTION_HEADER = "# Session Goal"


def _build_goal_system_section(state: GoalState) -> str:
    """Build the ``# Session Goal`` system prompt section."""
    parts: list[str] = []
    parts.append(f"{_GOAL_SECTION_HEADER}")

    if state.completed:
        # Goal was auto-detected as done — model should acknowledge and move on
        parts.append(
            f"The goal \"{state.text}\" appears to be complete "
            f"({state.completed_summary})."
        )
        parts.append("")
        parts.append(
            "Tell the user the goal is done and suggest they run /goal clear."
        )
        parts.append("")
        parts.append(
            "A session-scoped Stop hook is active: exiting requires confirmation "
            "while this goal is active."
        )
        return "\n".join(parts)

    parts.append(f"You are working toward the following goal: {state.text}")
    parts.append("")
    parts.append("Instructions:")
    parts.append(
        "- On your FIRST turn with this goal, break it down into concrete, "
        "verifiable sub-steps. Output them as a numbered checklist "
        '(e.g. "1. [ ] Reproduce the bug\\n2. [ ] Find root cause\\n...").'
    )
    parts.append(
        "- At the START of every subsequent turn, reproduce the checklist "
        "with items marked [x] for done, [ ] for pending. Pick the next "
        "uncompleted sub-step and work on it. Be concise — one line per item."
    )
    parts.append(
        "- Work through sub-steps ONE AT A TIME. Do not jump ahead. "
        "Each turn should make verifiable progress on exactly one sub-step."
    )
    parts.append(
        '- When ALL sub-steps are done, clearly announce: '
        '"Goal met: <summary of what was achieved>" '
        "The system will detect this and the user can then run /goal clear."
    )
    parts.append(
        "- If blocked or uncertain how to proceed on a sub-step, "
        "ask the user for guidance rather than looping or guessing."
    )
    if state.turn_limit is not None:
        parts.append("")
        parts.append(
            f"- Progress: turn {state.turns_elapsed}/{state.turn_limit}. "
            "If you reach the limit without completing the goal, "
            "summarize what was accomplished and what remains."
        )
    parts.append("")
    parts.append(
        "A session-scoped Stop hook is active: exiting requires confirmation "
        "while this goal is active."
    )
    return "\n".join(parts)


def _inject_goal_section(prompt: str, state: GoalState) -> str:
    """Insert or replace the goal section in *prompt*.

    The section is placed after dynamic sections (env, git, CLAUDE.md) but
    before memory / skills / coordinator sections.  If a goal section already
    exists it is replaced in-place.
    """
    prompt = _remove_goal_section(prompt)
    section = _build_goal_system_section(state)
    # Insert before "# Auto Memory" if present, otherwise append
    if "# Auto Memory" in prompt:
        idx = prompt.index("# Auto Memory")
        return prompt[:idx] + section + "\n\n" + prompt[idx:]
    return prompt.rstrip("\n") + "\n\n" + section


def _remove_goal_section(prompt: str) -> str:
    """Strip any existing ``# Session Goal`` section from *prompt*."""
    header = f"{_GOAL_SECTION_HEADER}\n"
    if header not in prompt:
        return prompt
    idx = prompt.index(header)
    rest = prompt[idx:]
    # Find the next top-level section header
    m = re.search(r"\n# [A-Za-z]", rest[len(header):])
    if m:
        end = idx + len(header) + m.start() + 1  # +1 for the newline
        return prompt[:idx] + prompt[end:]
    # No subsequent header — remove to end, but keep a trailing newline
    return prompt[:idx].rstrip("\n") + "\n"


def _parse_turn_limit(text: str) -> tuple[str, Optional[int]]:
    """Extract a self-bounding turn-limit clause from *text*.

    Returns ``(cleaned_text, turn_limit_or_None)``.  Recognised forms:
    ``[stop after N turns]``, ``(max N turns)``, ``(stop after N turns)``.
    """
    m = _TURN_LIMIT_PATTERN.search(text)
    if m is None:
        return text, None
    limit = int(m.group(1))
    cleaned = text[:m.start()] + text[m.end():]
    return cleaned.strip(), limit


# ---------------------------------------------------------------------------
# Evaluator model — separate cheap LLM call to judge goal completion
# ---------------------------------------------------------------------------

# Cheapest model per provider suitable for a yes/no transcript evaluation.
_EVALUATOR_MODEL: dict[str, str] = {
    "anthropic": "claude-3-5-haiku-20241022",
    "openai": "gpt-4o-mini",
    "lmstudio": "",  # filled from engine config at runtime
}

_EVALUATOR_SYSTEM_PROMPT = """\
You are a goal-completion evaluator. Your job is to read a conversation
transcript between a user, an AI coding assistant, and tool outputs, then
decide whether the stated goal has been accomplished.

Rules:
- Look for concrete evidence: files changed, commands run, tests passed,
  errors resolved, features implemented.  Do NOT rely on the assistant
  merely *claiming* the goal is done — check the tool outputs.
- If the assistant is still working through sub-steps, return "continue".
- If the assistant is blocked, asking for help, or going in circles,
  return "continue" with a reason suggesting what to try next.
- Only return "done" when there is clear, verifiable evidence that the
  goal has been fully accomplished.
- Be strict: when in doubt, return "continue".

Respond with ONLY a single JSON object (no markdown, no backticks):
{"decision": "<done|continue>", "reason": "<one sentence explaining why>"}"""


class GoalEvaluator:
    """Calls a separate small LLM to judge goal completion from the transcript.

    Created by ``GoalManager`` when the engine is bound.  Each call is
    stateless — the evaluator receives the full transcript fresh and
    returns a verdict.
    """

    def __init__(
        self,
        client: LLMClient,
        model: str,
        max_tokens: int = 256,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def evaluate(
        self,
        goal_text: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, str]:
        """Judge whether *goal_text* has been accomplished.

        *messages* is the conversation transcript in Anthropic-format
        (``role`` + ``content`` blocks).  The last 40 messages are used
        to stay within evaluator context limits.

        Returns ``{"decision": "done"|"continue", "reason": "..."}``.
        On any error the safe default is ``"continue"``.
        """
        # Use only recent messages to keep the evaluator call cheap
        recent = messages[-40:] if len(messages) > 40 else messages

        user_content = (
            f"Goal: {goal_text}\n\n"
            "Conversation transcript (most recent last):\n"
            + _format_transcript(recent)
        )

        try:
            result = self._client.create_message(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[{"role": "user", "content": user_content}],
                system=_EVALUATOR_SYSTEM_PROMPT,
            )
            text = _extract_text(result)
            verdict = json.loads(text)
            decision = str(verdict.get("decision", "continue")).lower()
            reason = str(verdict.get("reason", ""))
            if decision not in ("done", "continue"):
                decision = "continue"
            return {"decision": decision, "reason": reason}
        except Exception:
            # On any failure (network, parse error, etc.), default to
            # "continue" so the goal loop doesn't stop prematurely.
            return {"decision": "continue", "reason": "evaluator error — continuing"}


def _format_transcript(messages: list[dict[str, Any]]) -> str:
    """Convert Anthropic-format messages into a compact transcript."""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        # Flatten content blocks into a short text representation
        text = _flatten_content(content)
        if not text:
            continue
        label = {"user": "[user]", "assistant": "[assistant]"}.get(role, f"[{role}]")
        # Truncate very long messages
        if len(text) > 600:
            text = text[:600] + "…"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


def _flatten_content(content: Any) -> str:
    """Convert Anthropic content blocks to a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool_call: {block.get('name', '?')}]")
                elif block.get("type") == "tool_result":
                    result = str(block.get("content", ""))[:200]
                    parts.append(f"[tool_result: {result}]")
        return " ".join(parts)
    return str(content)


def _extract_text(message: Any) -> str:
    """Extract the text content from an LLMMessage (provider-agnostic)."""
    # Anthropic SDK Message
    if hasattr(message, "content") and isinstance(message.content, list):
        parts = []
        for block in message.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return " ".join(parts)
    # Fallback
    return str(message)


# ---------------------------------------------------------------------------
# GoalManager
# ---------------------------------------------------------------------------


class GoalManager:
    """Manages a session-scoped goal: set, clear, show, prompt injection.

    Constructed once at startup in ``app.py``.  Bound to the engine so that
    ``set_goal`` / ``clear_goal`` can mutate ``engine.system_prompt`` directly
    (following the same pattern as ``PlanModeManager``).

    Integration surface for other modules::

        # In slash-command handler:
        gm.set_goal(args) / gm.clear_goal() / gm.show_goal()

        # In lifecycle hooks:
        gm.on_post_turn()          # after each engine turn
        gm.on_prompt_rebuilt()     # after full prompt rebuild (resume, mode switch)

        # In stop-hook + status bar:
        gm.is_active               # bool
        gm.goal_text               # str | None
        gm.completed               # bool — model announced completion
        gm.status_line_text        # str — ready-to-display status bar text
    """

    def __init__(self) -> None:
        self._engine: Optional[Engine] = None
        self._state: Optional[GoalState] = None
        self._status_callback: Optional[Callable[[str], None]] = None
        self._evaluator: Optional[GoalEvaluator] = None

    # -- binding ----------------------------------------------------------

    def bind_engine(self, engine: Engine) -> None:
        """Must be called after the engine is constructed.

        Also initialises the evaluator model (a separate small LLM) if the
        engine's client is available.
        """
        self._engine = engine
        # Wire up the evaluator using the engine's LLM client
        self._evaluator = self._create_evaluator(engine)

    def _create_evaluator(self, engine: Engine) -> Optional[GoalEvaluator]:
        """Create a GoalEvaluator from the engine's client config."""
        try:
            from core.llm import LLMClient

            client = getattr(engine, "_client", None)
            if client is None:
                return None

            provider = getattr(client, "provider", "anthropic")
            model = _EVALUATOR_MODEL.get(provider, "")
            if not model:
                # LM Studio or unknown provider — use the session model
                model = engine.get_model()

            # Create a separate client for the evaluator (isolated from session)
            api_key = getattr(client, "_api_key", None)
            base_url = getattr(client, "_base_url", None)
            eval_client = LLMClient(provider=provider, api_key=api_key, base_url=base_url)

            return GoalEvaluator(client=eval_client, model=model)
        except Exception:
            return None

    def set_status_callback(self, cb: Callable[[str], None]) -> None:
        """Register a callback invoked whenever the status-line text changes.

        The callback receives the new ``status_line_text`` (empty string when
        no goal is active).  This lets the TUI layer stay in sync without
        polling the manager after every event.
        """
        self._status_callback = cb

    # -- properties -------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._state is not None

    @property
    def goal_text(self) -> Optional[str]:
        return self._state.text if self._state else None

    @property
    def goal_state(self) -> Optional[GoalState]:
        return self._state

    @property
    def completed(self) -> bool:
        return self._state.completed if self._state else False

    @property
    def status_line_text(self) -> str:
        """A single line suitable for the TUI status bar, or ``""``."""
        if self._state is None:
            return ""
        if self._state.completed:
            return f"{self._state.text}  [✓ done]"
        parts = [self._state.text]
        if self._state.turn_limit is not None:
            parts.append(f"({self._state.turns_elapsed}/{self._state.turn_limit})")
        return " ".join(parts)

    # -- actions ----------------------------------------------------------

    def set_goal(self, text: str) -> str:
        """Set the session goal and inject its section into the system prompt.

        Returns a confirmation message suitable for display.
        """
        raw = text.strip()
        if not raw:
            return self.show_goal()

        cleaned, turn_limit = _parse_turn_limit(raw)
        self._state = GoalState(text=cleaned, turn_limit=turn_limit, turns_elapsed=0)

        if self._engine is not None:
            self._engine.system_prompt = _inject_goal_section(
                self._engine.system_prompt, self._state
            )

        self._notify_status()

        msg = f"Goal set: {cleaned}"
        if turn_limit is not None:
            msg += f"  [dim](max {turn_limit} turns)[/dim]"
        return msg

    def clear_goal(self) -> str:
        """Remove the goal and its section from the system prompt.

        Returns a confirmation message.
        """
        if self._state is None:
            return "No goal is currently set."

        old = self._state.text
        self._state = None

        if self._engine is not None:
            self._engine.system_prompt = _remove_goal_section(
                self._engine.system_prompt
            )

        self._notify_status()
        return f"Goal cleared (was: {old})"

    def show_goal(self) -> str:
        """Return the current goal as a displayable message."""
        if self._state is None:
            return "No goal set. Use /goal <description> to set one."

        msg = f"Current goal: {self._state.text}"
        if self._state.completed:
            msg += f"  [green][✓ completed: {self._state.completed_summary}][/green]"
        elif self._state.turn_limit is not None:
            msg += (
                f"  [dim](turn {self._state.turns_elapsed}"
                f"/{self._state.turn_limit})[/dim]"
            )
        return msg

    # -- lifecycle --------------------------------------------------------

    def on_post_turn(self) -> str:
        """Called after each engine turn completes.

        Increments the turn counter, runs the evaluator model (if available)
        to judge goal completion from the transcript, and re-injects the
        updated prompt section.

        Returns the evaluator's ``reason`` string, or ``""`` if no evaluator
        is available.  The caller should inject this as guidance for the next
        turn when the decision is ``"continue"``.
        """
        if self._state is None:
            return ""

        self._state.turns_elapsed += 1
        evaluator_reason = ""

        # --- Evaluator model: separate LLM judges completion from transcript ---
        if not self._state.completed and self._evaluator is not None and self._engine is not None:
            try:
                messages = self._engine.get_messages()
                verdict = self._evaluator.evaluate(self._state.text, messages)
                if verdict.get("decision") == "done":
                    self._state.completed = True
                    self._state.completed_summary = verdict.get("reason", "")
                else:
                    evaluator_reason = verdict.get("reason", "")
            except Exception:
                pass

        # Fallback: scan assistant output for "Goal met:" text signal
        if not self._state.completed and self._engine is not None:
            last_text = self._engine.last_assistant_text()
            m = _COMPLETION_PATTERN.search(last_text)
            if m:
                self._state.completed = True
                self._state.completed_summary = m.group(1).strip()

        # Re-inject with updated turn count / completion status
        if self._engine is not None:
            self._engine.system_prompt = _inject_goal_section(
                self._engine.system_prompt, self._state
            )

        self._notify_status()
        return evaluator_reason

    def on_prompt_rebuilt(self) -> None:
        """Re-inject the goal section after a full prompt rebuild.

        Call this after ``engine.system_prompt`` is replaced wholesale
        (e.g. session resume, mode switch) to restore the goal section.
        """
        if self._state is not None and self._engine is not None:
            self._engine.system_prompt = _inject_goal_section(
                self._engine.system_prompt, self._state
            )

    def get_system_prompt_section(self) -> Optional[str]:
        """Return the prompt section to inject, or *None* if no goal."""
        if self._state is None:
            return None
        return _build_goal_system_section(self._state)

    # -- internal ---------------------------------------------------------

    def _notify_status(self) -> None:
        """Fire the status callback if registered."""
        if self._status_callback is not None:
            try:
                self._status_callback(self.status_line_text)
            except Exception:
                pass
