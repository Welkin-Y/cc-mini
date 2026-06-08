"""Verify FormattedBuffer — custom UIControl for output area."""

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import to_formatted_text


class TestFormattedBuffer:
    """FormattedBuffer: Rich styling + selection + scroll, no focus steal."""

    def test_construction(self):
        from tui.formatted_buffer import FormattedBuffer
        ctrl = FormattedBuffer(buffer=Buffer(multiline=True))
        assert ctrl is not None
        assert not ctrl.is_focusable()

    def test_styled_text_property(self):
        from tui.formatted_buffer import FormattedBuffer
        ctrl = FormattedBuffer(buffer=Buffer(multiline=True))
        ft = to_formatted_text([("bold", "test")])
        ctrl.styled_text = ft
        assert ctrl.styled_text == ft

    def test_default_styled_text_empty(self):
        from tui.formatted_buffer import FormattedBuffer
        ctrl = FormattedBuffer(buffer=Buffer(multiline=True))
        assert ctrl.styled_text == []

    def test_create_content_line_count(self):
        """UIContent line count matches buffer lines."""
        from tui.formatted_buffer import FormattedBuffer
        buf = Buffer(multiline=True)
        buf.text = "hello\nworld"
        ctrl = FormattedBuffer(buffer=buf)
        content = ctrl.create_content(80)
        assert content.line_count == 2

    def test_split_lines(self):
        from tui.formatted_buffer import _split_lines
        ft = to_formatted_text([("bold", "a\nb\nc")])
        result = _split_lines(ft)
        assert len(result) == 3
