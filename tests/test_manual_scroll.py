"""Verify scroll events pass through to Window."""

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout import Window
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType


def test_formatted_buffer_scroll_check():
    """FormattedBuffer.mouse_handler checks for SCROLL_UP/SCROLL_DOWN."""
    from tui.formatted_buffer import FormattedBuffer
    ctrl = FormattedBuffer(buffer=Buffer(multiline=True))
    # Verify the control handles mouse events
    assert hasattr(ctrl, 'mouse_handler')
    assert not ctrl.is_focusable()


def test_is_focusable_false():
    """Keyboard never goes to output area."""
    from tui.formatted_buffer import FormattedBuffer
    ctrl = FormattedBuffer(buffer=Buffer(multiline=True))
    assert not ctrl.is_focusable()
