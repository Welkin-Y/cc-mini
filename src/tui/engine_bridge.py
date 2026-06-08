"""Async bridge for the synchronous Engine.

Runs Engine.submit() in a thread pool executor and pipes events
back to the asyncio event loop via call_soon_threadsafe.

This avoids rewriting the entire LLMClient / tool execution layer
while still giving us a fully async TUI.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Callable, Optional

from core.engine import AbortedError, Engine
from core.permissions import PermissionChecker
from tui.display import ChatDisplay


# Sentinel to signal event stream completion
class _END:
    pass


_END_SENTINEL = _END()


# Event format (from engine.py):
#   ("text", str)
#   ("waiting",)
#   ("tool_call", name, input, activity, tool_use_id)
#   ("tool_executing", name, input, activity, tool_use_id)
#   ("tool_result", name, input, result, tool_use_id)
#   ("error", str)
#   ("usage", LLMUsage)

# Type for permission handler callback:
#   async def handler(tool_name: str, tool_input: dict) -> str
#   returns "allow" | "deny" | "always"
PermissionHandler = Callable[..., object]  # async (str, dict) -> str


async def submit_async(
    engine: Engine,
    user_input,
    display: ChatDisplay,
    permissions: Optional[PermissionChecker] = None,
    permission_handler: Optional[PermissionHandler] = None,
    refresh_callback=None,
    full_redraw_fn=None,
) -> None:
    """Run engine.submit() in a thread, streaming events to the display.

    If *permission_handler* is provided, it is called (awaited) for each
    permission prompt from the engine.  The handler should return "allow",
    "deny", or "always".  If omitted, prompts default to "deny".

    If *refresh_callback* is provided, it is called (synchronously) after
    every event that changes the display, so the TUI can repaint immediately.

    If *full_redraw_fn* is provided, it is called after AskUserQuestion
    completes (its mini PT app corrupts the terminal; a full redraw repairs it).
    """
    loop = asyncio.get_running_loop()
    _refresh = refresh_callback or (lambda: None)
    _full_redraw = full_redraw_fn or _refresh
    event_queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    # -- Setup bridged permission prompts --
    _bridge = _PromptBridge(loop)

    def _run_engine() -> None:
        """Run in thread pool: iterate engine.submit(), push events to queue."""
        try:
            for event in engine.submit(user_input):
                _enqueue_safe(loop, event_queue, event)
            _enqueue_safe(loop, event_queue, _END_SENTINEL)
        except AbortedError:
            _enqueue_safe(loop, event_queue, _END_SENTINEL)
        except Exception as exc:
            _enqueue_safe(loop, event_queue, exc)

    # Wire permissions to bridge (so engine thread gets async-safe prompts)
    _original_prompt_provider = None
    if permissions is not None:
        _original_prompt_provider = permissions._prompt_provider
        permissions._prompt_provider = _bridge.handle_prompt

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_run_engine)

            pending_tools: dict[str, str] = {}  # tool_use_id → display_key
            current_asst_id: Optional[str] = None
            streaming = False

            import time as _time
            _done = False
            _last_render = 0.0
            _TICK = 0.03  # ~30fps — render at most this often

            while not _done:
                # -- Handle pending permission requests --
                pending_reqs = _bridge.drain_pending()
                for req_info in pending_reqs:
                    tool, inputs, ev, result_container = req_info
                    if permission_handler is not None:
                        tool_name = getattr(tool, 'name', str(tool))
                        response = await permission_handler(tool_name, inputs)
                    else:
                        response = "deny"
                    result_container.append(response)
                    if response == "always" and permissions is not None:
                        permissions._always_allow.add(getattr(tool, 'name', str(tool)))
                    ev.set()

                # -- Wait for next event (short timeout to keep render cadence) --
                try:
                    first = await asyncio.wait_for(event_queue.get(), timeout=_TICK)
                except asyncio.TimeoutError:
                    # No event this tick — render anyway and keep looping
                    now = _time.monotonic()
                    if now - _last_render >= _TICK:
                        _refresh()
                        _last_render = now
                    continue

                # -- Process this event + drain any queued behind it --
                batch = [first]
                while True:
                    try:
                        batch.append(event_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                for event in batch:
                    if event is _END_SENTINEL:
                        _done = True
                        break
                    if isinstance(event, Exception):
                        display.add_system_message(f"Error: {event}")
                        _done = True
                        break

                    kind = event[0]

                    if kind == "text":
                        if not streaming:
                            current_asst_id = display.start_assistant_stream()
                            streaming = True
                        display.append_token(current_asst_id, event[1])

                    elif kind == "waiting":
                        streaming = False
                        display.set_status("Thinking…")

                    elif kind == "tool_call":
                        _, tool_name, tool_input, activity = event[:4]
                        tool_use_id = event[4] if len(event) > 4 else _make_fallback_id(tool_name, tool_input)
                        key = display.add_tool_call(tool_name, tool_input, activity)
                        pending_tools[tool_use_id] = key
                        display.set_status(f"Running {tool_name}…")

                    elif kind == "tool_executing":
                        _, tool_name, tool_input, activity = event[:4]
                        if tool_name == "AskUserQuestion":
                            continue
                        tool_use_id = event[4] if len(event) > 4 else _make_fallback_id(tool_name, tool_input)
                        if tool_use_id in pending_tools:
                            display.update_tool_running(pending_tools[tool_use_id])

                    elif kind == "tool_result":
                        _, tool_name, tool_input, result = event[:4]
                        tool_use_id = event[4] if len(event) > 4 else _make_fallback_id(tool_name, tool_input)

                        # AskUserQuestion already rendered via its own PT app —
                        # don't echo verbose result in output area.
                        if tool_name == "AskUserQuestion":
                            pending_tools.pop(tool_use_id, None)
                            display.add_system_message(" Answered")
                            _full_redraw()
                            continue

                        if tool_use_id in pending_tools:
                            key = pending_tools.pop(tool_use_id)
                            display.update_tool_done(
                                key,
                                content=result.content if hasattr(result, 'content') else str(result),
                                is_error=result.is_error if hasattr(result, 'is_error') else False,
                            )

                    elif kind == "error":
                        display.add_system_message(f"[red]{event[1]}[/red]")

                    elif kind == "usage":
                        pass  # handled by cost_tracker internally

                # -- Render on tick cadence (not per-event) --
                now = _time.monotonic()
                if now - _last_render >= _TICK:
                    _refresh()
                    _last_render = now

            # Final render to show complete state
            _refresh()
            streaming = False

    finally:
        if permissions is not None:
            permissions._prompt_provider = _original_prompt_provider


def _enqueue_safe(loop, queue, item):
    """Put an item on the queue from a non-asyncio thread, ignoring QueueFull.

    The exception must be caught INSIDE the scheduled callback, because
    ``call_soon_threadsafe`` schedules the call and returns immediately —
    the ``QueueFull`` is raised later on the event loop thread.
    """
    def _put():
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass  # queue backed up; drop event rather than crashing
    loop.call_soon_threadsafe(_put)


def _make_fallback_id(tool_name: str, tool_input: dict) -> str:
    """Fallback tool_use_id for events that don't carry one (pre-5-element format)."""
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))[:50]
        return f"{tool_name}:{cmd}"
    if tool_name in ("Read", "Edit", "Write"):
        fp = str(tool_input.get("file_path", ""))
        return f"{tool_name}:{fp}"
    return f"{tool_name}:{hash(str(tool_input))}"


class _PromptBridge:
    """Bridges synchronous permission prompts from engine thread to async TUI.

    The engine thread calls handle_prompt() which enqueues the request
    and blocks on a threading.Event.  The main async loop periodically
    calls drain_pending() to pick up requests, shows the permission
    prompt in the TUI, and responds via the Event.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._lock = threading.Lock()
        self._queue: list[tuple] = []  # [(tool, inputs, event, result_container)]

    def handle_prompt(self, tool, inputs) -> str:
        """Called from engine thread. Blocks until TUI collects response."""
        result: list[str] = []
        event = threading.Event()
        with self._lock:
            self._queue.append((tool, inputs, event, result))
        # Wait for main thread to respond (5 min timeout)
        if not event.wait(timeout=300):
            return "deny"
        return result[0] if result else "deny"

    def drain_pending(self) -> list[tuple]:
        """Called from main thread. Returns pending requests (non-blocking)."""
        with self._lock:
            if not self._queue:
                return []
            batch = list(self._queue)
            self._queue.clear()
        return batch
