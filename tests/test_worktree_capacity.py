"""Cross-process worktree capacity reservations."""
from pathlib import Path
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _worktree_capacity as capacity


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@localhost"], cwd=root, check=True)
    (root / "tracked.txt").write_text("test\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True,
                   capture_output=True, text=True)
    return root


def test_capacity_reserve_refuses_insufficient_slots(repository, monkeypatch):
    monkeypatch.setattr(capacity, "_worktree_count", lambda _: 63)
    with pytest.raises(capacity.WorktreeCapacityError, match="Need 2 free worktree slot"):
        with capacity.reserve_worktree_slots(repository, 2):
            pytest.fail("over-capacity reservation was granted")


def test_capacity_lock_serializes_concurrent_creation_sections(repository, monkeypatch):
    monkeypatch.setattr(capacity, "_worktree_count", lambda _: 63)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    failures = []

    def first():
        try:
            with capacity.reserve_worktree_slots(repository, 1):
                first_entered.set()
                if not release_first.wait(3):
                    raise TimeoutError("test did not release first reservation")
        except Exception as error:
            failures.append(error)

    def second():
        try:
            with capacity.reserve_worktree_slots(repository, 1):
                second_entered.set()
        except Exception as error:
            failures.append(error)

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    assert first_entered.wait(2)
    second_thread.start()
    assert not second_entered.wait(0.1)
    release_first.set()
    assert second_entered.wait(2)
    first_thread.join(2)
    second_thread.join(2)
    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert not failures


def test_integration_lock_allows_preparation_but_serializes_integrators(repository):
    prepared = threading.Event()
    second_integrator = threading.Event()
    failures = []

    def prepare():
        try:
            with capacity.reserve_worktree_slots(repository, 2):
                prepared.set()
        except Exception as error:
            failures.append(error)

    def integrate():
        try:
            with capacity.serialize_integrations(repository):
                second_integrator.set()
        except Exception as error:
            failures.append(error)

    with capacity.serialize_integrations(repository):
        with capacity.reserve_worktree_slots(repository, 1):
            pass  # Candidate registration has completed; the full gate may run.
        preparer = threading.Thread(target=prepare, daemon=True)
        integrator = threading.Thread(target=integrate, daemon=True)
        preparer.start()
        integrator.start()
        assert prepared.wait(2)
        assert not second_integrator.wait(0.1)
    assert second_integrator.wait(2)
    preparer.join(2)
    integrator.join(2)
    assert not preparer.is_alive() and not integrator.is_alive()
    assert not failures
