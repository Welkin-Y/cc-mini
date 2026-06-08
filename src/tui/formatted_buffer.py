"""FormattedBuffer — custom UIControl: Rich formatting + selection + scroll."""

from __future__ import annotations

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.layout.screen import Point
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.lexers import SimpleLexer


class FormattedBuffer(UIControl):
    """Custom UIControl that combines FormattedText styling with a Buffer.

    - Renders via FormattedText (Rich → ANSI styling)
    - Buffer handles cursor_position + selection_state (text selection)
    - Mouse scroll events → returned as NotImplemented → Window handles them
    - is_focusable = False → keyboard never goes here
    """

    def __init__(self, buffer: Buffer):
        self.buffer = buffer
        self._styled_text: FormattedText = []
        self._lexer = SimpleLexer()

    @property
    def styled_text(self) -> FormattedText:
        return self._styled_text

    @styled_text.setter
    def styled_text(self, value: FormattedText) -> None:
        self._styled_text = to_formatted_text(value)

    def is_focusable(self) -> bool:
        return False  # keyboard stays on TextArea

    def preferred_width(self, max_available_width: int) -> int | None:
        return None

    def preferred_height(
        self, width: int, max_available_height: int,
        wrap_lines: bool, get_line_prefix,
    ) -> int | None:
        return None  # let Window decide

    def create_content(self, width: int, height: int | None = None) -> UIContent:
        """Render buffer text with FormattedText styling and selection highlight."""
        doc = self.buffer.document
        styled_lines = _split_lines(self._styled_text)

        # Selection range (PT 3.x: tuple (start, end))
        sel = self.buffer.selection_state
        sel_start, sel_end = (sel if isinstance(sel, tuple) and len(sel) == 2
                              else (-1, -1))

        def get_line(i: int):
            if i >= len(doc.lines):
                return []
            plain = doc.lines[i]
            base = styled_lines[i] if i < len(styled_lines) else to_formatted_text(plain)

            # No selection on this line → return base styling
            line_start = doc.translate_row_col_to_index(i, 0)
            line_end = line_start + len(plain)
            if sel_end <= line_start or sel_start >= line_end:
                return base

            # Selection overlaps this line → apply reverse-video
            local_start = max(0, sel_start - line_start)
            local_end = min(len(plain), sel_end - line_start)

            result: list[tuple[str, str]] = []
            pos = 0
            for style, text in (base if isinstance(base, list) else [("", str(base))]):
                end = pos + len(text)
                if end <= local_start or pos >= local_end:
                    result.append((style, text))
                else:
                    # Split text: before selection, selected, after selection
                    before = text[:max(0, local_start - pos)]
                    mid_start = max(0, local_start - pos)
                    mid_end = min(len(text), local_end - pos)
                    mid = text[mid_start:mid_end]
                    after = text[mid_end:]
                    if before:
                        result.append((style, before))
                    if mid:
                        result.append((style + " reverse", mid))
                    if after:
                        result.append((style, after))
                pos = end
            return to_formatted_text(result) if result else base

        return UIContent(
            get_line=get_line,
            line_count=len(doc.lines),
            cursor_position=Point(
                x=doc.cursor_position_col,
                y=doc.cursor_position_row,
            ),
            menu_position=None,
            show_cursor=False,
        )

    def mouse_handler(self, mouse_event):
        """Scroll → Window. Click/drag → text selection in buffer."""
        if mouse_event.event_type in (MouseEventType.SCROLL_UP,
                                       MouseEventType.SCROLL_DOWN):
            return NotImplemented  # Window handles scroll

        doc = self.buffer.document
        pt = mouse_event.position
        row = max(0, min(doc.line_count - 1, pt.y))
        col = max(0, pt.x)
        pos = doc.translate_row_col_to_index(row, col)

        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            self.buffer.cursor_position = pos
            self.buffer.selection_state = None

        elif mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            btn = getattr(mouse_event, 'button', 0)
            if btn != 0:
                # Build selection range from original cursor to current pos
                orig = self.buffer.cursor_position
                start, end = sorted([orig, pos])
                self.buffer.cursor_position = pos
                self.buffer.selection_state = (start, end)

        elif mouse_event.event_type == MouseEventType.MOUSE_UP:
            self.buffer.cursor_position = pos

        return None  # handled

    def get_key_bindings(self):
        return None

    def get_invalidate_events(self):
        return []


def _split_lines(ft: FormattedText) -> list[FormattedText]:
    """Split FormattedText into per-line chunks."""
    lines: list[list[tuple[str, str]]] = [[]]
    for item in ft:
        style, text = item if isinstance(item, tuple) else ("", str(item))
        for j, part in enumerate(text.split("\n")):
            if j > 0:
                lines.append([])
            if part:
                lines[-1].append((style, part))
    return [to_formatted_text(line) for line in lines]
