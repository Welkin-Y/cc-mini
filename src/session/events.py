"""Lightweight synchronous pub/sub event system for session observation.

Zero external dependencies. Callbacks are called synchronously in registration
order when events are emitted. Errors in callbacks are caught and logged to
stderr so one broken observer doesn't break the session.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any, Callable

# Known event types emitted by Session
EVENT_MESSAGE_ADDED = "message_added"
EVENT_TOOL_CALL_STARTED = "tool_call_started"
EVENT_TOOL_CALL_COMPLETED = "tool_call_completed"
EVENT_TURN_COMPLETED = "turn_completed"
EVENT_SESSION_CLEARED = "session_cleared"


class EventBus:
    """A simple synchronous event bus.

    Usage::

        bus = EventBus()
        bus.on("message_added", lambda session, msg: print(msg))
        bus.emit("message_added", session, message)
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[..., Any]]] = {}

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        """Register *callback* to be called when *event* is emitted."""
        self._subscribers.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Callable[..., Any]) -> None:
        """Remove a previously registered callback."""
        subs = self._subscribers.get(event, [])
        if callback in subs:
            subs.remove(callback)

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Call all subscribers for *event*, passing through positional and keyword args."""
        for cb in self._subscribers.get(event, ()):
            try:
                cb(*args, **kwargs)
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def clear(self) -> None:
        """Remove all subscribers."""
        self._subscribers.clear()
