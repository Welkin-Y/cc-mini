"""Chat display — message store + Rich→ANSI→FormattedText rendering.

Replaces StreamingMarkdown, SpinnerManager, and scattered console.print calls
with a single unified display abstraction.

Pattern: follows async_ui's ChatHistory — messages are stored in a list,
each render() call re-renders the entire conversation via Rich → ANSI →
prompt_toolkit FormattedText.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text as RichText

# Shared off-screen console for markdown → ANSI rendering.
# force_terminal=True ensures rich emits ANSI even when stdout is not a TTY.
_render_console = Console(force_terminal=True, color_system="truecolor")


@dataclass
class _Msg:
    """Internal message node."""
    role: str  # "user" | "assistant" | "tool" | "system"
    content: str = ""
    tool_name: str = ""
    tool_preview: str = ""
    tool_status: str = ""     # "pending" | "running" | "done" | "error"
    tool_result_is_error: bool = False
    msg_id: str = ""


class ChatDisplay:
    """Unified chat display — stores messages, renders via Rich ANSI.

    Usage:
        display = ChatDisplay()
        display.add_user_message("hello")
        mid = display.start_assistant_stream()
        display.append_token(mid, "Hello")
        display.append_token(mid, " world!")
        display.finish_assistant_stream(mid)
        tk = display.add_tool_call("Bash", {"command": "ls"}, "list files")
        display.update_tool_running(tk)
        display.update_tool_done(tk, "file1\nfile2", is_error=False)
        display.add_system_message("Done.")
        formatted = display.render()  # → FormattedText for prompt_toolkit
    """

    def __init__(self) -> None:
        self._messages: list[_Msg] = []
        self._status: str = ""
        self._counter = 0

    # -- mutation ------------------------------------------------------------

    def add_user_message(self, text: str) -> None:
        self._messages.append(_Msg(role="user", content=text))

    def start_assistant_stream(self) -> str:
        """Begin a streaming assistant message. Returns a msg_id."""
        self._counter += 1
        msg_id = f"asst_{self._counter}"
        self._messages.append(_Msg(role="assistant", msg_id=msg_id))
        return msg_id

    def append_token(self, msg_id: str, token: str) -> None:
        """Append a token to the streaming assistant message."""
        for msg in reversed(self._messages):
            if msg.role == "assistant" and msg.msg_id == msg_id:
                msg.content += token
                return

    def finish_assistant_stream(self, msg_id: str) -> None:
        """Mark the streaming assistant message as complete."""
        # Currently a no-op — token accumulation is the state
        pass

    def add_tool_call(
        self,
        tool_name: str,
        tool_input: dict,
        activity: Optional[str] = None,
    ) -> str:
        """Add a pending tool call. Returns a key for status updates."""
        preview = _tool_preview(tool_name, tool_input)
        self._counter += 1
        key = f"tool_{self._counter}"
        self._messages.append(_Msg(
            role="tool",
            tool_name=tool_name,
            tool_preview=preview,
            tool_status="pending",
            msg_id=key,
        ))
        return key

    def update_tool_running(self, key: str) -> None:
        for msg in self._messages:
            if msg.msg_id == key:
                msg.tool_status = "running"
                return

    def update_tool_done(self, key: str, content: str = "", is_error: bool = False) -> None:
        found = False
        for msg in self._messages:
            if msg.msg_id == key:
                msg.tool_status = "error" if is_error else "done"
                msg.tool_result_is_error = is_error
                # Store short result excerpt for display
                if content:
                    excerpt = content[:200] + ("…" if len(content) > 200 else "")
                    msg.content = excerpt
                found = True
                break
        # Fallback: if key not found (e.g., concurrent tool from parallel batch),
        # add a minimal entry so the result is visible
        if not found:
            self._messages.append(_Msg(
                role="tool",
                tool_name="?",
                tool_preview=key,
                tool_status="error" if is_error else "done",
                tool_result_is_error=is_error,
                content=content[:200] if content else "",
                msg_id=key,
            ))

    def add_system_message(self, text: str, plain: bool = False) -> None:
        """Add a system/info message (errors, status, etc.).

        If *plain* is True, the text is rendered as-is without Rich markup parsing.
        """
        self._messages.append(_Msg(role="system", content=text,
                                   tool_status="plain" if plain else ""))

    _SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    _spin_idx = 0

    def show_thinking(self, _elapsed: float = 0.0) -> None:
        """Show spinner animation in the output area."""
        self._spin_idx = (self._spin_idx + 1) % len(self._SPINNER)
        frame = self._SPINNER[self._spin_idx]
        self._messages = [m for m in self._messages if m.role != "_thinking"]
        self._messages.append(_Msg(role="_thinking", content=f"  {frame} Thinking…"))

    def hide_thinking(self) -> None:
        """Remove thinking indicator."""
        self._messages = [m for m in self._messages if m.role != "_thinking"]

    def mark_done_timing(self, elapsed: float) -> None:
        """Show completion timing in the output area."""
        self._messages.append(_Msg(role="system", content=f"  ✓ Done ({elapsed:.1f}s)"))

    def set_status(self, text: str) -> None:
        self._status = text

    def render_ansi(self) -> str:
        """Render all messages to an ANSI string for BufferControl.

        Unlike ``render()`` which returns ``FormattedText`` for
        ``FormattedTextControl``, this method renders via Rich Console
        capture to a raw ANSI string.  The terminal interprets the
        escape sequences, giving us styled output + native text selection
        + mouse scrolling — all at once.
        """
        buf_lines: list[str] = []
        for msg in self._messages:
            if msg.role == "user":
                buf_lines.append(f"\x1b[1;36m▸ {msg.content}\x1b[0m")
            elif msg.role == "assistant":
                if msg.content:
                    try:
                        with _render_console.capture() as capture:
                            _render_console.print(Markdown(msg.content))
                        buf_lines.append(capture.get().rstrip("\n"))
                    except Exception:
                        buf_lines.append(msg.content)
            elif msg.role == "tool":
                label = f"↳ {msg.tool_name}({msg.tool_preview})" if msg.tool_preview else f"↳ {msg.tool_name}"
                if msg.tool_status == "done":
                    icon = "\x1b[31m✗\x1b[0m" if msg.tool_result_is_error else "\x1b[32m✓\x1b[0m"
                    buf_lines.append(f"  {icon} \x1b[37m{label}\x1b[0m")
                elif msg.tool_status == "error":
                    buf_lines.append(f"  \x1b[31m✗\x1b[0m \x1b[37m{label}\x1b[0m")
                elif msg.tool_status == "running":
                    buf_lines.append(f"\x1b[37m  {label} …\x1b[0m")
                else:
                    buf_lines.append(f"\x1b[37m  {label}\x1b[0m")
                if msg.content and msg.tool_status in ("done", "error"):
                    content = msg.content.replace("\t", "    ")[:500]
                    for line in content.split("\n")[:5]:
                        buf_lines.append(f"\x1b[37m\x1b[2m     {line}\x1b[0m")
            elif msg.role == "system":
                if msg.tool_status == "plain":
                    buf_lines.append(f"\x1b[37m  {msg.content}\x1b[0m")
                else:
                    try:
                        from rich.text import Text as RichText
                        with _render_console.capture() as capture:
                            _render_console.print(RichText.from_markup(msg.content))
                        buf_lines.append(capture.get().rstrip("\n"))
                    except Exception:
                        buf_lines.append(f"\x1b[37m  {msg.content}\x1b[0m")
        return "\n".join(buf_lines) + "\n"

    # -- rendering -----------------------------------------------------------

    def render(self) -> FormattedText:
        """Render all messages to a prompt_toolkit FormattedText."""
        result: list[tuple[str, str]] = []

        for msg in self._messages:
            if msg.role == "user":
                _render_user(msg, result)
            elif msg.role == "assistant":
                _render_assistant(msg, result)
            elif msg.role == "tool":
                _render_tool(msg, result)
            elif msg.role == "system":
                _render_system(msg, result)
            elif msg.role == "_thinking":
                _render_thinking(msg, result)

        # Trailing newline
        result.append(("", "\n"))
        return FormattedText(result)

    def render_status_line(self) -> list[tuple[str, str]]:
        """Render the status line."""
        if isinstance(self._status, list):
            return self._status
        if self._status:
            return [("fg:ansiyellow bold", f"  {self._status}")]
        return [("", " ")]


# -- internal render helpers ------------------------------------------------

def _render_user(msg: _Msg, result: list[tuple[str, str]]) -> None:
    """User message: show as quoted text."""
    lines = msg.content.split("\n")
    result.append(("bold fg:ansicyan", "▸ "))
    for i, line in enumerate(lines):
        if i > 0:
            result.append(("", "\n  "))
        result.append(("", line))
    result.append(("", "\n"))


def _render_assistant(msg: _Msg, result: list[tuple[str, str]]) -> None:
    """Assistant message: render via Rich Markdown→ANSI.

    If content is empty (initial streaming state), show an empty response line.
    """
    text = msg.content
    if not text:
        # Streaming hasn't produced a token yet; show nothing
        return
    try:
        with _render_console.capture() as capture:
            _render_console.print(Markdown(text))
        ansi_str = capture.get()
        if ansi_str:
            result.extend(to_formatted_text(ANSI(ansi_str)))
    except Exception:
        # Fallback: render as plain text if Rich fails
        for line in text.split("\n"):
            result.append(("", line))
            result.append(("", "\n"))


def _render_tool(msg: _Msg, result: list[tuple[str, str]]) -> None:
    """Tool call/result: icon + label + dim content. Edit: diff colors."""
    is_edit = msg.tool_name == "Edit"
    preview = msg.tool_preview or ""
    # Split preview: first line is file path, rest is diff/extra
    preview_lines = preview.split("\n")
    label = f"↳ {msg.tool_name}({preview_lines[0]})" if preview_lines[0] else f"↳ {msg.tool_name}"

    if msg.tool_status == "pending":
        result.append(("fg:ansigray", f"  {label}\n"))
    elif msg.tool_status == "running":
        result.append(("fg:ansigray", f"  {label} …\n"))
    elif msg.tool_status == "done":
        icon = "✗" if msg.tool_result_is_error else "✓"
        color = "fg:ansired" if msg.tool_result_is_error else "fg:ansigreen"
        result.append((color, f"  {icon} "))
        result.append(("fg:ansigray", f"{label}\n"))
        # Diff lines for Edit tool
        if is_edit:
            import re as _re
            for line in preview_lines[1:]:
                if not line.strip():
                    continue
                # Strip ANSI codes, detect +/- markers, apply PT styles
                clean = _re.sub(r'\x1b\[[0-9;]*m', '', line).lstrip()
                if clean.startswith('-'):
                    result.append(("fg:ansired", f"  {clean}\n"))
                elif clean.startswith('+'):
                    result.append(("fg:ansigreen", f"  {clean}\n"))
                else:
                    result.append(("fg:ansigray dim", f"  {clean}\n"))
        elif msg.content:
            content = msg.content.replace("\t", "    ")[:500]
            for line in content.split("\n")[:5]:
                result.append(("fg:ansigray dim italic", f"     {line}\n"))
    elif msg.tool_status == "error":
        result.append(("fg:ansired", "  ✗ "))
        result.append(("fg:ansigray", f"{label}\n"))
        if msg.content:
            content = msg.content.replace("\t", "    ")[:200]
            result.append(("fg:ansired dim", f"     {content}\n"))


def _render_thinking(msg: _Msg, result: list[tuple[str, str]]) -> None:
    """Spinner + elapsed time indicator."""
    result.append(("fg:ansiyellow", msg.content))
    result.append(("", "\n"))


def _render_system(msg: _Msg, result: list[tuple[str, str]]) -> None:
    """System/info message — renders Rich markup via ANSI, or plain if flagged."""
    text = msg.content
    if not text:
        return
    if msg.tool_status == "plain":
        # Plain text from _DisplayConsole (command output) — no markup parsing
        result.append(("fg:ansigray", f"  {text}"))
        result.append(("", "\n"))
        return
    try:
        from rich.text import Text as RichText
        with _render_console.capture() as capture:
            _render_console.print(RichText.from_markup(text))
        ansi_str = capture.get()
        if ansi_str:
            result.extend(to_formatted_text(ANSI(ansi_str)))
    except Exception:
        # Fallback: plain text
        result.append(("fg:ansigray", f"  {text}"))
        result.append(("", "\n"))


# -- tool preview helpers (from rendering.py) --------------------------------

def _format_diff(old: str, new: str) -> str:
    """Format a diff between old and new strings."""
    import difflib
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    result = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        if tag == "equal":
            for l in old_lines[i1:i2]:
                result.append(f"    {l}")
        elif tag == "delete":
            for l in old_lines[i1:i2]:
                result.append(f"  \x1b[31m-{l}\x1b[0m")
        elif tag == "insert":
            for l in new_lines[j1:j2]:
                result.append(f"  \x1b[32m+{l}\x1b[0m")
        elif tag == "replace":
            for l in old_lines[i1:i2]:
                result.append(f"  \x1b[31m-{l}\x1b[0m")
            for l in new_lines[j1:j2]:
                result.append(f"  \x1b[32m+{l}\x1b[0m")
    return "\n".join(result)


def _tool_preview(tool_name: str, tool_input: dict) -> str:
    """Generate a short preview string for a tool invocation."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return cmd[:80] + ("…" if len(cmd) > 80 else "")
    if tool_name == "Read":
        fp = tool_input.get("file_path", "")
        offset = tool_input.get("offset")
        limit = tool_input.get("limit")
        if offset is not None and limit is not None:
            return f"{fp} (L{int(offset)}-{int(offset)+int(limit)})"
        return fp
    if tool_name == "Edit":
        fp = tool_input.get("file_path", "")
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        diff = _format_diff(old, new)
        return f"{fp}\n{diff}"
    if tool_name == "Write":
        return tool_input.get("file_path", "")
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
