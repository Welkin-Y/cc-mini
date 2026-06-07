"""Output buffer for the async TUI — accumulates rendered output above the input area.

Uses prompt_toolkit FormattedTextControl so the output area is read-only and
automatically scrolls to show the newest content.
"""
from __future__ import annotations

from io import StringIO

from prompt_toolkit.formatted_text import FormattedText, ANSI, to_formatted_text
from prompt_toolkit.layout.controls import FormattedTextControl
from rich.console import Console as RichConsole
from rich.markdown import Markdown as RichMarkdown

# Max lines to keep in the output buffer before truncating from the top
_MAX_LINES = 2000


class OutputBuffer:
    """Accumulates output lines as prompt_toolkit FormattedText.

    Provides a FormattedTextControl for use in the persistent PT Application layout.
    Content is automatically truncated from the top when it exceeds _MAX_LINES.
    """

    def __init__(self):
        self._lines: list[tuple[str, str]] = []  # [(style, text), ...]
        self._control = FormattedTextControl(
            text=self._get_formatted_text,
            focusable=False,
        )

    @property
    def control(self) -> FormattedTextControl:
        """Return the FormattedTextControl for use in a PT Window."""
        return self._control

    # -- appending -----------------------------------------------------------

    def append_markdown(self, text: str) -> None:
        """Render Markdown via rich, convert ANSI → FormattedText, append."""
        if not text.strip():
            return
        formatted = _render_markdown(text)
        self._lines.extend(to_formatted_text(formatted))

    def append_text(self, text: str, style: str = "") -> None:
        """Append plain or styled text."""
        for line in text.split('\n'):
            self._lines.append((style, line))

    def append_line(self, text: str, style: str = "") -> None:
        """Append a single line (no splitting)."""
        self._lines.append((style, text))

    def append_formatted(self, formatted: FormattedText) -> None:
        """Append pre-formatted text (list of (style, text) tuples)."""
        self._lines.extend(formatted)

    def clear(self) -> None:
        """Reset the output buffer."""
        self._lines.clear()
        self._control = FormattedTextControl(
            text=self._get_formatted_text,
            focusable=False,
        )

    # -- internal ------------------------------------------------------------

    def _get_formatted_text(self) -> FormattedText:
        """Return current content, truncated if needed."""
        if len(self._lines) > _MAX_LINES:
            excess = len(self._lines) - _MAX_LINES
            self._lines = self._lines[excess:]
        return FormattedText(self._lines)

    def invalidate(self) -> None:
        """Force a refresh (call from the PT app's invalidate or via callback)."""
        # FormattedTextControl reads from the callable on each render,
        # so changes are picked up automatically. This is a no-op hook
        # for explicit refresh scenarios if needed.
        pass


def _render_markdown(text: str) -> str:
    """Render Markdown text to an ANSI string via rich."""
    buf = StringIO()
    rc = RichConsole(
        file=buf,
        force_terminal=True,
        color_system="standard",
        width=120,
    )
    try:
        rc.print(RichMarkdown(text))
    except Exception:
        # Fallback: plain text if markdown rendering fails
        buf.write(text)
    return buf.getvalue()
