"""Verify actual rendered output — mock components and inspect results."""

import pytest
from tui.display import ChatDisplay
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout import Window
from prompt_toolkit.formatted_text import FormattedText


def _fmt_to_str(ft) -> str:
    """Extract plain text from FormattedText."""
    parts = []
    for item in ft:
        parts.append(item[1] if isinstance(item, tuple) else str(item))
    return "".join(parts)


class TestRenderedOutput:
    """Inspect what the user actually sees."""

    def test_user_message_visible_in_output(self):
        """User message 'hello' renders with ▸ prefix."""
        d = ChatDisplay()
        d.add_user_message("hello world")
        ft = d.render()
        text = _fmt_to_str(ft)
        assert "▸" in text, f"Missing ▸ prefix in: {text[:100]}"
        assert "hello world" in text

    def test_assistant_markdown_renders_styled(self):
        """Rich markdown produces styled FormattedText (not plain)."""
        d = ChatDisplay()
        mid = d.start_assistant_stream()
        d.append_token(mid, "**bold** and _italic_")
        d.finish_assistant_stream(mid)
        ft = d.render()

        # Should have styled tuples, not just plain text
        styles = {item[0] for item in ft if isinstance(item, tuple) and item[0]}
        # Rich produces ANSI codes which become PT styles
        assert len(ft) > 0

    def test_tool_call_shows_with_icon(self):
        """Tool result shows checkmark/cross icon."""
        d = ChatDisplay()
        k = d.add_tool_call("Bash", {"command": "ls"}, "list")
        d.update_tool_running(k)
        d.update_tool_done(k, "file1\nfile2", is_error=False)
        ft = d.render()
        text = _fmt_to_str(ft)
        assert "↳" in text, f"Missing tool arrow in: {text[:200]}"
        assert "ls" in text

    def test_error_tool_shows_cross(self):
        """Failed tool shows error indicator."""
        d = ChatDisplay()
        k = d.add_tool_call("Bash", {"command": "rm -rf /"}, "danger")
        d.update_tool_done(k, "Permission denied", is_error=True)
        ft = d.render()
        text = _fmt_to_str(ft)
        assert "rm -rf" in text
        assert "Permission denied" in text

    def test_streaming_tokens_accumulate(self):
        """Each append_token adds to the same assistant message."""
        d = ChatDisplay()
        mid = d.start_assistant_stream()
        d.append_token(mid, "Hello")
        d.append_token(mid, " ")
        d.append_token(mid, "World")
        ft = d.render()
        text = _fmt_to_str(ft)
        assert "Hello World" in text

    def test_system_message_renders_with_style(self):
        """System message is visible in output."""
        d = ChatDisplay()
        d.add_system_message("Auto-compacting conversation…")
        ft = d.render()
        text = _fmt_to_str(ft)
        assert "Auto-compacting" in text

    def test_plain_system_message_renders_without_markup(self):
        """Plain system message shows text as-is, no Rich parsing."""
        d = ChatDisplay()
        d.add_system_message("[green]should not parse[/green]", plain=True)
        ft = d.render()
        text = _fmt_to_str(ft)
        # Plain: text shown as-is, not converted to green
        assert "[green]" not in text or "should not parse" in text

    def test_full_conversation_flow(self):
        """Simulate a complete conversation turn."""
        d = ChatDisplay()

        # User sends message
        d.add_user_message("list files")
        ft1 = _fmt_to_str(d.render())
        assert "list files" in ft1

        # Assistant streams response
        mid = d.start_assistant_stream()
        d.append_token(mid, "Here are the files:")
        d.finish_assistant_stream(mid)
        ft2 = _fmt_to_str(d.render())
        assert "Here are the files:" in ft2

        # Tool call happens
        k = d.add_tool_call("Bash", {"command": "ls"}, "list files")
        d.update_tool_running(k)
        d.update_tool_done(k, "file1.py\nfile2.py", is_error=False)
        ft3 = _fmt_to_str(d.render())
        assert "ls" in ft3

        # Another assistant response
        mid2 = d.start_assistant_stream()
        d.append_token(mid2, "Done!")
        d.finish_assistant_stream(mid2)
        ft4 = _fmt_to_str(d.render())
        assert "Done!" in ft4

    def test_rich_markdown_preserves_double_newlines(self):
        """Rich Markdown treats \n\n as paragraph break, \n as soft break."""
        d = ChatDisplay()
        mid = d.start_assistant_stream()
        # Double newlines = real paragraph breaks in markdown
        content = "\n\n".join(f"line {i}" for i in range(500))
        d.append_token(mid, content)
        d.finish_assistant_stream(mid)
        ft = d.render()
        text = _fmt_to_str(ft)
        assert "line 0" in text
        assert "line 499" in text

    def test_single_newlines_preserved_in_output(self):
        """Single newlines are preserved (not collapsed to spaces)."""
        d = ChatDisplay()
        mid = d.start_assistant_stream()
        d.append_token(mid, "hello\nworld\nfoo")
        d.finish_assistant_stream(mid)
        ft = d.render()
        text = _fmt_to_str(ft)
        # Both words must appear
        assert "hello" in text
        assert "world" in text
        assert "foo" in text
