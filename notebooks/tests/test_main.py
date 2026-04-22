"""Tests for main module."""


def test_main() -> None:
    """Test the main function prints Hello, World!."""
    import subprocess

    result = subprocess.run(
        ["python", "-m", "src.main"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "Hello, World!"
