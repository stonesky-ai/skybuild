"""Preflight rejects invalid prerequisites before invoking a source interpreter."""

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/coding_preflight.py"
spec = importlib.util.spec_from_file_location("coding_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


@pytest.fixture
def checkout(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "https://github.com/stonesky-ai/skybuild.git"], check=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/contract.md").write_text("contract")
    source = tmp_path / "src/skybuild"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    return tmp_path


def test_combines_identity_discovery_ref_cache_and_exact_source(checkout, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/wrong/source")
    result = preflight.preflight(checkout, required=["docs/contract.md"], discover=["docs/*.md"],
                                 refs=["refs/heads/" + subprocess.check_output(
                                     ["git", "-C", str(checkout), "branch", "--show-current"], text=True).strip()],
                                 interpreter=sys.executable, cache=checkout / "new-cache")
    assert result["ok"]
    assert result["discovered"] == {"docs/*.md": {"count": 1, "paths": ["docs/contract.md"]}}
    assert result["imported_source"] == str(checkout / "src/skybuild/__init__.py")
    assert not (checkout / "new-cache").exists()


@pytest.mark.parametrize("failure", ["origin", "push_origin", "missing", "ref", "cache", "nested"])
def test_prerequisite_failure_prevents_interpreter(checkout, monkeypatch, failure):
    original = preflight._run
    invoked = []

    def run(path, arguments, **kwargs):
        invoked.append(arguments)
        return original(path, arguments, **kwargs)

    monkeypatch.setattr(preflight, "_run", run)
    kwargs = {"interpreter": sys.executable}
    selected = checkout
    if failure == "origin":
        subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin",
                        "https://github.com/stonesky-ai/skykeep.git"], check=True)
    elif failure == "push_origin":
        subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "--push", "origin",
                        "https://github.com/stonesky-ai/skykeep.git"], check=True)
    elif failure == "missing":
        kwargs["required"] = ["docs/invented.md"]
    elif failure == "ref":
        kwargs["refs"] = ["refs/remotes/origin/not-fetched"]
    elif failure == "cache":
        kwargs["cache"] = checkout / "cache"
        monkeypatch.setattr(preflight.os, "access", lambda *_: False)
    elif failure == "nested":
        selected = checkout / "docs"
    with pytest.raises(preflight.PreflightError):
        preflight.preflight(selected, **kwargs)
    assert not any(arguments[0] == sys.executable for arguments in invoked)


def test_wrong_interpreter_import_is_rejected(checkout, monkeypatch):
    original = preflight._run

    def run(path, arguments, **kwargs):
        if arguments[0] == sys.executable:
            return "/another/checkout/src/skybuild/__init__.py"
        return original(path, arguments, **kwargs)

    monkeypatch.setattr(preflight, "_run", run)
    with pytest.raises(preflight.PreflightError, match="source_import_mismatch"):
        preflight.preflight(checkout, interpreter=sys.executable)


def test_missing_runtime_and_import_failures_are_distinct(checkout):
    with pytest.raises(preflight.PreflightError, match="interpreter_unavailable"):
        preflight.preflight(checkout, interpreter=checkout / "missing-python")
    (checkout / "src/skybuild/__init__.py").write_text("raise RuntimeError('PRIVATE_SECRET')")
    with pytest.raises(preflight.PreflightError, match="source_import_failed") as caught:
        preflight.preflight(checkout, interpreter=sys.executable)
    assert "PRIVATE_SECRET" not in str(caught.value)


def test_discovery_is_bounded_and_untracked_files_do_not_appear(checkout):
    for index in range(30):
        (checkout / f"docs/{index}.md").write_text("")
    subprocess.run(["git", "-C", str(checkout), "add", "docs"], check=True)
    (checkout / "docs/private-untracked.md").write_text("")
    result = preflight.preflight(checkout, discover=["docs/*.md"])["discovered"]["docs/*.md"]
    assert result["count"] == 31 and len(result["paths"]) == 20
    assert "docs/private-untracked.md" not in result["paths"]


def test_missing_discovery_and_revision_expressions_fail(checkout):
    with pytest.raises(preflight.PreflightError, match="discovery_no_match"):
        preflight.preflight(checkout, discover=["invented/*"])
    with pytest.raises(preflight.PreflightError, match="explicit_ref_required"):
        preflight.preflight(checkout, refs=["HEAD~1"])
    branch = subprocess.check_output(
        ["git", "-C", str(checkout), "branch", "--show-current"], text=True
    ).strip()
    with pytest.raises(preflight.PreflightError, match="explicit_ref_required"):
        preflight.preflight(checkout, refs=[f"refs/heads/{branch}~0"])


def test_cli_failure_has_no_raw_subprocess_output(checkout):
    result = subprocess.run([sys.executable, str(SCRIPT), "--checkout", str(checkout),
                             "--ref", "refs/remotes/origin/unfetched"], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 2
    assert "ref_unavailable_fetch_explicitly" in result.stdout
    assert result.stderr == ""
