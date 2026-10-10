#!/usr/bin/env python3
"""Write static editable-project metadata without importing or building SkyBuild."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tomllib


def write_environment(project: Path, environment: Path, site_packages: Path, uv_version: str) -> dict:
    project = project.resolve(strict=True)
    environment = environment.resolve(strict=True)
    site_packages.mkdir(parents=True, exist_ok=True)
    config = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = config.get("project")
    if not isinstance(metadata, dict) or metadata.get("name") != "skybuild":
        raise ValueError("runner metadata requires the reviewed SkyBuild project")
    if metadata.get("dynamic") or (project / "setup.py").exists() or (project / "setup.cfg").exists():
        raise ValueError("dynamic project metadata requires separate runner qualification")
    version = metadata.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.!+_-]+", version):
        raise ValueError("project version is not a static safe value")
    if uv_version != "0.11.22":
        raise ValueError("runner cache protocol is reviewed only for uv 0.11.22")
    pyproject = (project / "pyproject.toml").read_bytes()
    lock = (project / "uv.lock").read_bytes()
    manifest = {
        "schema": "skybuild.full-test.environment.v1",
        "uv_version": uv_version,
        "pyproject_sha256": hashlib.sha256(pyproject).hexdigest(),
        "uv_lock_sha256": hashlib.sha256(lock).hexdigest(),
    }
    dist_info = site_packages / f"skybuild-{version}.dist-info"
    dist_info.mkdir(mode=0o755, parents=True, exist_ok=False)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.3\nName: skybuild\nVersion: {version}\n\n",
        encoding="utf-8",
    )
    (dist_info / "WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: skybuild-gate-static-metadata\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        encoding="ascii",
    )
    (dist_info / "uv_build.json").write_text("{}\n", encoding="ascii")
    (dist_info / "direct_url.json").write_text(
        json.dumps({"url": project.as_uri(), "dir_info": {"editable": True}}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (dist_info / "RECORD").write_text(
        f"{dist_info.name}/METADATA,,\n{dist_info.name}/WHEEL,,\n"
        f"{dist_info.name}/uv_build.json,,\n{dist_info.name}/direct_url.json,,\n"
        f"{dist_info.name}/RECORD,,\n_editable_impl_skybuild.pth,,\n",
        encoding="utf-8",
    )
    (site_packages / "_editable_impl_skybuild.pth").write_text(
        str(project / "src") + "\n", encoding="utf-8"
    )
    (environment / ".skybuild-environment.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--uv-version", required=True)
    args = parser.parse_args()
    print(json.dumps(write_environment(args.project, args.environment, args.site_packages,
                                       args.uv_version), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
