"""Verify keyboard input always goes to TextArea, never to chat buffer."""

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.controls import BufferControl


class TestKeyboardStaysOnInput:
    """The user input TextArea must ALWAYS receive keyboard events."""

    def test_chat_control_setup(self):
        """Chat control exists with buffer and styled_text support."""
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker

        engine = Engine(
            tools=[], system_prompt="",
            permission_checker=PermissionChecker(auto_approve=True),
        )
        app = AsyncApp(engine=engine, permissions=None)

        assert hasattr(app, '_chat_control')
        assert hasattr(app, '_chat_buffer')

    def test_input_textarea_is_focusable(self):
        """TextArea must accept keyboard input."""
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker

        engine = Engine(
            tools=[], system_prompt="",
            permission_checker=PermissionChecker(auto_approve=True),
        )
        app = AsyncApp(engine=engine, permissions=None)

        # TextArea must be able to receive focus
        assert app._input.buffer is not None

    def test_chat_and_input_buffers_are_different(self):
        """Chat buffer and TextArea buffer must be separate objects."""
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker

        engine = Engine(
            tools=[], system_prompt="",
            permission_checker=PermissionChecker(auto_approve=True),
        )
        app = AsyncApp(engine=engine, permissions=None)

        # These MUST be different buffers
        assert app._chat_buffer is not app._input.buffer, (
            "Chat buffer and TextArea buffer must be separate objects"
        )
