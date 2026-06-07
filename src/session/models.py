"""Typed dataclasses for session messages, tool calls, and results.

These replace the opaque ``list[dict]`` used by Engine._messages with
strongly-typed, inspectable objects. Every model carries a unique id,
timestamp, and extensible ``meta`` dict.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Message role
# ---------------------------------------------------------------------------

class Role:
    USER = "user"
    ASSISTANT = "assistant"


# ---------------------------------------------------------------------------
# Tool call status
# ---------------------------------------------------------------------------

class ToolStatus:
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    ERRORED = "errored"


# ---------------------------------------------------------------------------
# Content blocks (appear inside assistant messages)
# ---------------------------------------------------------------------------

@dataclass
class TextBlock:
    """Plain text produced by the assistant."""
    text: str
    id: str = field(default_factory=_new_id)


@dataclass
class ToolCallBlock:
    """A tool-use request embedded in an assistant message.

    Lifecycle: pending → executing → completed | errored.
    Observers can watch ``status`` changes and ``started_at`` / ``completed_at``.
    """
    tool_use_id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)

    # -- lifecycle -----------------------------------------------------------
    status: str = ToolStatus.PENDING
    started_at: int | None = None       # epoch ms
    completed_at: int | None = None     # epoch ms
    result: ToolResult | None = None    # set after execution

    # -- metadata ------------------------------------------------------------
    id: str = field(default_factory=_new_id)
    timestamp: int = field(default_factory=_now_ms)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def elapsed_ms(self) -> int | None:
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        if self.started_at:
            return _now_ms() - self.started_at
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in (ToolStatus.COMPLETED, ToolStatus.ERRORED)


@dataclass
class ToolResult:
    """The output of a tool execution."""
    tool_use_id: str
    content: str
    is_error: bool = False
    id: str = field(default_factory=_new_id)
    timestamp: int = field(default_factory=_now_ms)
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@dataclass
class UserMessage:
    """A message from the user."""
    content: str | list[dict[str, Any]]
    role: str = Role.USER
    id: str = field(default_factory=_new_id)
    timestamp: int = field(default_factory=_now_ms)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        parts: list[str] = []
        for block in self.content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)


@dataclass
class AssistantMessage:
    """A message from the assistant — text blocks and/or tool-call blocks."""
    content: list[TextBlock | ToolCallBlock] = field(default_factory=list)
    role: str = Role.ASSISTANT
    id: str = field(default_factory=_new_id)
    timestamp: int = field(default_factory=_now_ms)
    usage: dict[str, int] = field(default_factory=dict)  # token counts
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        parts: list[str] = []
        for block in self.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
        return "".join(parts)

    @property
    def text_blocks(self) -> list[TextBlock]:
        return [b for b in self.content if isinstance(b, TextBlock)]

    @property
    def tool_calls(self) -> list[ToolCallBlock]:
        return [b for b in self.content if isinstance(b, ToolCallBlock)]

    @property
    def has_tool_calls(self) -> bool:
        return any(isinstance(b, ToolCallBlock) for b in self.content)


# ---------------------------------------------------------------------------
# Turn — one user→assistant cycle (may contain multiple tool-call rounds)
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    """A single user-request → final-assistant-response cycle.

    A turn may have intermediate assistant messages containing tool calls,
    followed by tool-result user messages, forming a multi-step loop within
    one user interaction.
    """
    user_message: UserMessage
    assistant_messages: list[AssistantMessage] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)

    @property
    def all_tool_calls(self) -> list[ToolCallBlock]:
        tcs: list[ToolCallBlock] = []
        for am in self.assistant_messages:
            tcs.extend(am.tool_calls)
        return tcs

    @property
    def final_text(self) -> str:
        """Text from the last assistant message (no tool calls)."""
        for am in reversed(self.assistant_messages):
            if not am.has_tool_calls and am.text:
                return am.text
        return ""

    @property
    def start_time(self) -> int:
        return self.user_message.timestamp

    @property
    def end_time(self) -> int | None:
        if self.assistant_messages:
            return self.assistant_messages[-1].timestamp
        return None

    @property
    def elapsed_ms(self) -> int | None:
        if self.end_time:
            return self.end_time - self.start_time
        return None


# ---------------------------------------------------------------------------
# Versionable history: checkpoints with workspace tie-in
# ---------------------------------------------------------------------------

@dataclass
class SessionCheckpoint:
    """A named snapshot of session state + workspace (git) state."""
    id: str = field(default_factory=_new_id)
    label: str = ""
    timestamp: int = field(default_factory=_now_ms)
    message_index: int = 0       # position in session._messages at snapshot time
    message_count: int = 0
    parent_checkpoint_id: str | None = None  # for branching
    branch: str = "main"
    # workspace tie-in
    git_sha: str | None = None
    git_branch: str | None = None
    git_dirty: bool = False
    git_files_changed: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Session-level stats
# ---------------------------------------------------------------------------

@dataclass
class SessionStats:
    total_turns: int = 0
    total_messages: int = 0
    total_tool_calls: int = 0
    tool_calls_completed: int = 0
    tool_calls_errored: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_duration_ms: int = 0
    avg_tool_latency_ms: float = 0.0
