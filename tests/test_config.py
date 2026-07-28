"""Settings validation, focused on the ADR-010 workspace boundary."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.api.app.config import REPO_ROOT, Settings


def test_default_worktree_root_is_outside_the_checkout() -> None:
    settings = Settings()
    assert REPO_ROOT not in settings.worktree_root.parents
    assert settings.worktree_root != REPO_ROOT


def test_worktree_root_is_resolved_to_an_absolute_path(tmp_path: Path) -> None:
    settings = Settings(worktree_root=tmp_path / "workspaces")
    assert settings.worktree_root.is_absolute()


def test_worktree_root_inside_the_checkout_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must be outside the repository"):
        Settings(worktree_root=REPO_ROOT / "worktrees")


def test_the_checkout_itself_is_rejected_as_a_worktree_root() -> None:
    with pytest.raises(ValidationError, match="must be outside the repository"):
        Settings(worktree_root=REPO_ROOT)
