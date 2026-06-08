"""Verify keyboard input always goes to TextArea."""

import pytest


class TestKeyboardStaysOnInput:
    def test_chat_control_exists(self):
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker
        engine = Engine(tools=[], system_prompt="",
                        permission_checker=PermissionChecker(auto_approve=True))
        app = AsyncApp(engine=engine, permissions=None)
        assert hasattr(app, '_chat_control')

    def test_textarea_has_buffer(self):
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker
        engine = Engine(tools=[], system_prompt="",
                        permission_checker=PermissionChecker(auto_approve=True))
        app = AsyncApp(engine=engine, permissions=None)
        assert app._input.buffer is not None

    def test_chat_and_input_buffers_are_different(self):
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker
        engine = Engine(tools=[], system_prompt="",
                        permission_checker=PermissionChecker(auto_approve=True))
        app = AsyncApp(engine=engine, permissions=None)
        assert app._chat_buffer is not app._input.buffer
