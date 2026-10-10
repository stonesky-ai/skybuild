#!/usr/bin/env python3
"""Build the exact local runner/firewall images consumed by isolated_full_test_gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tempfile
from uuid import uuid4

import isolated_full_test_gate as gate


PYTHON_BASE_ID = "sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5"
POSTGRES_BASE_ID = "sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54"
UV_VERSION = "0.11.22"
UV_SHA256_X86_64 = "2ed6f640f71df3520e60d91e8882dab50c38a232b1f13c954e89929021640422"
FIREWALL_EXECUTABLES = (
    "iptables-nft", "ip6tables-nft", "iptables-nft-save", "ip6tables-nft-save",
)
FIREWALL_ALIASES = {
    "iptables": "iptables-nft", "ip6tables": "ip6tables-nft",
    "iptables-save": "iptables-nft-save", "ip6tables-save": "ip6tables-nft-save",
}
FIREWALL_PLUGINS = ("libxt_conntrack.so",)
GIT_PACKAGE_MANIFEST = Path(__file__).with_name("gate_images") / "trusted_git_packages.json"
class BuildError(RuntimeError):
    """Local helper image inputs are incomplete or do not match the reviewed gate."""


def _run(arguments: list[str], *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                                check=False, env={**os.environ, "DOCKER_CLI_HINTS": "false"})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BuildError("local image preparation command failed") from error
    if check and result.returncode:
        raise BuildError("local image preparation command failed: " + Path(arguments[0]).name)
    return result


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inspect_local_image(image_id: str) -> dict:
    result = _run(["docker", "image", "inspect", image_id], timeout=30)
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise BuildError("local base image inspection returned invalid data") from error
    if len(rows) != 1 or rows[0].get("Id") != image_id:
        raise BuildError("required exact local base image is absent")
    return rows[0]


def _copy_file(source: Path, root: Path) -> None:
    source = source.resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise BuildError("firewall payload contains an unexpected non-file")
    destination = root / source.relative_to("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    shutil.copymode(source, destination)


def _needed_libraries(binary: Path) -> set[Path]:
    result = _run(["ldd", str(binary)], timeout=10)
    libraries: set[Path] = set()
    for line in result.stdout.splitlines():
        match = re.search(r"(?:=>\s+)?(/[^\s]+)\s+\(", line)
        if not match:
            continue
        path = Path(match[1]).resolve(strict=True)
        if str(path).startswith("/usr/lib/"):
            libraries.add(path)
        elif str(path).startswith(("/lib/", "/lib64/")):
            # The exact Debian Python base supplies its own compatible glibc.
            continue
        else:
            raise BuildError("firewall binary needs an unapproved host library location")
    if not libraries:
        raise BuildError("firewall dependency inspection found no packaged libraries")
    return libraries


def stage_firewall_payload(root: Path) -> dict:
    """Stage only nftables iptables, its conntrack matcher and non-glibc libraries."""
    if platform.machine() != "x86_64":
        raise BuildError("the pinned local firewall payload is reviewed only for x86_64")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    executable_source = Path("/usr/sbin/xtables-nft-multi").resolve(strict=True)
    if not executable_source.is_file():
        raise BuildError("host nftables iptables executable is unavailable")
    version = _run([str(executable_source), "iptables", "-V"], timeout=10).stdout.strip()
    if version != "iptables v1.8.11 (nf_tables)":
        raise BuildError("host firewall executable version differs from reviewed payload")
    binary_targets = {executable_source}
    plugin_dir = Path("/usr/lib/x86_64-linux-gnu/xtables")
    plugins = [plugin_dir / name for name in FIREWALL_PLUGINS]
    if any(not path.is_file() for path in plugins):
        raise BuildError("required conntrack firewall plugin is unavailable")
    libraries: set[Path] = set()
    for binary in (*binary_targets, *plugins):
        libraries.update(_needed_libraries(binary))
        _copy_file(binary, root)
    for library in libraries:
        _copy_file(library, root)
    for plugin in plugins:
        _copy_file(plugin, root)
    sbin = root / "usr/sbin"
    sbin.mkdir(parents=True, exist_ok=True)
    for name in FIREWALL_EXECUTABLES:
        (sbin / name).symlink_to("xtables-nft-multi")
    for name, target in FIREWALL_ALIASES.items():
        (sbin / name).symlink_to(target)
    manifest = {
        "schema": "skybuild.isolated-gate-firewall-payload.v1",
        "iptables_version": version,
        "base_image_id": PYTHON_BASE_ID,
        "executables": {str(path.relative_to(root)): _digest(path)
                        for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()},
        "aliases": FIREWALL_ALIASES,
    }
    (root / "firewall-payload.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return manifest


def _copy_pinned_package(source: Path, destination: Path, expected_sha256: str) -> Path:
    """Copy through a no-follow descriptor and return only verified private bytes."""
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise BuildError("trusted Git package input cannot be opened safely") from error
    complete = False
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise BuildError("trusted Git package input is not a regular file")
        try:
            destination_fd = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400
            )
        except OSError as error:
            raise BuildError("private Git package staging file cannot be created safely") from error
        digest = hashlib.sha256()
        try:
            while block := os.read(source_fd, 1024 * 1024):
                digest.update(block)
                view = memoryview(block)
                while view:
                    view = view[os.write(destination_fd, view):]
            os.fchmod(destination_fd, 0o400)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        if digest.hexdigest() != expected_sha256:
            raise BuildError("trusted Git package hash differs from the reviewed Debian payload")
        complete = True
        return destination
    except OSError as error:
        raise BuildError("trusted Git package could not be staged safely") from error
    finally:
        os.close(source_fd)
        if not complete:
            destination.unlink(missing_ok=True)


def stage_git_payload(root: Path, package_dir: Path) -> dict:
    """Unpack only the SHA-pinned Git/runtime packages bootstrapped from Debian trixie."""
    manifest = json.loads(GIT_PACKAGE_MANIFEST.read_bytes())
    if set(manifest) != {"schema", "base_image_id", "repository_suite", "packages"}:
        raise BuildError("trusted Git package manifest has an unexpected shape")
    if (manifest["schema"] != "skybuild.isolated-gate-debian-git-packages.v1"
            or manifest["base_image_id"] != PYTHON_BASE_ID
            or manifest["repository_suite"] != "Debian trixie"
            or not isinstance(manifest["packages"], list) or not manifest["packages"]):
        raise BuildError("trusted Git package manifest does not match the pinned Debian base")
    expected_files = {row.get("file") for row in manifest["packages"]
                      if isinstance(row, dict) and isinstance(row.get("file"), str)}
    actual_files = {path.name for path in package_dir.iterdir()}
    if (len(expected_files) != len(manifest["packages"]) or actual_files != expected_files
            or any(not path.is_file() or path.is_symlink() for path in package_dir.iterdir())):
        raise BuildError("offline Git package directory differs from the exact package allowlist")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    staged_packages = root / ".trusted-package-staging"
    staged_packages.mkdir(mode=0o700)
    package_rows = []
    try:
        for row in manifest["packages"]:
            if (set(row) != {"file", "name", "version", "architecture", "sha256"}
                    or not re.fullmatch(r"[A-Za-z0-9.+_%~-]+\.deb", row["file"])
                    or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])):
                raise BuildError("trusted Git package record is malformed")
            source = package_dir / row["file"]
            staged = staged_packages / row["file"]
            _copy_pinned_package(source, staged, row["sha256"])
            for field in ("Package", "Version", "Architecture"):
                actual = _run(["dpkg-deb", "-f", str(staged), field], timeout=10).stdout.strip()
                expected = row["name" if field == "Package" else field.lower()]
                if actual != expected:
                    raise BuildError("trusted Git package metadata differs from the reviewed Debian payload")
            _run(["dpkg-deb", "-x", str(staged), str(root)], timeout=30)
            package_rows.append(dict(row))
    finally:
        shutil.rmtree(staged_packages)
    if (not (root / "usr/bin/git").is_file()
            or not any((root / "usr/lib/git-core").glob("git-upload-pack*"))
            or not (root / "usr/share/git-core/templates").is_dir()):
        raise BuildError("trusted Debian package payload is missing Git runtime components")
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            files[relative] = "symlink:" + os.readlink(path)
        elif path.is_file():
            files[relative] = _digest(path)
        elif not path.is_dir():
            raise BuildError("trusted Git package payload contains a special file")
    return {"schema": "skybuild.isolated-gate-git-payload.v1", "base_image_id": PYTHON_BASE_ID,
            "repository_suite": manifest["repository_suite"], "packages": package_rows, "files": files}


def _image_digest(image_id: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise BuildError("Docker did not return an immutable image ID")
    return image_id


def _build_image(context: Path, dockerfile: str, tag: str, base_id: str,
                 build_args: dict[str, str]) -> str:
    command = ["docker", "build", "--pull=false", "--network=none", "--tag", tag,
               "--file", str(context / dockerfile), "--build-arg", "BASE_IMAGE=" + base_id]
    for name, value in sorted(build_args.items()):
        command.extend(("--build-arg", name + "=" + value))
    command.append(str(context))
    _run(command, timeout=1800)
    result = _run(["docker", "image", "inspect", tag], timeout=30)
    rows = json.loads(result.stdout)
    if len(rows) != 1:
        raise BuildError("built helper image could not be inspected")
    return _image_digest(rows[0].get("Id", ""))


def _tree_manifest(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = "symlink:" + os.readlink(path)
        elif path.is_file():
            result[relative] = _digest(path)
        elif not path.is_dir():
            raise BuildError("runner environment contains a special file")
    return result


def _prepare_runner_environment(context: Path, uv_cache: Path) -> dict:
    environment = context / "runner-environment"
    environment.mkdir(mode=0o700)
    name = "skybuild-gate-env-build-" + uuid4().hex[:16]
    command = [
        "docker", "run", "--pull=never", "--rm", "--name", name,
        "--label", "skybuild.isolated.image-builder=true",
        "--network=none", "--memory=2g", "--memory-swap=2g", "--cpus=1",
        "--pids-limit=64", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--user",
        f"{os.geteuid()}:{os.getegid()}",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=536870912,mode=1777",
        "--mount", f"type=bind,src={context},dst=/input,readonly",
        "--mount", f"type=bind,src={uv_cache},dst=/cache",
        "--mount", f"type=bind,src={environment},dst=/output",
        PYTHON_BASE_ID, "/bin/sh", "-ec",
        "mkdir -m 0700 /tmp/home; "
        "export HOME=/tmp/home UV_CACHE_DIR=/cache "
        "UV_PROJECT_ENVIRONMENT=/output/skybuild-venv PYTHONDONTWRITEBYTECODE=1; "
        "/input/uv sync --locked --offline --no-build --extra test --no-install-project "
        "--project /input --python /usr/local/bin/python3.14; "
        "/usr/local/bin/python3.14 -I /input/write_runner_environment.py "
        "--project /input --environment /output/skybuild-venv "
        "--site-packages /output/skybuild-venv/lib/python3.14/site-packages "
        "--uv-version " + UV_VERSION,
    ]
    try:
        _run(command, timeout=1800)
    finally:
        inspected = _run(["docker", "container", "inspect", name], timeout=20, check=False)
        if inspected.returncode == 0:
            try:
                rows = json.loads(inspected.stdout)
            except json.JSONDecodeError as error:
                raise BuildError("environment builder cleanup identity is unreadable") from error
            if (len(rows) != 1 or rows[0].get("Name", "").lstrip("/") != name
                    or (rows[0].get("Config", {}).get("Labels") or {}).get(
                        "skybuild.isolated.image-builder") != "true"):
                raise BuildError("environment builder cleanup refused an unowned container")
            _run(["docker", "container", "rm", "--force", name], timeout=30)
            remaining = _run(["docker", "container", "inspect", name], timeout=20, check=False)
            if remaining.returncode == 0:
                raise BuildError("environment builder container cleanup is unconfirmed")
    if not (environment / "skybuild-venv/bin/python").is_file():
        raise BuildError("offline runner environment preparation did not produce its Python")
    return {"container_name": name, "container_removed": True,
            "resource_limits": {"memory": "2g", "cpus": 1, "pids": 64,
                                "tmpfs": "512m", "network": "none"},
            "environment_files": _tree_manifest(environment)}


def build(checkout: Path, output: Path, *, gate_policy_sha256: str, uv_binary: Path,
          uv_cache: Path, git_package_dir: Path, runner_tag: str, firewall_tag: str) -> dict:
    checkout = checkout.resolve(strict=True)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    if not re.fullmatch(r"[0-9a-f]{64}", gate_policy_sha256):
        raise BuildError("gate policy hash must be a full SHA-256 value from the frozen policy")
    if _digest(uv_binary) != UV_SHA256_X86_64:
        raise BuildError("uv executable differs from the locally reviewed 0.11.22 binary")
    version = _run([str(uv_binary), "--version"], timeout=10).stdout.strip()
    if version != "uv " + UV_VERSION + " (x86_64-unknown-linux-gnu)":
        raise BuildError("uv executable does not match the reviewed version/platform")
    if not uv_cache.is_dir():
        raise BuildError("offline uv cache directory is unavailable")
    _inspect_local_image(PYTHON_BASE_ID)
    _inspect_local_image(POSTGRES_BASE_ID)
    if not (checkout / "pyproject.toml").is_file() or not (checkout / "uv.lock").is_file():
        raise BuildError("trusted project lock inputs are missing")
    if (checkout / "setup.py").exists() or (checkout / "setup.cfg").exists():
        raise BuildError("dynamic project build metadata has not been qualified")
    with tempfile.TemporaryDirectory(prefix="skybuild-isolated-images-") as name:
        context = Path(name)
        shutil.copy2(checkout / "scripts/gate_images/Dockerfile.runner", context)
        shutil.copy2(checkout / "scripts/gate_images/Dockerfile.firewall", context)
        shutil.copy2(checkout / "scripts/gate_images/write_runner_environment.py", context)
        shutil.copy2(checkout / "pyproject.toml", context)
        shutil.copy2(checkout / "uv.lock", context)
        shutil.copyfile(uv_binary, context / "uv")
        os.chmod(context / "uv", 0o755)
        runner_args = {
            "GATE_POLICY_SHA256": gate_policy_sha256,
            "UV_LOCK_SHA256": _digest(checkout / "uv.lock"),
            "PYPROJECT_SHA256": _digest(checkout / "pyproject.toml"),
            "COMMAND_SHA256": gate.DEFAULT_GATE_COMMAND_SHA256,
            "ENTRYPOINT_SHA256": _digest(checkout / "scripts/gate_container_entrypoint.py"),
            "NETWORK_PROBE_SHA256": _digest(checkout / "scripts/gate_network_probe.py"),
            "UV_VERSION": UV_VERSION,
        }
        runner_environment = _prepare_runner_environment(context, uv_cache)
        git_rootfs = context / "runner-git-rootfs"
        git_manifest = stage_git_payload(git_rootfs, git_package_dir.resolve(strict=True))
        runner_id = _build_image(context, "Dockerfile.runner", runner_tag, PYTHON_BASE_ID,
                                 runner_args)
        rootfs = context / "firewall-rootfs"
        manifest = stage_firewall_payload(rootfs)
        firewall_id = _build_image(context, "Dockerfile.firewall", firewall_tag, PYTHON_BASE_ID,
                                    {"FIREWALL_POLICY_SHA256": gate.FIREWALL_POLICY_SHA256})
    return {
        "schema": "skybuild.isolated-gate-local-images.v1",
        "runner_image_id": runner_id,
        "firewall_image_id": firewall_id,
        "postgres_image_id": POSTGRES_BASE_ID,
        "python_base_image_id": PYTHON_BASE_ID,
        "firewall_payload": manifest,
        "runner_environment": runner_environment,
        "git_payload": git_manifest,
        "runner_build": runner_args,
        "network": "none",
        "candidate_installation": False,
        "uv_offline": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate-policy-sha256", required=True)
    parser.add_argument("--uv-binary", type=Path, default=Path.home() / ".local/bin/uv")
    parser.add_argument("--uv-cache", type=Path, default=Path.home() / ".cache/uv")
    parser.add_argument("--git-package-dir", type=Path, required=True)
    parser.add_argument("--runner-tag", required=True)
    parser.add_argument("--firewall-tag", required=True)
    parser.add_argument("--build", action="store_true",
                         help="perform bounded offline local Docker builds (explicit side effect)")
    args = parser.parse_args()
    if not args.build:
        parser.error("pass --build only after independent source review and owner build approval")
    result = build(args.checkout, args.output, gate_policy_sha256=args.gate_policy_sha256,
                   uv_binary=args.uv_binary.resolve(strict=True), uv_cache=args.uv_cache.resolve(strict=True),
                   git_package_dir=args.git_package_dir.resolve(strict=True),
                   runner_tag=args.runner_tag, firewall_tag=args.firewall_tag)
    (args.output / "image-build-result.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as error:
        raise SystemExit("isolated gate image build stopped: " + str(error)) from None
