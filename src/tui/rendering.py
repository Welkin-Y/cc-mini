"""Streaming markdown renderer and spinner manager for the TUI."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown as RichMarkdown
from rich.spinner import Spinner
from rich.text import Text

if TYPE_CHECKING:
    from features.todo import TodoItem


# Regex for top-level block boundaries: blank line, heading, fence, hr, list
_BLOCK_BOUNDARY_RE = re.compile(r"\n(?=\n|\#{1,6} |```|---|\* |- |\d+\. )")


class StreamingMarkdown:
    """Accumulates streamed text and renders markdown incrementally.

    Matches TS StreamingMarkdown approach: splits at block boundaries,
    prints stable (complete) blocks as Rich Markdown, keeps the unstable
    trailing part in a Live widget for real-time updates.
    """

    def __init__(self, console: Console):
        self._console = console
        self._buf = ""
        self._stable_len = 0  # how much of _buf has been printed as stable
        self._live: Live | None = None

    def feed(self, chunk: str) -> None:
        """Add a streamed text chunk and update the display."""
        self._buf += chunk
        self._render()

    def _render(self) -> None:
        # Find the last block boundary in the full buffer
        text = self._buf
        boundary = self._stable_len
        for m in _BLOCK_BOUNDARY_RE.finditer(text, self._stable_len):
            boundary = m.start()

        # Print newly stable blocks
        if boundary > self._stable_len:
            # Stop live widget before printing stable content
            if self._live is not None:
                self._live.stop()
                self._live = None
            stable_text = text[self._stable_len:boundary]
            self._console.print(RichMarkdown(stable_text), end="")
            self._stable_len = boundary

        # Update live widget with the unstable trailing part
        unstable = text[self._stable_len:]
        if unstable:
            if self._live is None:
                self._live = Live(
                    RichMarkdown(unstable),
                    console=self._console,
                    refresh_per_second=8,
                    transient=True,
                )
                self._live.start()
            else:
                self._live.update(RichMarkdown(unstable))

    def flush(self) -> None:
        """Finalize: render any remaining text as stable markdown."""
        if self._live is not None:
            self._live.stop()
            self._live = None
        remaining = self._buf[self._stable_len:]
        if remaining:
            self._console.print(RichMarkdown(remaining), end="")
        self._buf = ""
        self._stable_len = 0


class StreamingMarkdownBuffer:
    """Variant of StreamingMarkdown that renders to ANSI strings instead of printing.

    Used by the async TUI to accumulate streaming output into a buffer that
    can be rendered in a prompt_toolkit output area. The caller pulls newly
    stabilized ANSI chunks and displays the unstable trailing part live.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._stable_len = 0          # chars rendered as stable
        self._stable_ansi = ""        # accumulated stable ANSI output
        self._stable_consumed = 0     # how much of _stable_ansi has been pulled
        self._unstable_raw = ""       # current unstable trailing part

    def feed(self, chunk: str) -> None:
        """Add a streamed text chunk and update boundaries."""
        self._buf += chunk
        self._find_boundaries()

    def _find_boundaries(self) -> None:
        """Find block boundaries, render newly stable blocks to ANSI."""
        text = self._buf
        boundary = self._stable_len
        for m in _BLOCK_BOUNDARY_RE.finditer(text, self._stable_len):
            boundary = m.start()

        if boundary > self._stable_len:
            new_stable = text[self._stable_len:boundary]
            self._stable_ansi += _render_md_to_ansi(new_stable)
            self._stable_len = boundary

        self._unstable_raw = text[self._stable_len:]

    def pull_stable(self) -> str:
        """Return newly stabilized ANSI text since the last pull."""
        new_text = self._stable_ansi[self._stable_consumed:]
        self._stable_consumed = len(self._stable_ansi)
        return new_text

    def get_unstable(self) -> str:
        """Return ANSI-rendered version of the current unstable trailing part."""
        if self._unstable_raw:
            return _render_md_to_ansi(self._unstable_raw)
        return ""

    def flush(self) -> str:
        """Finalize: return ANSI-rendered text for any remaining content."""
        remaining = self._buf[self._stable_len:]
        self._buf = ""
        self._stable_len = 0
        self._stable_ansi = ""
        self._stable_consumed = 0
        self._unstable_raw = ""
        if remaining:
            return _render_md_to_ansi(remaining)
        return ""


