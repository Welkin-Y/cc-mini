"""Versionable chat history — checkpoint + branch with workspace tie-in.

Checkpoints capture both the conversation state (message index) and the
workspace state (git HEAD, dirty files) at a point in time. Branches allow
forking the conversation from any checkpoint.

Storage: ``~/.config/cc-mini/checkpoints/<session_id>/``
  - ``checkpoints.jsonl`` — one JSON object per checkpoint
  - ``branches.json``   — branch metadata (name → tip checkpoint id)
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from session.models import SessionCheckpoint, _now_ms
from session.session import Session

_CHECKPOINTS_ROOT = Path.home() / ".config" / "cc-mini" / "checkpoints"


# ---------------------------------------------------------------------------
# Git helpers — gather workspace state at checkpoint time
# ---------------------------------------------------------------------------

def _git_sha(cwd: str | None = None) -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                           text=True, cwd=cwd, timeout=5)
        return r.stdout.strip()[:8] if r.returncode == 0 else None
    except Exception:
        return None


def _git_branch(cwd: str | None = None) -> str | None:
    try:
        r = subprocess.run(["git", "branch", "--show-current"], capture_output=True,
                           text=True, cwd=cwd, timeout=5)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def _git_status_short(cwd: str | None = None) -> list[str]:
    try:
        r = subprocess.run(["git", "status", "--short"], capture_output=True,
                           text=True, cwd=cwd, timeout=5)
        if r.returncode == 0:
            return [line.strip() for line in r.stdout.splitlines() if line.strip()]
        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# CheckpointStore
# ---------------------------------------------------------------------------

class CheckpointStore:
    """Manages checkpoint persistence for a session.

    Parameters
    ----------
    session_id : str
        The session this store belongs to.
    cwd : str | None
        Working directory for git workspace info. If None, git data is skipped.
    """

    def __init__(self, session_id: str, cwd: str | None = None) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self._dir = _CHECKPOINTS_ROOT / session_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._jsonl = self._dir / "checkpoints.jsonl"
        self._branches_file = self._dir / "branches.json"

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def save(self, session: Session, label: str, parent_checkpoint_id: str | None = None) -> SessionCheckpoint:
        """Snapshot current session state + git workspace info as a checkpoint."""
        cwd = self.cwd or os.getenv("CC_MINI_CWD") or str(Path.cwd())

        cp = SessionCheckpoint(
            id=uuid.uuid4().hex[:12],
            label=label,
            message_index=len(session._messages),
            message_count=len(session._messages),
            parent_checkpoint_id=parent_checkpoint_id,
            branch=self._current_branch(),
            git_sha=_git_sha(cwd),
            git_branch=_git_branch(cwd),
            git_dirty=len(_git_status_short(cwd)) > 0,
            git_files_changed=_git_status_short(cwd),
        )

        with open(self._jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(_checkpoint_to_dict(cp), ensure_ascii=False) + "\n")

        # Update branch tip
        self._set_branch_tip(cp.branch, cp.id)

        return cp

    def list(self) -> list[SessionCheckpoint]:
        """List all checkpoints for this session, oldest first."""
        cps: list[SessionCheckpoint] = []
        if not self._jsonl.exists():
            return cps
        with open(self._jsonl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    cps.append(_checkpoint_from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError):
                    continue
        return cps

    def get(self, checkpoint_id: str) -> SessionCheckpoint | None:
        """Look up a single checkpoint by id."""
        for cp in self.list():
            if cp.id == checkpoint_id:
                return cp
        return None

    def delete(self, checkpoint_id: str) -> bool:
        """Remove a checkpoint. Returns True if found and deleted."""
        cps = self.list()
        filtered = [cp for cp in cps if cp.id != checkpoint_id]
        if len(filtered) == len(cps):
            return False
        # Rewrite file without the deleted checkpoint
        with open(self._jsonl, "w", encoding="utf-8") as f:
            for cp in filtered:
                f.write(json.dumps(_checkpoint_to_dict(cp), ensure_ascii=False) + "\n")
        return True

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def restore(self, session: Session, checkpoint_id: str) -> int:
        """Roll back the session to the state at checkpoint time.

        Returns the number of messages removed.
        """
        cp = self.get(checkpoint_id)
        if cp is None:
            return 0
        removed = len(session._messages) - cp.message_index
        if removed <= 0:
            return 0
        # Truncate messages
        session._messages = session._messages[:cp.message_index]
        # Clean up tool index
        stale_ids = [tid for tid, (idx, _) in session._tool_index.items()
                     if idx >= cp.message_index]
        for tid in stale_ids:
            session._tool_index.pop(tid, None)
            session._tool_results.pop(tid, None)
        session._current_turn_start = None
        return removed

    # ------------------------------------------------------------------
    # Branching
    # ------------------------------------------------------------------

    def branches(self) -> list[str]:
        """List all branch names."""
        data = self._load_branches()
        return list(data.keys())

    def create_branch(self, session: Session, checkpoint_id: str, branch_name: str) -> SessionCheckpoint:
        """Fork a new branch from a checkpoint. Creates a checkpoint on the new branch."""
        cp = self.get(checkpoint_id)
        if cp is None:
            raise ValueError(f"Checkpoint not found: {checkpoint_id}")

        # Create a new checkpoint on the new branch, pointing back to parent
        label = f"branch: {branch_name} (from {cp.label})"
        new_cp = SessionCheckpoint(
            id=uuid.uuid4().hex[:12],
            label=label,
            message_index=cp.message_index,
            message_count=cp.message_count,
            parent_checkpoint_id=checkpoint_id,
            branch=branch_name,
            git_sha=cp.git_sha,
            git_branch=cp.git_branch,
            git_dirty=cp.git_dirty,
            git_files_changed=list(cp.git_files_changed),
        )

        with open(self._jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(_checkpoint_to_dict(new_cp), ensure_ascii=False) + "\n")

        self._set_branch_tip(branch_name, new_cp.id)
        return new_cp

    def switch_branch(self, session: Session, branch_name: str) -> int:
        """Switch session to the tip of a branch.

        Restores the session to the last checkpoint on that branch.
        Returns the number of messages removed.
        """
        tip_id = self._get_branch_tip(branch_name)
        if tip_id is None:
            raise ValueError(f"Branch not found: {branch_name}")
        return self.restore(session, tip_id)

    def _current_branch(self) -> str:
        # Return the last-used branch, or "main"
        data = self._load_branches()
        if "main" in data:
            return "main"
        if data:
            return next(iter(data))
        return "main"

    # ------------------------------------------------------------------
    # Branch file I/O
    # ------------------------------------------------------------------

    def _load_branches(self) -> dict[str, str]:
        if not self._branches_file.exists():
            return {}
        try:
            return json.loads(self._branches_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_branches(self, data: dict[str, str]) -> None:
        self._branches_file.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    def _get_branch_tip(self, branch_name: str) -> str | None:
        return self._load_branches().get(branch_name)

    def _set_branch_tip(self, branch_name: str, checkpoint_id: str) -> None:
        data = self._load_branches()
        # Initialize "main" with first checkpoint only when it is on main
        if not data and branch_name == "main":
            data["main"] = checkpoint_id
        data[branch_name] = checkpoint_id
        self._save_branches(data)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _checkpoint_to_dict(cp: SessionCheckpoint) -> dict[str, Any]:
    return {
        "id": cp.id,
        "label": cp.label,
        "timestamp": cp.timestamp,
        "message_index": cp.message_index,
        "message_count": cp.message_count,
        "parent_checkpoint_id": cp.parent_checkpoint_id,
        "branch": cp.branch,
        "git_sha": cp.git_sha,
        "git_branch": cp.git_branch,
        "git_dirty": cp.git_dirty,
        "git_files_changed": cp.git_files_changed,
        "meta": cp.meta,
    }


def _checkpoint_from_dict(d: dict[str, Any]) -> SessionCheckpoint:
    return SessionCheckpoint(
        id=d.get("id", ""),
        label=d.get("label", ""),
        timestamp=d.get("timestamp", _now_ms()),
        message_index=d.get("message_index", 0),
        message_count=d.get("message_count", 0),
        parent_checkpoint_id=d.get("parent_checkpoint_id"),
        branch=d.get("branch", "main"),
        git_sha=d.get("git_sha"),
        git_branch=d.get("git_branch"),
        git_dirty=d.get("git_dirty", False),
        git_files_changed=d.get("git_files_changed", []),
        meta=d.get("meta", {}),
    )


# ---------------------------------------------------------------------------
# Global convenience
# ---------------------------------------------------------------------------

def get_store(session_id: str, cwd: str | None = None) -> CheckpointStore:
    """Get or create a CheckpointStore for a session."""
    return CheckpointStore(session_id=session_id, cwd=cwd)
