"""Tests for CheckpointStore — save, list, restore, branch, switch."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from session.checkpoint import CheckpointStore, _git_sha, _git_branch, _git_status_short
from session.models import TextBlock, ToolCallBlock, ToolResult
from session.session import Session


@pytest.fixture
def tmp_store():
    """Create a CheckpointStore in a temp directory."""
    with tempfile.TemporaryDirectory() as td:
        # Patch the root so checkpoints go to temp
        with patch("session.checkpoint._CHECKPOINTS_ROOT", Path(td)):
            store = CheckpointStore(session_id="test-session", cwd=None)
            yield store


@pytest.fixture
def populated_session():
    s = Session(session_id="test-session")
    s.add_user_message("hello")
    s.add_assistant_message([TextBlock(text="hi there")])
    return s


class TestCheckpointCRUD:
    def test_save_and_list(self, tmp_store, populated_session):
        cp = tmp_store.save(populated_session, "first snapshot")
        assert cp.label == "first snapshot"
        assert cp.message_index == 2  # two messages

        cps = tmp_store.list()
        assert len(cps) == 1
        assert cps[0].id == cp.id

    def test_multiple_checkpoints(self, tmp_store, populated_session):
        tmp_store.save(populated_session, "cp1")
        populated_session.add_user_message("q2")
        populated_session.add_assistant_message([TextBlock(text="a2")])
        tmp_store.save(populated_session, "cp2")

        cps = tmp_store.list()
        assert len(cps) == 2
        assert cps[1].message_count == 4  # later checkpoint has more messages

    def test_get(self, tmp_store, populated_session):
        cp = tmp_store.save(populated_session, "test")
        found = tmp_store.get(cp.id)
        assert found is not None
        assert found.label == "test"

    def test_get_nonexistent(self, tmp_store):
        assert tmp_store.get("no-such-id") is None

    def test_delete(self, tmp_store, populated_session):
        cp = tmp_store.save(populated_session, "to-delete")
        assert tmp_store.delete(cp.id)
        assert len(tmp_store.list()) == 0

    def test_delete_nonexistent(self, tmp_store):
        assert not tmp_store.delete("no-such-id")


class TestRestore:
    def test_restore_rolls_back_messages(self, tmp_store, populated_session):
        cp = tmp_store.save(populated_session, "snap")
        # Add more messages
        populated_session.add_user_message("extra")
        populated_session.add_assistant_message([TextBlock(text="extra reply")])
        assert len(populated_session._messages) == 4

        n = tmp_store.restore(populated_session, cp.id)
        assert n == 2
        assert len(populated_session._messages) == 2

    def test_restore_nonexistent(self, tmp_store, populated_session):
        assert tmp_store.restore(populated_session, "no-such") == 0

    def test_restore_cleans_tool_index(self, tmp_store):
        s = Session(session_id="test-session")
        s.add_user_message("run")
        tc = ToolCallBlock(tool_use_id="tu_1", name="Read")
        s.add_assistant_message([tc])
        s.add_tool_result("tu_1", "result")

        cp = tmp_store.save(s, "before-more")
        s.add_user_message("more")
        s.add_assistant_message([TextBlock(text="done")])

        assert s.get_tool_call("tu_1") is not None
        tmp_store.restore(s, cp.id)
        # Tool call should survive since it was before the checkpoint
        assert s.get_tool_call("tu_1") is not None


class TestBranching:
    def test_branches_start_with_main(self, tmp_store, populated_session):
        tmp_store.save(populated_session, "init")
        assert "main" in tmp_store.branches()

    def test_create_branch(self, tmp_store, populated_session):
        cp = tmp_store.save(populated_session, "base")
        new_cp = tmp_store.create_branch(populated_session, cp.id, "experiment")
        assert new_cp.branch == "experiment"
        assert "experiment" in tmp_store.branches()

    def test_switch_branch(self, tmp_store, populated_session):
        # Save on main
        cp_main = tmp_store.save(populated_session, "main-cp")
        # Add messages
        populated_session.add_user_message("on main")
        populated_session.add_assistant_message([TextBlock(text="main reply")])

        # Create branch from main checkpoint
        tmp_store.create_branch(populated_session, cp_main.id, "alt")

        # Switch to alt — should rollback to cp_main point
        n = tmp_store.switch_branch(populated_session, "alt")
        assert n == 2  # the two extra messages removed

    def test_branch_nonexistent(self, tmp_store, populated_session):
        with pytest.raises(ValueError, match="Branch not found"):
            tmp_store.switch_branch(populated_session, "no-such-branch")


class TestGitHelpers:
    def test_git_sha_returns_str_or_none(self):
        result = _git_sha()
        assert result is None or isinstance(result, str)

    def test_git_branch_returns_str_or_none(self):
        result = _git_branch()
        assert result is None or isinstance(result, str)

    def test_git_status_short_returns_list(self):
        result = _git_status_short()
        assert isinstance(result, list)
