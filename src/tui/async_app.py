"""Async TUI application — persistent prompt_toolkit UI with async engine bridge.

Pattern: follows async_ui's single persistent Application with
Rich→ANSI→FormattedText rendering for the chat display, a TextArea
for input, and an async task for processing engine events.

This replaces the REPL loop in app.py + the run_query() function + the
StreamingMarkdown/SpinnerManager rendering with a unified async approach.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from tui.display import ChatDisplay

if TYPE_CHECKING:
    from core.engine import Engine
    from core.permissions import PermissionChecker
    from features.cost_tracker import CostTracker


def _fmt_to_plain(ft) -> str:
    parts = []
    for item in ft:
        parts.append(item[1] if isinstance(item, tuple) else str(item))
    return "".join(parts)


class AsyncApp:
    """Async TUI application with a Claude-like chat interface.

    Supports async permission prompts (y/n/a) via PT key bindings
    when the engine requests tool confirmation.

    Usage:
        app = AsyncApp(engine=engine, permissions=permissions, ...)
        await app.run()
    """

    def __init__(
        self,
        engine: Engine,
        permissions: Optional[PermissionChecker] = None,
        cost_tracker: Optional[CostTracker] = None,
        memory_dir=None,
        session_store=None,
        compact_service=None,
        app_config=None,
        plan_manager=None,
        worker_manager=None,
        run_dream_fn=None,
        sandbox_mgr=None,
    ):
        self.engine = engine
        self.permissions = permissions
        self.cost_tracker = cost_tracker
        self.memory_dir = memory_dir
        self.session_store = session_store
        self.compact_service = compact_service
        self.app_config = app_config
        self.plan_manager = plan_manager
        self.worker_manager = worker_manager
        self._run_dream_fn = run_dream_fn
        self._sandbox_mgr = sandbox_mgr

        # -- Display --
        self.display = ChatDisplay()

        # -- Processing state --
        self._is_processing = False
        self._abort_requested = False
        self._terminal_mode = False
        self._current_task: Optional[asyncio.Task] = None
        self._thinking_start: Optional[float] = None

        # -- Permission mode (Shift+Tab cycles through) --
        self._perm_mode = 0  # 0=normal, 1=auto-approve, 2=plan

        # -- Dismissable command output --
        self._dismissable_count: int = 0

        # -- Message stacking --
        self._pending_stack: list[str] = []

        # -- Permission prompt state --
        # When the engine needs permission, a Future is stored here.
        # Key bindings y/n/a resolve it, unblocking the engine thread.
        self._permission_future: Optional[asyncio.Future] = None
        self._permission_tool_name: str = ""

        # -- Question panel state (inline above input) --
        self._question_active = False
        self._question_cursor = 0
        self._question_labels: list[str] = []
        self._question_text = ""
        self._question_other_text = ""
        self._question_future: Optional[asyncio.Future] = None

        # -- Model picker overlay state --
        self._overlay_active = False
        self._overlay_cursor = 0
        self._overlay_options: list[tuple[str, str, str]] = []  # (alias, label, desc)
        self._overlay_current_alias = ""
        self._overlay_effort_idx = 2  # default: high
        self._overlay_scroll = 0
        self._overlay_future: Optional[asyncio.Future] = None

        # -- Build layout --
        self._build_ui()

    # ---- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        """Build the persistent prompt_toolkit layout."""
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.buffer import Buffer
        from tui.formatted_buffer import FormattedBuffer
        self._chat_buffer = Buffer(multiline=True, read_only=False)
        self._chat_control = FormattedBuffer(buffer=self._chat_buffer)
        self._chat_window = Window(
            content=self._chat_control,
            wrap_lines=True,
            allow_scroll_beyond_bottom=False,
        )
        self._following = True  # auto-scroll to bottom on new content

        from pathlib import Path
        from prompt_toolkit.history import FileHistory
        from tui.prompt import slash_completer
        _history_file = Path.home() / ".config" / "cc-mini" / "history"
        _history_file.parent.mkdir(parents=True, exist_ok=True)
        self._input = TextArea(
            height=1,
            prompt="> ",
            multiline=False,
            accept_handler=self._on_send,
            history=FileHistory(str(_history_file)),
            completer=slash_completer,
            complete_while_typing=True,
            style="class:input-field",
        )

        # (panel_window created below after ConditionalContainer import)

        # Header line between separator and input: "cc-mini provider:model"
        self._header_control = FormattedTextControl(
            text=[("bold fg:ansicyan", "cc-mini")],
            focusable=False,
        )

        # Status line
        self._status_control = FormattedTextControl(
            text=self.display.render_status_line(),
            focusable=False,
        )
        self._status_window = Window(
            content=self._status_control,
            height=1,
            style="class:status-line",
        )

        # Layout — FloatContainer so overlays can appear above content
        from prompt_toolkit.layout.menus import CompletionsMenu

        # Model picker overlay (hidden by default)
        from prompt_toolkit.layout.containers import ConditionalContainer, FloatContainer, Float

        # Question/ask panel
        self._panel_control = FormattedTextControl(text=[], focusable=False)
        self._panel_window = ConditionalContainer(
            content=Window(content=self._panel_control, dont_extend_height=True),
            filter=Condition(lambda: self._question_active),
        )

        # Stacked messages above header (while processing)
        self._pending_control = FormattedTextControl(text=[], focusable=False)
        self._pending_window = ConditionalContainer(
            content=Window(content=self._pending_control,
                           dont_extend_height=True),
            filter=Condition(lambda: len(self._pending_stack) > 0),
        )

        self._overlay_control = FormattedTextControl(
            text=self._render_overlay,
            focusable=True,
        )
        self._overlay_window = ConditionalContainer(
            content=Window(
                content=self._overlay_control,
                width=55,
                height=14,
                style="class:dialog",
            ),
            filter=Condition(lambda: self._overlay_active),
        )

        _body = HSplit([
            self._chat_window,
            Window(height=1, char="━", style="class:separator"),
            self._pending_window,
            self._panel_window,
            Window(content=self._header_control, height=1, dont_extend_height=True),
            self._input,
            self._status_window,
        ])

        self._layout = Layout(
            FloatContainer(
                content=_body,
                floats=[
                    Float(
                        xcursor=True, ycursor=True,
                        content=CompletionsMenu(max_height=8, scroll_offset=1),
                    ),
                    Float(
                        content=self._overlay_window,
                    ),
                ],
            )
        )
        # Key bindings
        self._kb = KeyBindings()

        # Double-press Ctrl+C / Ctrl+D tracking (matches Claude Code useDoublePress)
        import time as _time
        self._last_ctrlc_time: float = 0.0
        _DOUBLE_PRESS_MS = 0.8

        @self._kb.add("c-c")
        def _(event):
            # Output or input area has selection → copy to system clipboard
            out_sel = getattr(self._chat_buffer, 'selection_state', None)
            in_sel = getattr(self._input.buffer, 'selection_state', None)
            sel = out_sel or in_sel
            buf = self._chat_buffer if out_sel else self._input.buffer
            if sel and isinstance(sel, tuple) and len(sel) == 2:
                try:
                    s, e = min(sel), max(sel)
                    text = buf.text[s:e]
                    # PT clipboard (works if xclip/xsel/pyperclip available)
                    from prompt_toolkit.clipboard import ClipboardData
                    event.app.clipboard.set_data(ClipboardData(text))
                    # OSC 52 via /dev/tty (bypasses PT output layer entirely)
                    import base64, os as _os
                    b64 = base64.b64encode(text.encode()).decode()
                    try:
                        with open("/dev/tty", "w") as _tty:
                            _tty.write(f"\x1b]52;c;{b64}\x07")
                            _tty.flush()
                    except Exception:
                        pass
                    buf.selection_state = None
                    self.display.set_status("Copied!")
                    self._refresh()
                except Exception:
                    pass
                return
            # Ctrl+C with text: clear input, never exit
            if self._input.buffer.text:
                self._input.buffer.text = ""
                return
            # Ctrl+C on empty: double-press within 800ms to exit
            now = _time.monotonic()
            if now - self._last_ctrlc_time <= _DOUBLE_PRESS_MS:
                self._last_ctrlc_time = 0.0
                if self._permission_future is not None and not self._permission_future.done():
                    self._permission_future.set_result("deny")
                event.app.exit()
            else:
                self._last_ctrlc_time = now
                self.display.set_status("Press Ctrl+C again to exit")
                self._refresh()

        @self._kb.add("c-d", filter=Condition(lambda: not self._input.buffer.text))
        def _(event):
            # Ctrl+D on empty: double-press within 800ms to exit
            now = _time.monotonic()
            if now - self._last_ctrlc_time <= _DOUBLE_PRESS_MS:
                self._last_ctrlc_time = 0.0
                if self._permission_future is not None and not self._permission_future.done():
                    self._permission_future.set_result("deny")
                event.app.exit()
            else:
                self._last_ctrlc_time = now
                self.display.set_status("Press Ctrl+C/D again to exit")
                self._refresh()
        # When buffer has text, c-d falls through to TextArea's default delete-forward

        @self._kb.add("escape")
        def _(event):
            """Esc: dismiss command output > cancel question > deny permission > abort turn."""
            # 0. Cancel question panel
            if self._question_active:
                if self._question_future and not self._question_future.done():
                    self._question_future.set_result(None)
                return
            # 1. Dismiss command output if present
            if self._dismissable_count > 0 and not self._is_processing:
                self._dismiss_command_output()
                return
            # 2. Deny pending permission
            if self._permission_future is not None and not self._permission_future.done():
                self._permission_future.set_result("deny")
                return
            # 3. Abort current turn
            if self._is_processing:
                self._abort_requested = True
                self.engine.abort()

        @self._kb.add("s-tab")
        def _(event):
            """Cycle permission mode: normal → auto-approve → plan → normal."""
            MODES = ["normal", "auto-approve", "plan"]
            PT_COLORS = {
                "normal": "fg:ansigreen", "auto-approve": "fg:ansiyellow",
                "plan": "fg:ansicyan",
            }
            self._perm_mode = (self._perm_mode + 1) % 3
            mode = MODES[self._perm_mode]
            if mode == "auto-approve" and self.permissions:
                self.permissions._auto_approve = True
            elif mode == "plan":
                if self.permissions:
                    self.permissions._auto_approve = False
                if self.plan_manager and not self.plan_manager.is_active:
                    self.plan_manager.enter()
            else:
                if self.permissions:
                    self.permissions._auto_approve = False
                if self.plan_manager and self.plan_manager.is_active:
                    self.plan_manager.exit()
            color = PT_COLORS[mode]
            # Update header with new mode color
            provider = getattr(self.app_config, 'provider', '?') if self.app_config else '?'
            model = self.engine.get_model()
            self._header_control.text = [
                ("bold fg:ansicyan", f" cc-mini "),
                ("", f"{provider}:{model}  "),
                (f"{color} bold", f"({mode})"),
            ]
            self.display.set_status("")
            self._refresh()

        @self._kb.add("!")
        def _(event):
            """Toggle terminal mode when input is empty, insert ! otherwise."""
            if self._permission_future is not None:
                return  # ignore during permission prompt
            if not self._input.buffer.text:
                self._terminal_mode = not self._terminal_mode
                self._input.prompt = "$ " if self._terminal_mode else "> "
                event.app.invalidate()
            else:
                self._input.buffer.insert_text("!")

        # Keyboard scrolling for chat area
        # PageUp/PageDown work in plain terminal; Ctrl+Up/Ctrl+Down work in tmux
        @self._kb.add("up")
        def _(event):
            # If pending messages exist and no processing, pop last for editing
            if self._pending_stack and not self._is_processing and not self._input.buffer.text:
                msg = self._pending_stack.pop()
                if self._pending_stack:
                    lines = []
                    for m in self._pending_stack:
                        lines.append(("class:pending", f" {m[:60]}\n"))
                    self._pending_control.text = lines
                else:
                    self._pending_control.text = []
                self._input.buffer.text = msg
                self._refresh()
                return
            # Normal: scroll output up
            self._following = False
            cur = self._chat_window.vertical_scroll or 0
            self._chat_window.vertical_scroll = max(0, cur - 5)

        @self._kb.add("pageup")
        @self._kb.add("c-up")
        def _(event):
            self._following = False
            cur = self._chat_window.vertical_scroll or 0
            self._chat_window.vertical_scroll = max(0, cur - 5)

        @self._kb.add("pagedown")
        @self._kb.add("c-down")
        def _(event):
            self._following = False
            cur = self._chat_window.vertical_scroll or 0
            self._chat_window.vertical_scroll = cur + 5

        @self._kb.add("end")
        @self._kb.add("c-end")
        def _(event):
            self._following = True
            self._chat_window.vertical_scroll = 0

        # Permission prompt keys (y/n/a) — only fire when a prompt is pending
        _perm_active = Condition(lambda: self._permission_future is not None)

        @self._kb.add("y", filter=_perm_active)
        @self._kb.add("Y", filter=_perm_active)
        def _(event):
            self._resolve_permission("allow")

        @self._kb.add("n", filter=_perm_active)
        @self._kb.add("N", filter=_perm_active)
        def _(event):
            self._resolve_permission("deny")

        @self._kb.add("a", filter=_perm_active)
        @self._kb.add("A", filter=_perm_active)
        def _(event):
            self._resolve_permission("always")

        # Model picker overlay keys — only fire when overlay is active
        _overlay_active = Condition(lambda: self._overlay_active)

        @self._kb.add("up", filter=_overlay_active)
        def _(event):
            if self._overlay_options:
                self._overlay_cursor = (self._overlay_cursor - 1) % len(self._overlay_options)
                self._overlay_control.text = self._render_overlay()
                self._app.invalidate()

        @self._kb.add("down", filter=_overlay_active)
        def _(event):
            if self._overlay_options:
                self._overlay_cursor = (self._overlay_cursor + 1) % len(self._overlay_options)
                self._overlay_control.text = self._render_overlay()
                self._app.invalidate()

        @self._kb.add("left", filter=_overlay_active)
        def _(event):
            self._overlay_effort_idx = (self._overlay_effort_idx - 1) % 3
            self._overlay_control.text = self._render_overlay()
            self._app.invalidate()

        @self._kb.add("right", filter=_overlay_active)
        def _(event):
            self._overlay_effort_idx = (self._overlay_effort_idx + 1) % 3
            self._overlay_control.text = self._render_overlay()
            self._app.invalidate()

        @self._kb.add("enter", filter=_overlay_active)
        def _(event):
            if (self._overlay_future and not self._overlay_future.done()
                    and self._overlay_options):
                self._overlay_future.set_result(
                    self._overlay_options[self._overlay_cursor][0]
                )

        @self._kb.add("escape", filter=_overlay_active)
        def _(event):
            if self._overlay_future and not self._overlay_future.done():
                self._overlay_future.set_result(None)

        # Question panel keys
        _question_active_filter = Condition(lambda: self._question_active)

        @self._kb.add("up", filter=_question_active_filter)
        def _(event):
            self._question_cursor = (self._question_cursor - 1) % len(self._question_labels)
            self._render_question_panel()
            self._app.invalidate()

        @self._kb.add("down", filter=_question_active_filter)
        def _(event):
            self._question_cursor = (self._question_cursor + 1) % len(self._question_labels)
            self._render_question_panel()
            self._app.invalidate()

        @self._kb.add("enter", filter=_question_active_filter)
        def _(event):
            if self._question_future and not self._question_future.done():
                other_idx = len(self._question_labels) - 1
                if self._question_cursor == other_idx and self._question_other_text:
                    self._question_future.set_result(self._question_other_text)
                elif self._question_cursor == other_idx:
                    self._question_future.set_result(None)  # empty Other = cancel
                else:
                    self._question_future.set_result(
                        self._question_labels[self._question_cursor])

        @self._kb.add("escape", filter=_question_active_filter)
        def _(event):
            other_idx = len(self._question_labels) - 1
            if self._question_cursor == other_idx and self._question_other_text:
                self._question_other_text = ""
                self._render_question_panel()
                self._app.invalidate()
                return
            if self._question_future and not self._question_future.done():
                self._question_future.set_result(None)

        @self._kb.add("backspace", filter=_question_active_filter)
        def _(event):
            other_idx = len(self._question_labels) - 1
            if self._question_cursor == other_idx and self._question_other_text:
                self._question_other_text = self._question_other_text[:-1]
                self._render_question_panel()
                self._app.invalidate()

        @self._kb.add("<any>", filter=_question_active_filter)
        def _(event):
            ch = event.data
            if not ch or not ch.isprintable():
                return
            other_idx = len(self._question_labels) - 1
            if self._question_cursor == other_idx:
                # Type into Other text buffer
                self._question_other_text += ch
                self._render_question_panel()
                self._app.invalidate()
                return
            # Number quick-select on regular options
            if ch.isdigit():
                idx = int(ch) - 1
                if 0 <= idx < len(self._question_labels):
                    if idx == other_idx:
                        self._question_cursor = other_idx
                    elif self._question_future and not self._question_future.done():
                        self._question_future.set_result(
                            self._question_labels[idx])
                    return
            # Any other char: jump to Other and start typing
            self._question_cursor = other_idx
            self._question_other_text += ch
            self._render_question_panel()
            self._app.invalidate()

        for _n in range(1, 10):
            @self._kb.add(str(_n), filter=_question_active_filter)
            def _(event, n=_n):
                idx = n - 1
                if idx < len(self._question_labels):
                    if self._question_future and not self._question_future.done():
                        self._question_future.set_result(
                            self._question_labels[idx])

        # Number shortcuts 1-9 for quick model selection
        for _n in range(1, 10):
            @self._kb.add(str(_n), filter=_overlay_active)
            def _(event, n=_n):
                idx = n - 1
                if idx < len(self._overlay_options):
                    self._overlay_cursor = idx
                    if self._overlay_future and not self._overlay_future.done():
                        self._overlay_future.set_result(
                            (self._overlay_options[idx][0],
                             self._overlay_effort_idx)
                        )

        # Application
        self._app = Application(
            layout=self._layout,
            key_bindings=self._kb,
            full_screen=True,
            mouse_support=True,
        )

    def _resolve_permission(self, response: str) -> None:
        """Resolve the pending permission future if one exists."""
        if self._permission_future is not None and not self._permission_future.done():
            self._permission_future.set_result(response)

    # ---- refresh ------------------------------------------------------------

    def _full_redraw(self) -> None:
        """Force complete terminal redraw (for recovery after mini PT apps)."""
        try:
            self._app.renderer.clear()
        except Exception:
            pass
        self._refresh()

    def _refresh(self) -> None:
        """Push latest display state into the UI controls and invalidate."""
        import time as _time
        # Update spinner animation if thinking
        if self._thinking_start and self._is_processing:
            elapsed = _time.monotonic() - self._thinking_start
            self.display.show_thinking(elapsed)
        ft = self.display.render()
        plain = _fmt_to_plain(ft)
        self._chat_buffer.text = plain
        self._chat_control.styled_text = ft
        self._status_control.text = self.display.render_status_line()
        if self._following:
            self._chat_buffer.cursor_position = len(plain)
        self._app.invalidate()

    # ---- input handling -----------------------------------------------------

    # ---- model picker overlay ------------------------------------------------

    def _render_overlay(self) -> list[tuple[str, str]]:
        """Render model/session picker overlay with scroll support."""
        current = self.engine.get_model()
        effort_levels = ["low", "medium", "high"]
        effort_sym = {"low": "◑", "medium": "◕", "high": "●"}
        eff = effort_levels[self._overlay_effort_idx]
        MAX_VISIBLE = 8  # max items visible, scroll when more

        lines: list[tuple[str, str]] = []

        # Title
        if self._overlay_options and len(self._overlay_options[0]) > 2:
            lines.append(("bold ansibrightcyan", "  Select session\n"))
            lines.append(("ansigray", "  Pick a session to resume.\n\n"))
        else:
            lines.append(("bold ansibrightcyan", "  Select model\n"))
            lines.append(("ansigray", "  Switch between models.\n\n"))

        # Calculate visible range
        total = len(self._overlay_options)
        # Keep cursor in view
        if self._overlay_cursor < self._overlay_scroll:
            self._overlay_scroll = self._overlay_cursor
        elif self._overlay_cursor >= self._overlay_scroll + MAX_VISIBLE:
            self._overlay_scroll = self._overlay_cursor - MAX_VISIBLE + 1
        start = max(0, self._overlay_scroll)
        end = min(total, start + MAX_VISIBLE)

        for i in range(start, end):
            alias, label, desc = self._overlay_options[i]
            is_cur = i == self._overlay_cursor
            is_active = alias == self._overlay_current_alias
            ptr = "❯" if is_cur else " "
            sty = "bold ansibrightcyan" if is_cur else ""
            chk = " ✔" if is_active else ""
            lines.append((sty, f"  {ptr} {i+1}. {label}{chk}\n"))
            if desc:
                lines.append(("ansigray", f"     {desc}\n"))

        if total > MAX_VISIBLE:
            lines.append(("ansigray", f"\n  ({start+1}-{min(end,total)} of {total})"))

        lines.append(("", "\n"))
        if self._overlay_options and len(self._overlay_options[0]) <= 2:
            lines.append(("ansigray", "  Effort: "))
            for lvl in effort_levels:
                s = "bold ansibrightcyan" if lvl == eff else "ansigray"
                lines.append((s, f" {effort_sym[lvl]} {lvl} "))
            lines.append(("", "\n"))
        lines.append(("ansigray", "  ↑↓ select · ↵ confirm · esc cancel"))
        return lines

    async def _show_model_picker(self, options: list[tuple[str, str, str]],
                                  current_alias: str) -> Optional[str]:
        """Show modal model picker, return selected alias or None."""
        for i, (alias, _, _) in enumerate(options):
            if alias == current_alias:
                self._overlay_cursor = i
                break
        self._overlay_options = options
        self._overlay_current_alias = current_alias
        self._overlay_effort_idx = 2
        self._overlay_future = asyncio.get_running_loop().create_future()
        self._overlay_active = True
        self._overlay_control.text = self._render_overlay()
        self._app.invalidate()
        try:
            return await self._overlay_future
        finally:
            self._overlay_active = False
            self._overlay_future = None
            self._app.invalidate()

    async def _handle_resume_command(self) -> None:
        """Show session list inline above input area."""
        from core.session import SessionStore
        cwd = __import__('os').getcwd()
        sessions = SessionStore.list_sessions(cwd)
        if not sessions:
            self.display.add_system_message("No saved sessions.")
            self._refresh()
            return

        options = [(s.session_id[:8], f"{s.title[:50]} ({s.message_count} msgs)", s.updated_at[:10] if hasattr(s, 'updated_at') else "") for s in sessions]
        result = await self._show_model_picker(options, "")
        if result is None:
            return

        # Find and resume selected session
        for s in sessions:
            if s.session_id[:8] == result:
                _, messages = SessionStore.load_session(s.session_id, cwd)
                if messages:
                    self.engine.set_messages(messages)
                    self.display._messages.clear()
                    self.display.add_system_message(f"Resumed: {s.title[:50]}")
                    self._refresh()
                return

    async def _handle_cost_command(self) -> None:
        """Show cost info above input area."""
        if self.cost_tracker is None:
            self.display.add_system_message("Cost tracking unavailable.")
            self._refresh()
            return
        cost_text = self.cost_tracker.format_cost()
        self._header_control.text = [
            ("bold fg:ansicyan", f" cc-mini "),
            ("", f"{cost_text}"),
        ]
        self._refresh()

    def _hide_overlay(self) -> None:
        self._overlay_active = False
        self._app.invalidate()

    async def _handle_model_command(self, args: str) -> None:
        """Handle /model: direct switch or overlay picker."""
        from core.config import resolve_model, default_max_tokens_for_model, DEFAULT_MODEL

        provider = self.app_config.provider if self.app_config else "anthropic"
        current = self.engine.get_model()

        # Direct model switch: /model <name>
        if args.strip():
            model_name = args.strip()
            self.engine.set_model(model_name)
            actual = self.engine.get_model()
            self.display.set_status(f"Model: {actual}")
            self._refresh()
            return

        # Build options based on provider
        if provider == "lmstudio":
            try:
                available = self.engine.list_available_models()
            except Exception:
                available = []
            if not available and self.app_config:
                available = list(self.app_config.model_list)
            if current not in available:
                available.insert(0, current)
            options = [(m, m, "") for m in available]
            if not options:
                self.display.add_system_message("No LM Studio models discovered.")
                self._refresh()
                return
        else:
            # Anthropic model options (matches old _cmd_model)
            _NAMES = {
                "claude-sonnet-4-6": "Sonnet 4.6", "claude-sonnet-4-5": "Sonnet 4.5",
                "claude-sonnet-4": "Sonnet 4", "claude-opus-4-6": "Opus 4.6",
                "claude-opus-4-5": "Opus 4.5", "claude-opus-4-1": "Opus 4.1",
                "claude-opus-4": "Opus 4", "claude-haiku-4-5": "Haiku 4.5",
            }
            display_name = next((n for p, n in _NAMES.items() if p in current), "Sonnet 4.6")
            options = [
                (DEFAULT_MODEL, f"Default ({display_name})",
                 "Use the default model · $3/$15 per Mtok"),
                ("sonnet", "Sonnet 4.6",
                 "Best for everyday tasks · $3/$15 per Mtok"),
                ("opus", "Opus 4.6",
                 "Most capable for complex work · $5/$25 per Mtok"),
                ("haiku", "Haiku 4.5",
                 "Fastest for quick answers · $1/$5 per Mtok"),
            ]

        # Resolve current alias for cursor positioning
        current_alias = current
        for alias, _, _ in options:
            if resolve_model(alias) == current:
                current_alias = alias
                break

        result = await self._show_model_picker(options, current_alias)
        if result is None:
            self.display.set_status(f"Kept model as {current}")
            self._refresh()
            return

        self.engine.set_model(result)
        actual = self.engine.get_model()
        if self.session_store:
            self.session_store.model = actual
        self.display.set_status(f"Model: {actual}")
        self._refresh()

    # ---- dismiss --------------------------------------------------------------

    def _dismiss_command_output(self) -> None:
        """Clear command output messages from the display."""
        if self._dismissable_count <= 0:
            return
        # Remove the last N messages that were added by the command
        keep = len(self.display._messages) - self._dismissable_count
        if keep >= 0:
            self.display._messages = self.display._messages[:keep]
        self._dismissable_count = 0
        self._refresh()

    def _on_send(self, buffer: Buffer) -> bool:
        """Handle Enter in the input field.

        Supports message stacking: when the assistant is already processing,
        new messages are queued and displayed immediately, then processed
        in order when the current turn finishes.
        """
        if self._overlay_active or self._question_active:
            return True  # ignore during overlay/question
        if self._permission_future is not None:
            return True  # ignore during permission prompt

        text = buffer.text.strip()
        if not text:
            return True

        # New input dismisses previous command output and resets state
        self._dismissable_count = 0
        self._last_ctrlc_time = 0.0
        self._following = True  # auto-follow new responses
        buffer.text = ""

        if self._is_processing:
            # Show stacked messages above header, not in chat yet
            self._pending_stack.append(text)
            lines = []
            for msg in self._pending_stack:
                preview = msg[:60] + ("…" if len(msg) > 60 else "")
                lines.append(("class:pending", f" {preview}\n"))
            self._pending_control.text = lines
            self._refresh()
            return True

        loop = asyncio.get_running_loop()
        self._current_task = loop.create_task(self._process_input(text))
        return True

    # ---- main processing loop -----------------------------------------------

    async def _process_input(self, text: str) -> None:
        """Process user input: handle commands, shell, or submit to engine."""
        self._is_processing = True
        self._abort_requested = False

        try:
            if self._terminal_mode:
                await self._run_shell(text)
                return

            if text.startswith("!") and len(text) > 1:
                await self._run_shell(text[1:].lstrip())
                return

            if text.lower() in ("exit", "quit", "/exit", "/quit"):
                self.display.add_system_message("Goodbye.")
                self._refresh()
                try:
                    self._app.exit()
                except Exception:
                    pass
                return

            if text.startswith("/") and not text.startswith("/sandbox"):
                await self._handle_command(text)
                return

            if text.startswith("/sandbox"):
                if self._sandbox_mgr is not None:
                    import subprocess
                    parts = text.strip().split(maxsplit=1)
                    subcmd = parts[1] if len(parts) > 1 else "status"
                    if subcmd == "status":
                        dep = self._sandbox_mgr.check_dependencies()
                        mode = ("auto-allow" if self._sandbox_mgr.is_auto_allow()
                                else ("regular" if self._sandbox_mgr.config.enabled else "disabled"))
                        self.display.add_system_message(
                            f"Sandbox: [cyan]{mode}[/cyan]  "
                            f"enabled={'yes' if self._sandbox_mgr.is_enabled() else 'no'}  "
                            f"net={'isolated' if self._sandbox_mgr.config.unshare_net else 'open'}"
                        )
                        for e in dep.errors:
                            self.display.add_system_message(f"[red]{e}[/red]")
                        for w in dep.warnings:
                            self.display.add_system_message(f"[yellow]{w}[/yellow]")
                    else:
                        self.display.add_system_message(
                            f"Sandbox: /sandbox {subcmd} — use /sandbox status for now")
                else:
                    self.display.add_system_message("Sandbox: not configured.")
                self._refresh()
                return

            # Normal message: add to chat and submit to engine
            self.display.add_user_message(text)
            self._refresh()
            await self._run_engine(text)

        except asyncio.CancelledError:
            self.display.add_system_message("[dim yellow]⏹ Turn cancelled[/dim yellow]")
        except Exception as exc:
            self.display.add_system_message(f"[red]Error: {exc}[/red]")
        finally:
            self._is_processing = False
            self._abort_requested = False
            self.display.set_status("")
            self._refresh()

            # Drain stacked messages — process next queued input if any
            if self._pending_stack:
                next_msg = self._pending_stack.pop(0)
                if self._pending_stack:
                    lines = []
                    for msg in self._pending_stack:
                        preview = msg[:60] + ("…" if len(msg) > 60 else "")
                        lines.append(("class:pending", f" {preview}\n"))
                    self._pending_control.text = lines
                else:
                    self._pending_control.text = []
                loop = asyncio.get_running_loop()
                self._current_task = loop.create_task(self._process_input(next_msg))

    async def _run_engine(self, user_input) -> None:
        """Submit user input to the engine and stream results to the display."""
        from tui.engine_bridge import submit_async
        import time as _time

        await self._auto_compact()

        t0 = _time.monotonic()
        self._thinking_start = t0
        self.display.show_thinking(0.0)
        self._refresh()

        # Background spinner ticker — updates spinner even when no events arrive
        async def _tick_spinner():
            while self._thinking_start is not None:
                await asyncio.sleep(0.1)
                if self._thinking_start is not None:
                    self._refresh()
        spinner_task = asyncio.create_task(_tick_spinner())

        try:
            await submit_async(
                engine=self.engine,
                user_input=user_input,
                display=self.display,
                permissions=self.permissions,
                permission_handler=self._permission_handler,
                refresh_callback=self._refresh,
                question_handler=self._question_handler,
            )
        except Exception as exc:
            self.display.add_system_message(f"[red]{exc}[/red]")

        elapsed = _time.monotonic() - t0
        self._thinking_start = None
        spinner_task.cancel()
        self.display.hide_thinking()
        self.display.mark_done_timing(elapsed)
        self._post_turn_hooks()

    # ---- permission prompt handler ------------------------------------------

    async def _question_handler(self, questions: list) -> list:
        """Handle AskUserQuestion inline — renders above input area."""
        results = []
        for q in questions:
            question_text = q.get("question", "")
            options = q.get("options", [])
            labels = [o["label"] for o in options] + ["Other"]
            answer = await self._show_question_panel(question_text, labels)
            if answer is None:
                return []
            results.append(answer)
        return results

    async def _show_question_panel(self, question: str,
                                    labels: list) -> Optional[str]:
        """Show question inline above input, return selected label or None."""
        self._question_active = True
        self._question_cursor = 0
        self._question_labels = labels
        self._question_text = question
        self._question_other_text = ""
        self._question_future = asyncio.get_running_loop().create_future()
        self._render_question_panel()
        self._app.invalidate()
        try:
            return await self._question_future
        finally:
            self._question_active = False
            self._question_future = None
            self._app.invalidate()

    def _render_question_panel(self):
        other_idx = len(self._question_labels) - 1  # last option = Other
        lines: list[tuple[str, str]] = []
        lines.append(("bold", f"? {self._question_text}\n"))
        for i, label in enumerate(self._question_labels):
            is_cur = i == self._question_cursor
            ptr = "❯" if is_cur else " "
            sty = "bold ansibrightcyan" if is_cur else ""
            if i == other_idx and is_cur and self._question_other_text:
                lines.append((sty, f"  {ptr} {i+1}) "))
                lines.append(("ansibrightgreen bold", self._question_other_text))
                lines.append(("ansigray", "█\n"))
            elif i == other_idx and is_cur:
                lines.append((sty, f"  {ptr} {i+1}) {label}\n"))
                lines.append(("ansigray", "     Type something.\n"))
            else:
                lines.append((sty, f"  {ptr} {i+1}) {label}\n"))
        lines.append(("ansigray", "  ↑↓ select · type for Other · ↵ confirm · esc cancel"))
        self._panel_control.text = lines

    async def _permission_handler(self, tool_name: str, tool_input: dict) -> str:
        """Show an inline permission prompt and wait for y/n/a key press.

        Called by submit_async() when the engine thread needs permission.
        Returns "allow", "deny", or "always".
        """
        # Show the prompt in the chat display and status line
        preview = _format_tool_input(tool_name, tool_input)
        self.display.add_system_message(
            f"[bold yellow]Permission required:[/bold yellow] [bold]{tool_name}[/bold]  {preview}"
        )
        self.display.set_status(
            f"  [{tool_name}]  [Y]es / [N]o / [A]lways ?"
        )
        self._permission_tool_name = tool_name
        self._refresh()

        # Create a Future and wait for a key binding to resolve it
        self._permission_future = asyncio.get_running_loop().create_future()

        try:
            response = await self._permission_future
        finally:
            self._permission_future = None
            self._permission_tool_name = ""

        # Confirm the choice
        labels = {"allow": "Yes", "deny": "No", "always": "Always"}
        label = labels.get(response, response)
        self.display.add_system_message(f"[dim]  → {label}[/dim]")
        self.display.set_status("")
        self._refresh()

        return response

    # ---- shell execution ----------------------------------------------------

    async def _run_shell(self, cmd: str) -> None:
        """Execute a shell command and show output."""
        import subprocess

        self.display.add_system_message(f"$ {cmd}")
        self._refresh()

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, shell=True, text=True, encoding="utf-8", errors="replace",
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
            )
            if result.stdout:
                for line in result.stdout.splitlines():
                    self.display.add_system_message(line)
            if result.returncode != 0:
                self.display.add_system_message(f"[red][exit {result.returncode}][/red]")
        except Exception as exc:
            self.display.add_system_message(f"[red]Error: {exc}[/red]")
        self._refresh()

    # ---- command handling ---------------------------------------------------

    async def _handle_command(self, text: str) -> None:
        """Handle slash commands, posting output to the display.

        Runs command handler in a thread executor so that commands which
        create their own prompt_toolkit Applications (e.g. /model) can
        call app.run() without conflicting with the running event loop.
        """
        from commands import parse_command, handle_command, CommandContext
        from core.session import SessionStore

        parsed = parse_command(text)
        if parsed is None:
            return

        cmd_name, cmd_args = parsed

        # /model with overlay — handled inline, not via thread executor
        if cmd_name == "model":
            await self._handle_model_command(cmd_args)
            return
        if cmd_name == "resume":
            await self._handle_resume_command()
            return
        if cmd_name == "cost":
            await self._handle_cost_command()
            return
        if cmd_name == "clear":
            self.display._messages.clear()
            self.engine.set_messages([])
            self.display.add_system_message("Conversation cleared.")
            self._refresh()
            return
        if cmd_name in ("exit", "quit"):
            self.display.add_system_message("Goodbye.")
            self._refresh()
            try:
                self._app.exit()
            except Exception:
                pass
            return

        ctx = CommandContext(
            engine=self.engine,
            session_store=self.session_store,
            compact_service=self.compact_service,
            console=None,
            app_config=self.app_config,
            memory_dir=self.memory_dir,
            permissions=self.permissions,
            run_dream=None,
            cost_tracker=self.cost_tracker,
            new_session_store=(lambda: SessionStore(
                cwd=str(__import__('os').getcwd()),
                model=self.engine.get_model(),
            )) if self.session_store else None,
            reconfigure_mode=None,
            plan_manager=self.plan_manager,
            on_model_change=lambda model: None,
            pending_query=None,
        )

        # Save message count before command so we can track what was added
        _before_count = len(self.display._messages)

        ctx.console = _DisplayConsole(self.display)
        self.display.set_status(f"Running /{cmd_name}…")
        self._refresh()

        # Run in thread: commands like /model internally call app.run()
        # which uses asyncio.run() — this must happen in a fresh thread
        # where no event loop is running.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: handle_command(cmd_name, cmd_args, ctx),
        )

        # Remember how many messages command output added — Esc can dismiss them
        _added = len(self.display._messages) - _before_count
        if _added > 0:
            self._dismissable_count = _added

        # If the command set a pending query (e.g. /plan <description>),
        # process it now
        if ctx.pending_query:
            query = ctx.pending_query
            ctx.pending_query = None
            self._dismissable_count = 0  # query output replaces command output
            self.display.add_user_message(query)
            self._refresh()
            await self._run_engine(query)
            return

        self.display.set_status("")
        self._refresh()

    # ---- auto-compact / post-turn hooks -------------------------------------

    async def _auto_compact(self) -> None:
        """Auto-compact conversation when approaching token limits."""
        if self.compact_service is None or self.cost_tracker is None:
            return
        try:
            from features.compact import should_compact, estimate_tokens
            messages = self.engine.get_messages()
            model = self.engine.get_model()
            if should_compact(
                messages, model=model,
                last_input_tokens=self.cost_tracker.last_input_tokens,
            ):
                self.display.add_system_message("Auto-compacting conversation…")
                self._refresh()
                new_msgs, _ = self.compact_service.compact(
                    messages, self.engine.system_prompt,
                )
                self.engine.set_messages(new_msgs)
                self.display.add_system_message(
                    f"Context compressed to {estimate_tokens(new_msgs):,} tokens."
                )
        except Exception:
            pass

    def _post_turn_hooks(self) -> None:
        """Extract memory tags and trigger auto-dream after each turn."""
        # Extract <memory> tags from assistant output
        if self.memory_dir is not None:
            try:
                from features.memory import extract_memory_tags, append_to_daily_log
                text = self.engine.last_assistant_text()
                for mem in extract_memory_tags(text):
                    append_to_daily_log(self.memory_dir, mem)
            except Exception:
                pass

        # Auto-dream gate check (mirrors legacy REPL behavior)
        if (self.app_config and self.app_config.auto_dream
                and self.memory_dir is not None and self._run_dream_fn is not None):
            try:
                from features.memory import (
                    should_auto_dream, read_last_consolidated_at,
                    try_acquire_lock, release_lock, list_sessions_since,
                )
                ss = self.session_store
                current_sid = ss.session_id if ss else ""
                sessions_path = ss._dir if ss else None
                if should_auto_dream(
                    self.memory_dir,
                    min_hours=self.app_config.dream_interval_hours,
                    min_sessions=self.app_config.dream_min_sessions,
                    current_session_id=current_sid,
                    sessions_dir=sessions_path,
                ):
                    prior_mtime = read_last_consolidated_at(self.memory_dir)
                    if try_acquire_lock(self.memory_dir):
                        try:
                            sids = list_sessions_since(
                                prior_mtime,
                                sessions_dir=sessions_path,
                                current_session_id=current_sid,
                            )
                            transcript_dir = str(sessions_path) if sessions_path else ""
                            self._run_dream_fn(
                                quiet=True,
                                transcript_dir=transcript_dir,
                                session_ids=sids,
                            )
                            release_lock(self.memory_dir)
                        except Exception:
                            # Rollback lock mtime so dream retries next time
                            from features.memory import _lock_path
                            try:
                                lp = _lock_path(self.memory_dir)
                                if lp.exists():
                                    import os
                                    os.utime(lp, (prior_mtime, prior_mtime))
                            except OSError:
                                pass
            except Exception:
                pass

    # ---- entry point --------------------------------------------------------

    async def run(self) -> None:
        """Launch the TUI (blocks until the user exits)."""
        provider = getattr(self.app_config, 'provider', 'anthropic') if self.app_config else 'anthropic'
        model = self.engine.get_model()
        MODES = ["normal", "auto-approve", "plan"]
        PT_COLORS = {"normal": "fg:ansigreen", "auto-approve": "fg:ansiyellow", "plan": "fg:ansicyan"}
        mode = MODES[self._perm_mode]
        self._header_control.text = [
            ("bold fg:ansicyan", f" cc-mini "),
            ("", f"{provider}:{model}  "),
            (PT_COLORS[mode], f"({mode})"),
        ]
        self._refresh()
        await self._app.run_async()


# ---- display console shim ---------------------------------------------------

class _DisplayConsole:
    """A shim that wraps ChatDisplay so commands using console.print() work.

    Renders Rich markup via an off-screen Console, strips ANSI codes,
    and posts plain text to ChatDisplay.
    """

    def __init__(self, display: ChatDisplay):
        self._display = display
        from rich.console import Console as _RC
        self._rich = _RC(force_terminal=True, color_system="truecolor")

    def print(self, *args, **kwargs) -> None:
        import re
        with self._rich.capture() as capture:
            self._rich.print(*args, **kwargs)
        ansi = capture.get()
        text = re.sub(r'\x1b\[[0-9;]*m', '', ansi)
        self._display.add_system_message(text.rstrip("\n"), plain=True)

    def __getattr__(self, name):
        return lambda *a, **kw: None


# ---- helpers ----------------------------------------------------------------

def _format_tool_input(tool_name: str, tool_input: dict) -> str:
    """Format tool input for the permission prompt preview."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:80] + ("…" if len(cmd) > 80 else "")
    if tool_name in ("Read", "Edit", "Write"):
        return tool_input.get("file_path", "")
    if tool_name in ("Glob", "Grep"):
        return tool_input.get("pattern", "")
    # Generic: show first key/value
    items = list(tool_input.items())[:2]
    return " ".join(f"{k}={str(v)[:40]}" for k, v in items)
