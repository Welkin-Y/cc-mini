"""Tests for the TUI rendering module."""
from __future__ import annotations

import pytest
from rich.console import Console
from io import StringIO

from tui.rendering import StreamingMarkdown, _close_unclosed_fences


def test_close_unclosed_fences():
    """Test that unclosed code fences are properly closed."""
    # Single unclosed fence
    text = "```python\nprint('hello')\n"
    result = _close_unclosed_fences(text)
    assert "```python\nprint('hello')\n```\n" == result

    # Multiple unclosed fences - only the last one is unclosed (first block properly closed)
    text = "```python\nx = 1\n```\n```rust\nfn main() {}\n"
    result = _close_unclosed_fences(text)
    assert "```python\nx = 1\n```\n```rust\nfn main() {}\n```\n" == result

    # Already closed fences should not be affected
    text = "```python\nprint('hello')\n```\n"
    result = _close_unclosed_fences(text)
    assert text == result


def test_close_unclosed_fences_with_language():
    """Test closing fences with language identifiers."""
    text = "```javascript\nconst x = 1;\n"
    result = _close_unclosed_fences(text)
    assert "```javascript\nconst x = 1;\n```\n" == result


def test_close_unclosed_fences_empty():
    """Test with empty string."""
    text = ""
    result = _close_unclosed_fences(text)
    assert result == ""


def test_streaming_markdown_basic():
    """Test basic streaming markdown rendering."""
    console = Console(file=StringIO(), force_terminal=True)
    md = StreamingMarkdown(console)

    md.feed("# Hello\n")
    md.feed("World\n")
    md.flush()

    # Should not raise any exceptions


def test_streaming_markdown_unclosed_fence():
    """Test that unclosed fences don't cause errors."""
    console = Console(file=StringIO(), force_terminal=True)
    md = StreamingMarkdown(console)

    # Feed markdown with an unclosed code fence
    md.feed("```python\nprint('hello')\n")
    md.flush()

    # Should not raise any exceptions


def test_streaming_markdown_multiple_unclosed_fences():
    """Test multiple unclosed fences."""
    console = Console(file=StringIO(), force_terminal=True)
    md = StreamingMarkdown(console)

    md.feed("```python\nx = 1\n")
    md.feed("```\n")
    md.feed("```rust\nfn main() {}\n")
    md.flush()

    # Should not raise any exceptions


def test_streaming_markdown_mixed():
    """Test mixed closed and unclosed fences."""
    console = Console(file=StringIO(), force_terminal=True)
    md = StreamingMarkdown(console)

    md.feed("# Title\n")
    md.feed("```python\nprint('hello')\n```\n")
    md.feed("Some text\n")
    md.feed("```rust\nfn main() {}\n")  # Unclosed
    md.flush()

    # Should not raise any exceptions


def test_streaming_markdown_incremental():
    """Test incremental rendering with streaming."""
    console = Console(file=StringIO(), force_terminal=True)
    md = StreamingMarkdown(console)

    # Simulate streaming chunks
    for chunk in ["# Hello\n", "World\n", "\n", "```python\n"]:
        md.feed(chunk)

    md.flush()

    # Should not raise any exceptions


def test_find_block_boundaries_blank_lines_in_code():
    """Test that blank lines inside code blocks are NOT treated as boundaries."""
    from tui.rendering import _find_block_boundaries
    
    text = "```python\nx = 1\n\ny = 2\n```\n"
    # Should find only the fence boundaries, not the blank line inside the code block
    boundaries = _find_block_boundaries(text, 0)
    
    # Expected: opening fence at position 0-10, closing fence at position 23-27
    assert len(boundaries) == 2
    
    # First boundary should be the opening fence (```python\n)
    assert boundaries[0] == (0, 10)
    
    # Second boundary should be the closing fence (```\n)
    # The code block is: ```python\nx = 1\n\ny = 2\n```
    # So closing fence starts at position 23 and ends at 27 (including newline)
    assert boundaries[1] == (23, 27)


def test_find_block_boundaries_multiple_code_blocks():
    """Test that blank lines BETWEEN separate code blocks ARE treated as boundaries."""
    from tui.rendering import _find_block_boundaries
    
    text = "```python\nx = 1\n```\n\n```rust\nfn main() {}\n```\n"
    # Should find: opening python fence, closing python fence, blank line separator, 
    # opening rust fence, closing rust fence, trailing newline
    boundaries = _find_block_boundaries(text, 0)
    
    assert len(boundaries) == 6
    
    # Opening python fence at position 0
    assert boundaries[0][0] == 0
    
    # Closing python fence - "```python\nx = 1\n" is 14 chars (including newline), so ``` starts at 14
    assert boundaries[1][0] == 14
    
    # Blank line separator after closing python fence and its trailing newline
    # Position 14-17: ```\n, position 18-19: \n\n (blank line)
    assert boundaries[2][0] == 18
    
    # Opening rust fence - after blank line and "fn main() {}\n"
    # Position 18-19: \n\n, 20-34: ```rust\nfn main() {}\n (15 chars), so ``` starts at 35
    assert boundaries[3][0] == 35
    
    # Closing rust fence - "```rust\nfn main() {}\n" is 19 chars from position 35, so ``` starts at 54
    assert boundaries[4][0] == 54
    
    # Trailing newline after closing rust fence
    assert boundaries[5][0] == 58


def test_find_block_boundaries_mixed_content():
    """Test mixed content with headings and code blocks."""
    from tui.rendering import _find_block_boundaries
    
    text = "# Title\n\n```python\ndef foo():\n    pass\n```\n\n## Subtitle\n"
    boundaries = _find_block_boundaries(text, 0)
    
    # Should find: blank line after title, opening fence, closing fence, 
    # blank line between code and subtitle, heading
    assert len(boundaries) == 5
    
    # Blank line after "# Title\n" (position 8-9)
    assert boundaries[0][0] == 8
    
    # Opening fence at position 10
    assert boundaries[1][0] == 10
    
    # Closing fence - "```python\ndef foo():\n    pass\n" is 26 chars, so ``` starts at 37
    assert boundaries[2][0] == 37
    
    # Blank line after closing fence and its trailing newline (position 41-42)
    assert boundaries[3][0] == 41
    
    # Heading "## Subtitle" at position 44
    assert boundaries[4][0] == 44


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
