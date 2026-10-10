from __future__ import annotations

import json
from pathlib import Path

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
