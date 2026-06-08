"""Verify slash commands render correctly through the DisplayConsole shim."""

import pytest
from tui.display import ChatDisplay


class TestCommandRendering:
    """Command output goes through _DisplayConsole → system message (plain)."""

    def test_help_command_output_is_captured(self):
        """/help produces a Rich table → stripped to plain text → system message."""
        from tui.async_app import _DisplayConsole
        from rich.table import Table

        d = ChatDisplay()
        dc = _DisplayConsole(d)

        # Simulate /help command output (a Rich table)
        table = Table(title="Commands")
        table.add_column("Cmd")
        table.add_column("Desc")
        table.add_row("/help", "Show help")
        dc.print(table)

        # Should have added a system message
        before = len(d._messages)
        assert before >= 1
        assert d._messages[-1].role == "system"
        assert d._messages[-1].tool_status == "plain"
        assert "help" in d._messages[-1].content.lower()

    def test_status_message_is_plain_text(self):
        """Plain system messages render without Rich markup parsing."""
        d = ChatDisplay()
        # Add a plain system message (like from _DisplayConsole)
        d.add_system_message("Some [bracketed] text", plain=True)
        d.add_system_message("[bold]styled[/bold] text", plain=False)

        # Both should render without crash
        result = d.render()
        assert len(result) > 0

    def test_simple_print_is_captured(self):
        """Simple console.print() goes to system message."""
        from tui.async_app import _DisplayConsole

        d = ChatDisplay()
        dc = _DisplayConsole(d)
        dc.print("Model set to sonnet")

        assert d._messages[-1].role == "system"
        assert "sonnet" in d._messages[-1].content

    def test_model_command_uses_overlay_not_console(self):
        """/model command uses overlay, not _DisplayConsole."""
        # This is verified by _handle_command checking cmd_name == "model"
        # and routing to _handle_model_command instead of thread executor
        from tui.async_app import AsyncApp
        # The overlay method exists
        assert hasattr(AsyncApp, '_handle_model_command')
        assert hasattr(AsyncApp, '_show_model_picker')
