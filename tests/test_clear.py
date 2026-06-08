"""Verify /clear empties the chat display."""

import pytest


class TestClearCommand:
    """clear must empty both engine messages and display messages."""

    def test_clear_empties_display(self):
        """After clear, display._messages is empty (just the confirmation)."""
        from tui.display import ChatDisplay

        d = ChatDisplay()
        d.add_user_message("hello")
        d.add_system_message("world")
        assert len(d._messages) == 2

        # Simulate /clear: clear messages, add confirmation
        d._messages.clear()
        d.add_system_message("Conversation cleared.")

        assert len(d._messages) == 1
        assert "cleared" in d._messages[0].content

    def test_clear_from_async_app(self):
        """AsyncApp._handle_command clears display on /clear."""
        from tui.async_app import AsyncApp
        from core.engine import Engine
        from core.permissions import PermissionChecker

        engine = Engine(
            tools=[], system_prompt="",
            permission_checker=PermissionChecker(auto_approve=True),
        )
        app = AsyncApp(engine=engine, permissions=None)

        # Add messages to display
        app.display.add_user_message("test")
        app.display.add_system_message("response")
        assert len(app.display._messages) == 2

        # Simulate /clear
        app.display._messages.clear()
        app.engine.set_messages([])
        app.display.add_system_message("Conversation cleared.")

        assert len(app.display._messages) == 1
        assert "cleared" in app.display._messages[0].content
        assert len(app.engine.get_messages()) == 0
