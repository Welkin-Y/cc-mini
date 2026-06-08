"""FormattedInput — custom input area with selection + inline completion."""

from __future__ import annotations

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.layout.screen import Point
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.completion import Completer


class FormattedInput(UIControl):
    """Custom single-line input control with mouse selection support.

    - Buffer for text storage + selection + cursor
    - Mouse click/drag → text selection
    - Keyboard input → insert text (via get_key_bindings)
    - Enter → calls accept_handler
    - Slash-completion via completer
    """

    def __init__(
        self,
        buffer: Buffer,
        prompt: str = "> ",
        completer: Completer | None = None,
        accept_handler=None,
    ):
        self.buffer = buffer
        self._prompt = prompt
        self._completer = completer
        self._accept_handler = accept_handler
        self._kb = KeyBindings()

        # Build key bindings for typing+enter
        @self._kb.add("<any>")
        def _(event):
            key = event.key_sequence[0].key if event.key_sequence else ""
            # Printable chars: insert into buffer
            if len(key) == 1 and key.isprintable():
                self.buffer.insert_text(key)
            elif key == "backspace":
                self.buffer.delete_before_cursor()
            elif key == "delete":
                self.buffer.delete()
            elif key == "left":
                self.buffer.cursor_position = max(
                    0, self.buffer.cursor_position - 1)
            elif key == "right":
                self.buffer.cursor_position = min(
                    len(self.buffer.text), self.buffer.cursor_position + 1)
            elif key == "home":
                self.buffer.cursor_position = 0
            elif key == "end":
                self.buffer.cursor_position = len(self.buffer.text)
            elif key == "enter":
                if self._accept_handler:
                    self._accept_handler(self.buffer)
                return
            elif key == "escape":
                # Clear selection on Esc
                self.buffer.selection_state = None
            # Trigger slash completion
            if self._completer and self.buffer.text.lstrip().startswith("/"):
                import asyncio
                try:
                    self.buffer.start_completion(select_first=False)
                except Exception:
                    pass

    def is_focusable(self) -> bool:
        return True  # we want keyboard input

    def preferred_width(self, max_available_width: int) -> int | None:
        return None

    def preferred_height(
        self, width: int, max_available_height: int,
        wrap_lines: bool, get_line_prefix,
    ) -> int | None:
        return 1

    def create_content(self, width: int, height: int | None = None):
        doc = self.buffer.document
        display_text = f"{self._prompt}{doc.text}"

        # Selection in prompt+text space
        sel = self.buffer.selection_state
        sel_s, sel_e = -1, -1
        if isinstance(sel, tuple) and len(sel) == 2:
            sel_s, sel_e = min(sel) + len(self._prompt), max(sel) + len(self._prompt)

        def get_line(i: int):
            if i != 0:
                return []
            result: list[tuple[str, str]] = []
            if sel_s >= 0:
                before = display_text[:sel_s]
                mid = display_text[sel_s:sel_e]
                after = display_text[sel_e:]
                if before:
                    result.append(("", before))
                if mid:
                    result.append(("reverse", mid))
                if after:
                    result.append(("", after))
            else:
                result.append(("", display_text))
            return to_formatted_text(result)

        cursor_col = doc.cursor_position + len(self._prompt)

        return UIContent(
            get_line=get_line,
            line_count=1,
            cursor_position=Point(x=cursor_col, y=0),
            menu_position=None,
            show_cursor=True,
        )

    def mouse_handler(self, mouse_event):
        """Click/drag → text selection."""
        if mouse_event.event_type in (MouseEventType.SCROLL_UP,
                                       MouseEventType.SCROLL_DOWN):
            return NotImplemented

        pt = mouse_event.position
        col = max(0, pt.x - len(self._prompt))
        pos = max(0, min(len(self.buffer.text), col))

        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            self.buffer.cursor_position = pos
            self.buffer.selection_state = None

        elif mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            if mouse_event.button != MouseButton.NONE:
                sel = self.buffer.selection_state
                if sel is None or not isinstance(sel, tuple):
                    self.buffer.selection_state = (pos, pos)
                else:
                    self.buffer.selection_state = (sel[0], pos)
                self.buffer.cursor_position = pos

        elif mouse_event.event_type == MouseEventType.MOUSE_UP:
            self.buffer.cursor_position = pos

        return None

    def get_key_bindings(self):
        return self._kb

    def get_invalidate_events(self):
        return []
