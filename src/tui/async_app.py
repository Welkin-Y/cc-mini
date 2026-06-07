"""Async REPL with non-blocking input and message queueing.

Core design:
- A single persistent prompt_toolkit Application runs via asyncio.
- The engine runs in a background thread so the input area stays live.
- User messages queue while the engine is busy; they process sequentially.
- Output renders above the input area, auto-scrolling to show new content.
"""
from __future__ import annotations

import asyncio
import queue
import sys
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from typing import TYPE_CHECKING

from prompt_toolkit.application import Application as PTApp
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText, ANSI, to_formatted_text
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window, FloatContainer, Float
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from rich.console import Console as RichConsole
from rich.markdown import Markdown as RichMarkdown

from core.engine import AbortedError, Engine
from core.permissions import PermissionChecker
from tui.output_buffer import OutputBuffer
from tui.rendering import (
    StreamingMarkdownBuffer,
    tool_preview,
    collapsed_tool_summary,
    render_todo_list,
)
from tui.prompt import slash_completer
from tui.query import run_query_threadsafe
from tui.input_parser import parse_input

if TYPE_CHECKING:
    from features.todo import TodoManager
    from features.cost_tracker import CostTracker
    from features.coordinator import WorkerManager
    from features.plan import PlanModeManager
    from features.memory import MemoryDir

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class AsyncREPL:
    """Async REPL with persistent prompt_toolkit Application.

    The input area stays live while the model generates. Messages sent while
    the engine is busy are queued and processed sequentially.
    """

    def __init__(
        self,
        engine: Engine,
        permissions: PermissionChecker,
        *,
        file_history: FileHistory | None = None,
        todo_manager: TodoManager | None = None,
        cost_tracker: CostTracker | None = None,
        worker_manager: WorkerManager | None = None,
        plan_manager: PlanModeManager | None = None,
        app_config=None,
        memory_dir=None,
        session_store=None,
        compact_service=None,
        sandbox_mgr=None,
        animator=None,
        # Callbacks for external features
        on_submit: callable | None = None,
        on_turn_complete: callable | None = None,
        on_drain_workers: callable | None = None,
    ):
        self._engine = engine
        self._permissions = permissions
        self._todo_manager = todo_manager
        self._cost_tracker = cost_tracker
        self._worker_manager = worker_manager
        self._plan_manager = plan_manager
        self._app_config = app_config
        self._memory_dir = memory_dir
        self._session_store = session_store
        self._compact_service = compact_service
        self._sandbox_mgr = sandbox_mgr
        self._animator = animator
        self._on_submit = on_submit
        self._on_turn_complete = on_turn_complete
        self._on_drain_workers = on_drain_workers
        self._file_history = file_history

        # Output buffer (shared between engine thread and UI)
        self._output = OutputBuffer()

        # Message queue
        self._message_queue: asyncio.Queue[str] = asyncio.Queue()
        self._engine_busy = False
        self._queued_count = 0  # how many messages waiting

        # Status / spinner state
        self._status_text = ""
        self._status_style = "dim"
        self._spinner_idx = 0

        # Terminal mode toggle
        self._terminal_mode = False

        # Abort state
        self._aborting = False

        # Thread executor for engine
        self._executor = ThreadPoolExecutor(max_workers=2)

        # Last Ctrl+C time for double-press exit
        self._last_ctrlc_time = 0.0
        self._DOUBLE_PRESS_TIMEOUT_MS = 0.8

        # Build the persistent app
        self._app = self._build_app()
        self._output_window: Window | None = None  # set during build

    # -- public API -----------------------------------------------------------

    def queue_message(self, text: str | list) -> None:
        """Schedule a message for processing (safe to call from any thread).

        Use this for programmatic submission, e.g. pending queries from
        slash commands, or worker notifications.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._message_queue.put(text))
            else:
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self._message_queue.put(text))
                )
        except RuntimeError:
            pass

    def run(self) -> None:
        """Entry point: run the async REPL (blocks until exit)."""
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            pass
        finally:
            self._executor.shutdown(wait=False)

    async def _run(self) -> None:
        """Set up async tasks and run the Application."""
        # Start the message consumer
        consumer_task = asyncio.create_task(self._message_consumer())

        # Start the app (this blocks until app.exit() is called)
        try:
            await self._app.run_async()
        finally:
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass

    # -- Application construction --------------------------------------------

    def _build_app(self) -> PTApp:
        """Build the persistent prompt_toolkit Application."""
        kb = self._build_keybindings()
        layout = self._build_layout()

        # Refresh interval: drives spinner animation + animator ticks
        refresh = 0.1  # 100ms (10fps for spinner)

        app = PTApp(
            layout=layout,
            key_bindings=kb,
            full_screen=False,
            refresh_interval=refresh,
            include_default_pygments_style=False,
        )

        # Wire up animator invalidate if available
        if self._animator:
            self._animator.set_invalidate(app.invalidate)

        return app

    def _build_layout(self) -> Layout:
        """Build the app layout: output area + status + input area."""
        # --- Output area ---
        output_window = Window(
            content=self._output.control,
            wrap_lines=True,
            allow_scroll_beyond_bottom=False,
        )
        self._output_window = output_window

        # --- Status bar ---
        status_control = FormattedTextControl(self._get_status_text)
        status_window = Window(
            content=status_control,
            height=1,
            dont_extend_height=True,
        )

        # --- Input buffer ---
        self._input_buf = Buffer(
            history=self._file_history,
            completer=slash_completer,
            complete_while_typing=False,
        )

        def _on_text_changed(_buf):
            if _buf.text.lstrip().startswith('/'):
                # Trigger completion on next event loop tick
                try:
                    loop = asyncio.get_event_loop()
                    loop.call_soon(lambda: _buf.start_completion(select_first=False))
                except RuntimeError:
                    pass

        self._input_buf.on_text_changed += _on_text_changed

        input_control = BufferControl(buffer=self._input_buf)

        # Line prefix: '> ' for first line, '  ' for wrapped lines
        def _line_prefix(lineno, wrap_count):
            if lineno == 0 and wrap_count == 0:
                if self._terminal_mode:
                    return [('bold fg:ansiyellow', '$ ')]
                return [('bold fg:ansicyan', '> ')]
            return [('', '  ')]

        # Top border
        def _top_border():
            try:
                w = os.get_terminal_size().columns
            except OSError:
                w = 80
            fill = "─" * max(0, w - 1)
            if self._terminal_mode:
                return [('bold fg:ansiyellow', f'╭{fill}')]
            return [('bold fg:ansicyan', f'╭{fill}')]

        # Bottom border
        def _bottom_border():
            try:
                w = os.get_terminal_size().columns
            except OSError:
                w = 80
            if self._terminal_mode:
                hints = "─ TERMINAL MODE · ! to exit · Enter run "
                fill = "─" * max(0, w - 1 - len(hints))
                parts: list[tuple[str, str]] = [('fg:ansiyellow', f'╰{hints}{fill}')]
            else:
                hints = "─ Enter send · Alt+Enter newline · ! shell · / commands "
                fill = "─" * max(0, w - 1 - len(hints))
                parts: list[tuple[str, str]] = [('fg:ansicyan', f'╰{hints}{fill}')]
            return parts

        body = HSplit([
            output_window,
            status_window,
            Window(FormattedTextControl(_top_border), height=1, dont_extend_height=True),
            Window(
                input_control,
                get_line_prefix=_line_prefix,
                height=Dimension(min=1),
                dont_extend_height=True,
                wrap_lines=True,
            ),
            Window(FormattedTextControl(_bottom_border), dont_extend_height=True),
        ])

        root = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True, ycursor=True,
                    content=CompletionsMenu(max_height=8, scroll_offset=1),
                ),
            ],
        )

        return Layout(root, focused_element=self._input_buf)

    # -- key bindings ---------------------------------------------------------

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add('enter')
        def _(event):
            buf = self._input_buf
            if buf.text.endswith('\\'):
                buf.delete_before_cursor(1)
                buf.insert_text('\n')
            else:
                text = buf.text
                buf.reset()
                if text.strip():
                    if self._terminal_mode:
                        self._handle_shell(text)
                    elif text.startswith('!') and len(text) > 1:
                        self._handle_shell(text[1:].lstrip())
                    else:
                        # Schedule submission on the event loop
                        asyncio.ensure_future(self._submit_message(text))

        @kb.add('escape', 'enter')
        def _(event):
            self._input_buf.insert_text('\n')

        @kb.add('escape')
        def _(event):
            # Abort the current turn
            self._engine.abort()
            self._aborting = True
            self._output.append_line(
                "⏹ Turn cancelled (Esc)", style="fg:ansiyellow"
            )

        @kb.add('c-c')
        def _(event):
            # Single Ctrl+C: abort current turn
            # Double Ctrl+C: exit
            now = time.monotonic()
            if now - self._last_ctrlc_time <= self._DOUBLE_PRESS_TIMEOUT_MS:
                event.app.exit()
                return
            self._last_ctrlc_time = now
            self._engine.abort()
            self._aborting = True
            self._output.append_line(
                "⏹ Turn cancelled (Ctrl+C)", style="fg:ansiyellow"
            )

        @kb.add('c-d')
        def _(event):
            if not self._input_buf.text:
                event.app.exit()

        @kb.add('!')
        def _(event):
            if not self._input_buf.text:
                self._terminal_mode = not self._terminal_mode
                event.app.invalidate()
            else:
                self._input_buf.insert_text('!')

        return kb

    # -- message handling ----------------------------------------------------

    async def _submit_message(self, text: str) -> None:
        """Called from the UI thread when user presses Enter with text.

        Checks for special inputs (slash commands, companion address, exit, etc.)
        via the on_submit callback. If the callback returns True, the message
        was handled and is not queued to the engine.
        """
        # Check for exit
        if text.lower() in ("exit", "quit", "/exit", "/quit"):
            self._app.exit()
            return

        # Shell commands execute inline
        if self._terminal_mode:
            self._handle_shell(text)
            return
        if text.startswith("!") and len(text) > 1:
            self._handle_shell(text[1:].lstrip())
            return

        # Let external handler process slash commands, companion, etc.
        if self._on_submit:
            try:
                handled = self._on_submit(text)
                if handled:
                    return
            except Exception:
                pass

        # Normal message: queue for engine
        parsed = parse_input(text)
        self._message_queue.put_nowait(parsed)

    async def _message_consumer(self) -> None:
        """Background coroutine: process messages from the queue sequentially."""
        while True:
            user_input = await self._message_queue.get()
            self._queued_count = self._message_queue.qsize()

            if self._queued_count > 0:
                self._set_status(
                    f"⏳ {self._queued_count} message{'s' if self._queued_count > 1 else ''} queued",
                    "dim",
                )

            self._engine_busy = True
            try:
                await self._process_turn(user_input)
            finally:
                self._engine_busy = False
                self._queued_count = self._message_queue.qsize()
                if self._queued_count == 0:
                    self._set_status("", "")

                # Fire on_turn_complete callback
                if self._on_turn_complete:
                    try:
                        self._on_turn_complete()
                    except Exception:
                        pass

    async def _process_turn(self, user_input: str | list) -> None:
        """Run a single turn: execute engine in thread, stream UI updates."""
        ui_queue: queue.Queue = queue.Queue()
        loop = asyncio.get_event_loop()

        def _run_engine():
            run_query_threadsafe(
                self._engine,
                user_input,
                ui_queue,
                permissions=self._permissions,
                todo_manager=self._todo_manager,
            )

        future = loop.run_in_executor(self._executor, _run_engine)

        # Streaming state
        md_stream = StreamingMarkdownBuffer()
        streaming = False           # currently receiving text
        first_text = True
        pending_tools: dict[str, tuple[str, str]] = {}  # key → (name, line)
        aborted = False

        def _apply_stable():
            """Pull stable chunks from md_stream and append to output."""
            stable = md_stream.pull_stable()
            if stable:
                # stable is an ANSI string; convert to FormattedText
                self._output.append_formatted(
                    to_formatted_text(ANSI(stable))
                )

        def _apply_unstable():
            """Show the unstable trailing part (last line only, for perf)."""
            unstable = md_stream.get_unstable()
            if unstable:
                # We'd need a third buffer for the unstable part.
                # For now, stable chunks handle incremental display.
                pass

        self._set_status("Thinking…", "dim")

        # Poll loop: check ui_queue for events while engine runs
        while not future.done() or not ui_queue.empty():
            try:
                event = ui_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)  # 20fps polling
                continue

            kind = event[0]

            if kind == "text":
                if first_text:
                    self._set_status("", "")
                    streaming = True
                    first_text = False
                md_stream.feed(event[1])
                _apply_stable()

            elif kind == "waiting":
                # Text stream done, flush remaining
                md_stream.flush()
                _apply_stable()
                streaming = False
                self._set_status("Preparing tool call…", "dim")

            elif kind == "tool_call":
                _, tool_name, tool_input, activity = event
                preview = tool_preview(tool_name, tool_input)
                key = f"{tool_name}({preview})"
                pending_tools[key] = (tool_name, f"↳ {key}")
                self._set_status(f"↳ {key}", "dim")

            elif kind == "tool_executing":
                _, tool_name, tool_input, activity = event
                n = len(pending_tools)
                if n > 1:
                    names = [tn for tn, _ in pending_tools.values()]
                    self._set_status(
                        collapsed_tool_summary(names), "dim"
                    )
                else:
                    _, line = next(iter(pending_tools.values()), ("", f"↳ {tool_name}"))
                    activity_text = activity or f"Running {tool_name}…"
                    self._set_status(f"{line} … {activity_text}", "dim")

            elif kind == "tool_result":
                _, tool_name, tool_input, result = event
                preview = tool_preview(tool_name, tool_input)
                key = f"{tool_name}({preview})"
                pending_tools.pop(key, None)

                if result.is_error:
                    self._output.append_line(
                        f"↳ {key} ✗", style="fg:ansired"
                    )
                    # Show error snippet
                    err = result.content[:200]
                    self._output.append_line(f"  {err}", style="fg:ansired")
                else:
                    self._output.append_line(
                        f"↳ {key} ✓", style="fg:ansigreen"
                    )

                if pending_tools:
                    names = [tn for tn, _ in pending_tools.values()]
                    self._set_status(
                        collapsed_tool_summary(names), "dim"
                    )
                else:
                    streaming = False
                    # Show in-progress todo item in status if available
                    status_text = "Thinking…"
                    if self._todo_manager:
                        wip = self._todo_manager.in_progress_item()
                        if wip:
                            label = wip.subject
                            if len(label) > 60:
                                label = label[:57] + "…"
                            status_text = label
                    self._set_status(status_text, "dim")
                    first_text = True

            elif kind == "error":
                self._output.append_line(f"\n{event[1]}", style="bold fg:ansired")

            elif kind == "aborted":
                aborted = True
                self._output.append_line(
                    "⏹ Turn cancelled", style="fg:ansiyellow"
                )
                break

            elif kind == "done":
                break

            elif kind == "usage":
                # Silently tracked by cost_tracker via engine
                pass

        # Finalize
        md_stream.flush()
        _apply_stable()

        if not aborted:
            self._set_status("", "")

        # Drain worker notifications after turn
        if self._on_drain_workers:
            try:
                self._on_drain_workers()
            except Exception:
                pass

    # -- shell handling -------------------------------------------------------

    def _handle_shell(self, cmd: str) -> None:
        """Execute a shell command and show output."""
        import subprocess
        self._output.append_line(f"$ {cmd}", style="dim")
        try:
            result = subprocess.run(
                cmd, shell=True, text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=30,
            )
            if result.stdout:
                for line in result.stdout.splitlines():
                    self._output.append_line(line)
            if result.returncode != 0:
                self._output.append_line(
                    f"[exit {result.returncode}]", style="fg:ansired"
                )
        except subprocess.TimeoutExpired:
            self._output.append_line("Command timed out", style="fg:ansired")
        except Exception as exc:
            self._output.append_line(f"Error: {exc}", style="fg:ansired")

    # -- status bar -----------------------------------------------------------

    def _set_status(self, text: str, style: str = "dim") -> None:
        """Set the status bar text (thread-safe)."""
        self._status_text = text
        self._status_style = style

    def _get_status_text(self) -> FormattedText:
        """Return the current status bar content."""
        if not self._status_text:
            if self._queued_count > 0:
                s = "s" if self._queued_count > 1 else ""
                return FormattedText([
                    ("dim", f"⏳ {self._queued_count} message{s} queued"),
                ])
            return FormattedText([("", "")])

        # Show spinner when thinking
        if self._status_text == "Thinking…":
            self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
            frame = _SPINNER_FRAMES[self._spinner_idx]
            return FormattedText([
                ("dim", f"{frame} {self._status_text}"),
            ])
        elif self._status_text.startswith("↳"):
            self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
            frame = _SPINNER_FRAMES[self._spinner_idx]
            return FormattedText([
                ("dim", f"{frame} {self._status_text}"),
            ])

        return FormattedText([
            (self._status_style, self._status_text),
        ])

