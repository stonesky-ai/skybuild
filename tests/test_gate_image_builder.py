from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

import build_isolated_gate_images as builder
from gate_images.write_runner_environment import write_environment


def test_debian_git_package_manifest_is_pinned_to_the_python_base():
    manifest = json.loads(builder.GIT_PACKAGE_MANIFEST.read_bytes())
    assert manifest["schema"] == "skybuild.isolated-gate-debian-git-packages.v1"
    assert manifest["base_image_id"] == builder.PYTHON_BASE_ID
    assert manifest["repository_suite"] == "Debian trixie"
    assert {row["name"] for row in manifest["packages"]} >= {"git", "git-man", "perl-base"}
    assert all(len(row["sha256"]) == 64 for row in manifest["packages"])


def test_git_package_payload_rejects_unpinned_or_missing_archives(tmp_path):
    package_dir = tmp_path / "packages"
    package_dir.mkdir()
    (package_dir / "git.deb").write_bytes(b"untrusted")

    with pytest.raises(builder.BuildError, match="exact package allowlist"):
        builder.stage_git_payload(tmp_path / "rootfs", package_dir)


def test_git_package_staging_uses_private_hashed_copy_and_rejects_symlinks(tmp_path):
    source = tmp_path / "source.deb"
    source.write_bytes(b"trusted package bytes")
    staged = tmp_path / "private" / "package.deb"
    staged.parent.mkdir(mode=0o700)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()

    builder._copy_pinned_package(source, staged, expected)
    source.write_bytes(b"replacement package bytes")

    assert staged.read_bytes() == b"trusted package bytes"
    assert stat.S_IMODE(staged.stat().st_mode) == 0o400
    link = tmp_path / "link.deb"
    link.symlink_to(source)
    with pytest.raises(builder.BuildError, match="opened safely"):
        builder._copy_pinned_package(link, tmp_path / "private" / "link.deb", expected)
    fifo = tmp_path / "race.deb"
    os.mkfifo(fifo)
    with pytest.raises(builder.BuildError, match="regular file"):
        builder._copy_pinned_package(fifo, tmp_path / "private" / "race.deb", expected)


def test_runner_python_link_is_checked_against_pinned_image_path(tmp_path):
    binary_dir = tmp_path / "skybuild-venv" / "bin"
    binary_dir.mkdir(parents=True)
    python = binary_dir / "python"
    python.symlink_to("/usr/local/bin/python3.14")

    assert builder._has_pinned_runner_python(tmp_path)
    python.unlink()
    python.symlink_to("/usr/bin/python3")
    assert not builder._has_pinned_runner_python(tmp_path)


def test_runner_image_copies_prepared_environment_at_entrypoint_path():
    checkout = Path(builder.__file__).resolve().parents[1]
    dockerfile = (checkout / "scripts/gate_images/Dockerfile.runner").read_text()

    assert "COPY runner-environment/skybuild-venv/ /opt/skybuild-venv/" in dockerfile
    assert "COPY runner-environment/ /opt/skybuild-venv/" not in dockerfile


def test_image_build_is_offline_and_memory_cpu_bounded(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1:3] == ["image", "inspect"]:
            return SimpleNamespace(stdout=json.dumps([{"Id": "sha256:" + "a" * 64}]))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(builder, "_run", fake_run)
    digest = builder._build_image(tmp_path, "Dockerfile.runner", "runner:test",
                                  builder.PYTHON_BASE_ID, {})

    assert digest == "sha256:" + "a" * 64
    command, kwargs = calls[0]
    assert command[:2] == ["docker", "build"]
    assert "--pull=false" in command and "--network=none" in command
    assert "--memory=2g" in command and "--memory-swap=2g" in command
    assert "--cpu-period=100000" in command and "--cpu-quota=100000" in command
    assert kwargs["timeout"] == 1800


def test_runner_environment_metadata_is_static_and_binds_exact_project_lock(tmp_path):
    project = tmp_path / "project"
    environment = tmp_path / "environment"
    site_packages = environment / "lib/python3.14/site-packages"
    project.mkdir()
    environment.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname="skybuild"\nversion="0.1.0"\nrequires-python=">=3.12"\n',
        encoding="utf-8",
    )
    (project / "uv.lock").write_text('version = 1\nrevision = 3\nrequires-python = ">=3.12"\n',
                                      encoding="utf-8")

    manifest = write_environment(project, environment, site_packages, "0.11.22")

    assert manifest["schema"] == "skybuild.full-test.environment.v1"
    assert json.loads((environment / ".skybuild-environment.json").read_text()) == manifest
    dist_info = site_packages / "skybuild-0.1.0.dist-info"
    assert json.loads((dist_info / "uv_build.json").read_text()) == {}
    assert json.loads((dist_info / "direct_url.json").read_text())["dir_info"] == {"editable": True}
    assert (site_packages / "_editable_impl_skybuild.pth").read_text().endswith("/src\n")


@pytest.mark.parametrize("project_fields", [
    '[project]\nname="skybuild"\nversion="0.1.0"\ndynamic=["version"]\n',
    '[project]\nname="other"\nversion="0.1.0"\n',
])
def test_runner_environment_metadata_rejects_dynamic_or_wrong_project(tmp_path, project_fields):
    project = tmp_path / "project"
    environment = tmp_path / "environment"
    project.mkdir()
    environment.mkdir()
    (project / "pyproject.toml").write_text(project_fields, encoding="utf-8")
    (project / "uv.lock").write_text('version = 1\nrevision = 3\n', encoding="utf-8")

    with pytest.raises(ValueError):
        write_environment(project, environment, environment / "lib/site-packages", "0.11.22")
