"""Verify input stays in user input TextArea, never leaks to chat buffer."""

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl
from prompt_toolkit.widgets import TextArea


class TestInputStaysInTextArea:
    """The user input area must always receive keyboard input."""

    def test_textarea_buffer_separate_from_chat_buffer(self):
        """Chat buffer and TextArea buffer are different objects."""
        chat_buf = Buffer(multiline=True)
        chat_ctrl = BufferControl(buffer=chat_buf, focusable=False)

        received = []
        def _accept(buf):
            received.append(buf.text)
            return True

        textarea = TextArea(
            height=1, prompt="> ", multiline=False,
            accept_handler=_accept,
        )

        # These are DIFFERENT buffers
        assert chat_buf is not textarea.buffer
        # Chat buffer starts empty
        assert chat_buf.text == ""
        # TextArea starts empty
        assert textarea.text == ""

    def test_typing_goes_to_textarea_not_chat(self):
        """Simulate: typing sets text on TextArea buffer, not chat buffer."""
        chat_buf = Buffer(multiline=True)
        chat_ctrl = BufferControl(buffer=chat_buf, focusable=False)

        received = []
        def _accept(buf):
            received.append(buf.text)
            return True

        textarea = TextArea(
            height=1, prompt="> ", multiline=False,
            accept_handler=_accept,
        )

        # Simulate user typing in the TextArea
        textarea.buffer.text = "hello world"
        textarea.buffer.validate_and_handle()

        # TextArea processed the input
        assert received == ["hello world"]
        # Chat buffer was NOT touched
        assert chat_buf.text == ""

    def test_formatted_text_control_never_receives_text(self):
        """FormattedTextControl has no buffer — it can't receive input."""
        ctrl = FormattedTextControl(text="output area", focusable=False)
        # No buffer attribute at all
        assert not hasattr(ctrl, 'buffer')
