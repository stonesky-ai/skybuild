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
import shlex
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
FIREWALL_PLUGINS = ("libxt_conntrack.so", "libxt_standard.so", "libxt_tcp.so")
BASE_GLIBC_LIBRARIES = frozenset({
    "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1",
    "libresolv.so.2", "ld-linux-x86-64.so.2",
})
GIT_PACKAGE_MANIFEST = Path(__file__).with_name("gate_images") / "trusted_git_packages.json"
class BuildError(RuntimeError):
    """Local helper image inputs are incomplete or do not match the reviewed gate."""


_DIAGNOSTIC_LIMIT = 2000
_URL_USER_INFO = re.compile(r"(?i)\b(https?://)[^/@:\s]+:[^/@\s]+@([^/\s]+)")
_JSON_AUTHORIZATION_VALUE = re.compile(
    r"(?i)([\"']authorization[\"']\s*:\s*[\"'](?:[a-z]+\s+)?)([^\"']+)([\"'])"
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*(?:[a-z]+\s+)?)([^\s,;]+)"
)
_SECRET_VALUE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|access[_-]?key|private[_-]?key|key|token|secret|password|passwd)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}\]]+)"
    r"|\b(bearer\s+)([^\s,;]+)"
)


def _safe_diagnostic(value: str) -> str:
    value = _URL_USER_INFO.sub(r"\1[REDACTED]@\2", value)
    value = _JSON_AUTHORIZATION_VALUE.sub(r"\1[REDACTED]\3", value)
    value = _AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", value)
    value = _SECRET_VALUE.sub(
        lambda match: (match.group(1) or match.group(3)) + "[REDACTED]", value
    )
    if len(value) > _DIAGNOSTIC_LIMIT:
        value = "[truncated]\n" + value[-_DIAGNOSTIC_LIMIT:]
    return value


def _command_failure(arguments: list[str], *, returncode: int | str,
                     stderr: str | bytes = "") -> BuildError:
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    safe_arguments = []
    redact_next = False
    for argument in arguments:
        if redact_next:
            safe_arguments.append("[REDACTED]")
            redact_next = False
            continue
        safe_arguments.append(argument)
        if argument.lower() in {
            "--token", "--secret", "--password", "--authorization", "--key",
            "--api-key", "--access-key", "--private-key", "--user", "-u",
        }:
            redact_next = True
    command = _safe_diagnostic(shlex.join(safe_arguments))
    detail = _safe_diagnostic(stderr.strip()) or "[no stderr]"
    return BuildError(
        f"local image preparation command failed: command={command!r}; "
        f"returncode={returncode}; stderr={detail!r}"
    )


def _run(arguments: list[str], *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                                check=False, env={**os.environ, "DOCKER_CLI_HINTS": "false"})
    except subprocess.TimeoutExpired as error:
        raise _command_failure(arguments, returncode="timeout", stderr=error.stderr or "") from error
    except OSError as error:
        raise _command_failure(arguments, returncode=f"os-error:{error.errno}") from error
    if check and result.returncode:
        raise _command_failure(arguments, returncode=result.returncode, stderr=result.stderr)
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


def _needed_libraries(binary: Path) -> dict[Path, set[str]]:
    result = _run(["ldd", str(binary)], timeout=10)
    libraries: dict[Path, set[str]] = {}
    for line in result.stdout.splitlines():
        if "=> not found" in line:
            raise BuildError("firewall dependency inspection found an unresolved library")
        match = re.search(r"^\s*([^\s]+)\s+=>\s+(/[^\s]+)\s+\(", line)
        if match is None:
            continue
        soname = match[1]
        if soname in BASE_GLIBC_LIBRARIES:
            # The image is pinned to a Debian base that supplies these exact
            # glibc runtime names. The in-image smoke verifies that contract.
            continue
        path = Path(match[2]).resolve(strict=True)
        if str(path).startswith("/usr/lib/"):
            libraries.setdefault(path, set()).add(soname)
        elif str(path).startswith(("/lib/", "/lib64/")):
            raise BuildError("firewall dependency is outside the pinned base-library allowlist")
        else:
            raise BuildError("firewall binary needs an unapproved host library location")
    if not libraries:
        raise BuildError("firewall dependency inspection found no packaged libraries")
    return libraries


