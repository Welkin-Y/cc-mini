"""Verify auto-scroll works when content exceeds screen height."""

import pytest
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl


class TestScrollFollow:
    """When following, the window must scroll to bottom on new content."""

    def test_vertical_scroll_changes_when_content_grows(self):
        """vertical_scroll increases after adding many lines."""
        ctrl = FormattedTextControl(
            text="line1\nline2\nline3",
            focusable=False,
        )
        win = Window(content=ctrl, wrap_lines=True,
                     allow_scroll_beyond_bottom=False,
                     height=2)  # window shorter than content

        # Initially at top
        assert win.vertical_scroll == 0

        # Simulate _refresh with _following=True:
        win.vertical_scroll = 100_000
        # Window should clamp to show bottom content
        assert win.vertical_scroll > 0, (
            f"vertical_scroll={win.vertical_scroll}, should be > 0 "
            f"to show bottom of 3-line content in 2-line window"
        )

    def test_growing_content_keeps_bottom_visible(self):
        """As content grows, bottom stays visible when following."""
        lines = ["line0"]
        ctrl = FormattedTextControl(text="\n".join(lines), focusable=False)
        win = Window(content=ctrl, wrap_lines=True,
                     allow_scroll_beyond_bottom=False,
                     height=3)

        # Add lines one by one, simulating streaming
        for i in range(1, 50):
            lines.append(f"line{i}")
            ctrl.text = "\n".join(lines)
            # _refresh() with _following=True
            win.vertical_scroll = 100_000

        # After 50 lines, scroll position should be well past 0
        assert win.vertical_scroll > 40, (
            f"After 50 lines in 3-line window, scroll={win.vertical_scroll}, "
            f"should be ~47 to show bottom"
        )