def _render_md_to_ansi(text: str) -> str:
    """Render Markdown text to an ANSI string using rich."""
    from io import StringIO
    buf = StringIO()
    rc = Console(file=buf, force_terminal=True, color_system="standard", width=120)
    try:
        rc.print(RichMarkdown(text))
    except Exception:
        buf.write(text)
    return buf.getvalue()


class SpinnerManager:
    """Manages a Rich Live spinner that shows while waiting for API/tool responses.

    Matches claude-code-main's spinner behavior: show a spinning indicator
    with contextual text while the model is thinking or tools are executing.
    """

    def __init__(self, console: Console):
        self._console = console
        self._live: Live | None = None
        self._spinner_text = "Thinking…"

    def start(self, text: str = "Thinking…"):
        self._spinner_text = text
        # Stop existing Live instance if running
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._live = Live(
            Spinner("dots", text=Text(self._spinner_text, style="dim")),
            console=self._console,
            refresh_per_second=12,
            transient=True,
        )
        self._live.start()

    def update(self, text: str):
        self._spinner_text = text
        if self._live is not None:
            self._live.update(
                Spinner("dots", text=Text(self._spinner_text, style="dim"))
            )

    def stop(self):
        if self._live is not None:
            self._live.stop()
            self._live = None


def tool_preview(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:80] + ("…" if len(cmd) > 80 else "")
    if tool_name in ("Read", "Edit", "Write"):
        fp = tool_input.get("file_path", "")
        return fp[-60:] if len(fp) > 60 else fp
    if tool_name == "Glob":
        pat = tool_input.get("pattern", "")
        p = tool_input.get("path", "")
        return f"{pat} in {p}" if p else pat
    if tool_name == "Grep":
        pat = tool_input.get("pattern", "")
        p = tool_input.get("path", "")
        return f"{pat} in {p}" if p else pat
    if tool_name == "Agent":
        return tool_input.get("description", "")[:60]
    if tool_name == "SendMessage":
        return tool_input.get("to", "")
    return ""


def collapsed_tool_summary(tool_names: list[str], done: bool = False) -> str:
    """Summarize tools by type, matching TS CollapsedReadSearchContent.

    E.g. active: "Reading 5 files…"  done: "Read 5 files"
    """
    from collections import Counter
    counts = Counter(tool_names)
    parts = []
    _ACTIVE = {
        "Read": ("Reading {n} files", "Reading file"),
        "Glob": ("Searching {n} patterns", "Searching"),
        "Grep": ("Searching {n} patterns", "Searching"),
        "Bash": ("Running {n} commands", "Running command"),
        "Edit": ("Editing {n} files", "Editing file"),
        "Write": ("Writing {n} files", "Writing file"),
    }
    _DONE = {
        "Read": ("Read {n} files", "Read file"),
        "Glob": ("Searched {n} patterns", "Searched"),
        "Grep": ("Searched {n} patterns", "Searched"),
        "Bash": ("Ran {n} commands", "Ran command"),
        "Edit": ("Edited {n} files", "Edited file"),
        "Write": ("Wrote {n} files", "Wrote file"),
    }
    labels = _DONE if done else _ACTIVE
    for name, n in counts.items():
        plural, singular = labels.get(name, (f"{name} ×{{n}}", name))
        parts.append(plural.format(n=n) if n > 1 else singular)
    suffix = "" if done else "…"
    return " · ".join(parts) + suffix


# ---------------------------------------------------------------------------
# Todo list rendering
# ---------------------------------------------------------------------------

_TODO_ICONS = {
    "pending": "[dim]◻[/dim]",
    "in_progress": "[yellow]◼[/yellow]",
    "completed": "[green]✓[/green]",
}


def render_todo_list(items: list[TodoItem], console: Console) -> None:
    """Print a checklist-style todo list with status icons."""
    for item in items:
        icon = _TODO_ICONS.get(item.status, "[dim]◻[/dim]")
        subject = item.subject
        if len(subject) > 72:
            subject = subject[:69] + "…"
        if item.status == "completed":
            console.print(f"  {icon} [dim]{subject}[/dim]", highlight=False)
        elif item.status == "in_progress":
            console.print(f"  {icon} [bold]{subject}[/bold]", highlight=False)
        else:
            console.print(f"  {icon} {subject}", highlight=False)
