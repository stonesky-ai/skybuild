from __future__ import annotations

import json
from pathlib import Path

import pytest

from gate_images.write_runner_environment import write_environment


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