def stage_firewall_payload(root: Path) -> dict:
    """Stage nftables iptables, required match/target plugins, and runtime libraries."""
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
    libraries: dict[Path, set[str]] = {}
    for binary in (*binary_targets, *plugins):
        for library, sonames in _needed_libraries(binary).items():
            libraries.setdefault(library, set()).update(sonames)
        _copy_file(binary, root)
    library_aliases: dict[str, str] = {}
    for library, sonames in libraries.items():
        _copy_file(library, root)
        destination = root / library.relative_to("/")
        for soname in sorted(sonames):
            if not re.fullmatch(r"[A-Za-z0-9_.+-]+", soname):
                raise BuildError("firewall dependency has an unsafe library name")
            if soname == library.name:
                continue
            alias = destination.parent / soname
            source_alias = library.parent / soname
            if source_alias.resolve(strict=True) != library:
                raise BuildError("firewall soname does not resolve to its inspected library")
            if alias.exists() or alias.is_symlink():
                if not alias.is_symlink() or os.readlink(alias) != library.name:
                    raise BuildError("firewall library alias conflicts with another staged file")
            else:
                alias.symlink_to(library.name)
            library_aliases[str(alias.relative_to(root))] = library.name
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
        "library_aliases": dict(sorted(library_aliases.items())),
        "aliases": FIREWALL_ALIASES,
    }
    (root / "firewall-payload.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    return manifest


def _smoke_firewall_image(image_id: str) -> dict:
    """Exercise the exact nft-backed v4/v6 commands offline in a disposable netns."""
    ipv4_marker = "SKYBUILD_IPV4_SAVE_BEGIN"
    ipv6_marker = "SKYBUILD_IPV6_SAVE_BEGIN"
    script = "\n".join((
        "set -eu",
        "iptables -V", "ip6tables -V", "iptables-save --version", "ip6tables-save --version",
        "iptables -w -F INPUT; iptables -w -F OUTPUT; iptables -w -F FORWARD",
        "iptables -w -P INPUT DROP; iptables -w -P OUTPUT DROP; iptables -w -P FORWARD DROP",
        "iptables -w -A INPUT -i lo -j ACCEPT",
        "iptables -w -A INPUT -s 198.18.0.3/32 -p tcp --dport 5432 -j ACCEPT",
        "iptables -w -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "iptables -w -A OUTPUT -d 127.0.0.11/32 -j DROP",
        "iptables -w -A OUTPUT -o lo -j ACCEPT",
        "iptables -w -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "ip6tables -w -F INPUT; ip6tables -w -F OUTPUT; ip6tables -w -F FORWARD",
        "ip6tables -w -P INPUT DROP; ip6tables -w -P OUTPUT DROP; ip6tables -w -P FORWARD DROP",
        "ip6tables -w -A INPUT -i lo -j ACCEPT",
        "ip6tables -w -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "ip6tables -w -A OUTPUT -o lo -j ACCEPT",
        "ip6tables -w -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "printf '%s\\n' " + ipv4_marker, "iptables-save -t filter",
        "printf '%s\\n' " + ipv6_marker, "ip6tables-save -t filter",
    ))
    smoke_name = "skybuild-firewall-smoke-" + uuid4().hex[:12]
    result = _run(["docker", "run", "--rm", "--name", smoke_name, "--network", "none",
          "--memory=128m", "--memory-swap=128m",
          "--cpus=0.25", "--pids-limit=32", "--read-only", "--tmpfs", "/run:rw,noexec,nosuid,size=4m",
          "--cap-drop", "ALL", "--cap-add", "NET_ADMIN", "--security-opt=no-new-privileges",
          "--label", "skybuild.isolated.firewall-smoke=true", "--entrypoint", "/bin/sh",
          image_id, "-ceu", script], timeout=30)
    lines = result.stdout.splitlines()
    if lines.count(ipv4_marker) != 1 or lines.count(ipv6_marker) != 1:
        raise BuildError("firewall smoke did not return both save outputs")
    ipv4_start, ipv6_start = lines.index(ipv4_marker) + 1, lines.index(ipv6_marker) + 1
    if ipv4_start >= ipv6_start:
        raise BuildError("firewall smoke save outputs are out of order")
    try:
        ipv4_rules = gate._saved_rules("\n".join(lines[ipv4_start:ipv6_start - 1]))
        ipv6_rules = gate._saved_rules("\n".join(lines[ipv6_start:]))
    except gate.GateError as error:
        raise BuildError("firewall smoke save output failed gate parser contract") from error
    expected_ipv4 = gate._expected_firewall_rules("198.18.0.2", "198.18.0.3",
                                                  ipv6=False, postgres_namespace=True)
    expected_ipv6 = gate._expected_firewall_rules("198.18.0.2", "198.18.0.3",
                                                  ipv6=True, postgres_namespace=True)
    if ipv4_rules != expected_ipv4 or ipv6_rules != expected_ipv6:
        raise BuildError("firewall smoke rules differ from gate parser contract")
    return {"status": "passed", "network": "none", "capabilities": ["NET_ADMIN"],
            "commands": ["iptables", "ip6tables", "iptables-save", "ip6tables-save", "conntrack matcher"],
            "parser_contract": "exact dual-stack filter rules"}


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
    command = ["docker", "build", "--pull=false", "--network=none",
               "--memory=2g", "--memory-swap=2g", "--cpu-period=100000", "--cpu-quota=100000",
               "--tag", tag,
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


def _has_pinned_runner_python(environment: Path) -> bool:
    python = environment / "skybuild-venv/bin/python"
    return python.is_symlink() and os.readlink(python) == "/usr/local/bin/python3.14"


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
    if not _has_pinned_runner_python(environment):
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
        firewall_smoke = _smoke_firewall_image(firewall_id)
    return {
        "schema": "skybuild.isolated-gate-local-images.v1",
        "runner_image_id": runner_id,
        "firewall_image_id": firewall_id,
        "postgres_image_id": POSTGRES_BASE_ID,
        "python_base_image_id": PYTHON_BASE_ID,
        "firewall_payload": manifest,
        "firewall_smoke": firewall_smoke,
        "runner_environment": runner_environment,
        "git_payload": git_manifest,
        "runner_build": runner_args,
        "network": "none",
        "candidate_installation": False,
        "uv_offline": True,
    }


def build_firewall_only(checkout: Path, output: Path, *, gate_policy_sha256: str,
                        firewall_tag: str, prior_receipt: Path) -> dict:
    """Replace only the firewall image while preserving verified runner/PG pins."""
    if not re.fullmatch(r"[0-9a-f]{64}", gate_policy_sha256):
        raise BuildError("gate policy hash must be a full SHA-256 value from the frozen policy")
    try:
        fd = os.open(prior_receipt, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise BuildError("prior image receipt cannot be opened safely") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 2_000_000:
            raise BuildError("prior image receipt is not a bounded regular file")
        raw = os.read(fd, 2_000_001)
    finally:
        os.close(fd)
    try:
        prior = json.loads(raw)
    except json.JSONDecodeError as error:
        raise BuildError("prior image receipt is invalid JSON") from error
    runner_build = prior.get("runner_build") if isinstance(prior, dict) else None
    if (not isinstance(prior, dict) or prior.get("schema") != "skybuild.isolated-gate-local-images.v1"
            or prior.get("postgres_image_id") != POSTGRES_BASE_ID
            or prior.get("python_base_image_id") != PYTHON_BASE_ID
            or prior.get("network") != "none" or prior.get("candidate_installation") is not False
            or prior.get("uv_offline") is not True or not isinstance(runner_build, dict)
            or runner_build.get("GATE_POLICY_SHA256") != gate_policy_sha256
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(prior.get("runner_image_id", "")))):
        raise BuildError("prior runner receipt does not match this frozen gate and pinned bases")
    checkout = checkout.resolve(strict=True)
    if output.exists():
        raise BuildError("firewall-only output path already exists")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    _inspect_local_image(PYTHON_BASE_ID)
    with tempfile.TemporaryDirectory(prefix="skybuild-firewall-image-") as name:
        context = Path(name)
        shutil.copy2(checkout / "scripts/gate_images/Dockerfile.firewall", context)
        rootfs = context / "firewall-rootfs"
        manifest = stage_firewall_payload(rootfs)
        firewall_id = _build_image(context, "Dockerfile.firewall", firewall_tag, PYTHON_BASE_ID,
                                    {"FIREWALL_POLICY_SHA256": gate.FIREWALL_POLICY_SHA256})
        firewall_smoke = _smoke_firewall_image(firewall_id)
    result = {**prior, "firewall_image_id": firewall_id, "firewall_payload": manifest,
              "firewall_smoke": firewall_smoke, "firewall_build_scope": "firewall-only"}
    (output / "image-build-result.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gate-policy-sha256", required=True)
    parser.add_argument("--uv-binary", type=Path, default=Path.home() / ".local/bin/uv")
    parser.add_argument("--uv-cache", type=Path, default=Path.home() / ".cache/uv")
    parser.add_argument("--git-package-dir", type=Path)
    parser.add_argument("--runner-tag")
    parser.add_argument("--firewall-tag", required=True)
    parser.add_argument("--firewall-only", action="store_true",
                         help="reuse verified runner/PostgreSQL pins and rebuild only the firewall image")
    parser.add_argument("--reuse-runner-receipt", type=Path,
                        help="prior exact builder receipt required with --firewall-only")
    parser.add_argument("--build", action="store_true",
                         help="perform bounded offline local Docker builds (explicit side effect)")
    args = parser.parse_args()
    if not args.build:
        parser.error("pass --build only after independent source review and owner build approval")
    if args.firewall_only:
        if args.reuse_runner_receipt is None:
            parser.error("--firewall-only requires --reuse-runner-receipt")
        result = build_firewall_only(args.checkout, args.output, gate_policy_sha256=args.gate_policy_sha256,
                                     firewall_tag=args.firewall_tag,
                                     prior_receipt=args.reuse_runner_receipt)
    else:
        if args.reuse_runner_receipt is not None:
            parser.error("--reuse-runner-receipt requires --firewall-only")
        if args.uv_binary is None or args.uv_cache is None or args.git_package_dir is None or args.runner_tag is None:
            parser.error("full image builds require --uv-binary, --uv-cache, --git-package-dir, and --runner-tag")
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
