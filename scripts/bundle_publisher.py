#!/usr/bin/env python3
"""Publish one reviewed bundle by exact-base Git ref compare-and-swap.

This module owns the publication boundary only. Candidate execution and the
independent review/gate qualification must be provided by a separately trusted
controller before calling ``publish_bundle``. An ambiguous push is recorded as
unresolved and is never retried; ``reconcile_bundle`` only reads remote state.
"""
from __future__ import annotations

import fcntl
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Mapping
from _repo_guard import verify_skybuild, verify_skybuild_remote
from trusted_gate_attestation import verify_attestation
from skybuild.manual_integration import DEFAULT_GATE_COMMAND, DEFAULT_GATE_COMMAND_SHA256


class PublisherError(RuntimeError):
    pass


_OID = re.compile(r"[0-9a-f]{40}")
_REF = re.compile(r"refs/heads/[A-Za-z0-9._/-]+")
_TERMINAL = {"confirmed"}
_REPOSITORY = "stonesky-ai/skybuild"
_KEYRING_PATH = Path("/etc/skybuild/trusted-gate-keyring.json")
_INTENT_DIR = Path("/var/lib/skybuild/publication-intents")


def _run(argv: list[str], cwd: Path, *, check: bool = True) -> str:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(argv, cwd=cwd, env=env, text=True,
                            capture_output=True, timeout=60)
    if check and result.returncode:
        raise PublisherError(f"{argv[0]} failed ({result.returncode})")
    return result.stdout.strip()


def _require_oid(value: str, name: str) -> None:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise PublisherError(f"{name} must be a full lowercase commit SHA")


def _remote_oid(checkout: Path, remote: str, ref: str) -> str | None:
    output = _run(["git", "ls-remote", "--refs", remote, ref], checkout)
    if not output:
        return None
    rows = output.splitlines()
    if len(rows) != 1 or len(rows[0].split()) != 2 or rows[0].split()[1] != ref:
        raise PublisherError("Remote returned an ambiguous ref observation")
    return rows[0].split()[0]


def _candidate_archive_sha256(checkout: Path, candidate: str) -> str:
    """Hash the exact deterministic archive that the isolated runner consumes."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    digest = hashlib.sha256()
    process = subprocess.Popen(["git", "archive", "--format=tar", candidate], cwd=checkout,
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    while block := process.stdout.read(1024 * 1024):
        digest.update(block)
    stderr = process.stderr.read() if process.stderr else b""
    if process.wait(timeout=120):
        raise PublisherError("Candidate source archive could not be verified: " +
                             stderr[-256:].decode("utf-8", "replace"))
    return digest.hexdigest()


def observe_github_pr(pr: int) -> dict:
    """Read the configured bundle PR from GitHub; this performs no writes."""
    result = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", _REPOSITORY, "--json",
         "number,state,isDraft,headRefOid,baseRefOid,baseRefName,headRepository,headRepositoryOwner,reviewDecision,mergeCommit,statusCheckRollup"],
        text=True, capture_output=True, timeout=30,
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")} |
            {"GH_PROMPT_DISABLED": "1"})
    if result.returncode:
        raise PublisherError("GitHub PR read failed")
    try:
        raw = json.loads(result.stdout)
        owner = raw["headRepositoryOwner"]["login"]
        repository = raw["headRepository"]["name"]
        merge = raw.get("mergeCommit")
        rollup = raw.get("statusCheckRollup")
        checks_pass = isinstance(rollup, list) and all(
            isinstance(check, dict) and check.get("status") == "COMPLETED"
            and check.get("conclusion") in {"SUCCESS", "NEUTRAL"} for check in rollup)
        return {"number": raw["number"], "state": raw["state"],
                "isDraft": raw["isDraft"], "headRefOid": raw["headRefOid"],
                "baseRefOid": raw["baseRefOid"],
                "baseRefName": raw["baseRefName"], "headRepository": f"{owner}/{repository}",
                "reviewDecision": raw["reviewDecision"],
                "checks": "PASS" if checks_pass else "FAIL",
                "mergeCommitOid": merge.get("oid") if isinstance(merge, dict) else None}
    except (KeyError, TypeError, ValueError):
        raise PublisherError("GitHub PR read returned an incomplete response") from None


def _private_store(path: Path) -> Path:
    if not path.is_absolute():
        raise PublisherError("Intent store path must be absolute")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or path.resolve() != path):
        raise PublisherError("Intent store must be an owned private directory")
    return path


def _read_attestation(path: Path, checkout: Path) -> dict:
    if not path.is_absolute() or path.resolve().is_relative_to(checkout):
        raise PublisherError("Gate attestation must be stored outside the candidate checkout")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > 64 * 1024):
            raise PublisherError("Gate attestation must be a small private owned file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(64 * 1024 + 1)
    except OSError as error:
        raise PublisherError("Gate attestation could not be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > 64 * 1024:
        raise PublisherError("Gate attestation exceeds size limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise PublisherError("Gate attestation is not valid JSON") from error
    if not isinstance(value, dict):
        raise PublisherError("Gate attestation must be a JSON object")
    return value


_RUNNER_PIN_FIELDS = {
    "runner_identity", "runner_version", "runner_image_id", "postgres_image_id",
    "firewall_image_id", "firewall_policy_sha256", "trusted_entrypoint_sha256",
    "network_probe_sha256", "execution_host", "environment_allowlist", "resource_limits",
}


def _read_keyring(path: Path = _KEYRING_PATH) -> tuple[dict[str, Path], str, str, dict]:
    """Load root-owned key IDs and the exact trusted publisher source commit."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022 or info.st_size > 8192):
            raise PublisherError("Trusted gate keyring must be root-owned and not group/world writable")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(8193)
    except OSError as error:
        raise PublisherError("Trusted gate keyring could not be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        config = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise PublisherError("Trusted gate keyring is invalid JSON") from error
    if (not isinstance(config, dict)
            or set(config) != {"schema", "active_key_id", "publisher_commit", "runner_pins", "keys"}
            or config["schema"] != "skybuild.publisher-keyring.v1"
            or not isinstance(config["active_key_id"], str)
            or not isinstance(config["publisher_commit"], str)
            or not _OID.fullmatch(config["publisher_commit"])
            or not isinstance(config["keys"], dict) or not config["keys"]
            or len(config["keys"]) > 16):
        raise PublisherError("Trusted gate keyring has invalid fields")
    keys = {}
    for key_id, key_path in config["keys"].items():
        if (not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", key_id)
                or not isinstance(key_path, str) or not Path(key_path).is_absolute()):
            raise PublisherError("Trusted gate keyring entry is invalid")
        keys[key_id] = Path(key_path)
    active = config["active_key_id"]
    if active not in keys:
        raise PublisherError("Active trusted gate key ID is not pinned")
    runner_pins = config["runner_pins"]
    if not isinstance(runner_pins, dict) or set(runner_pins) != _RUNNER_PIN_FIELDS:
        raise PublisherError("Trusted runner pin set has invalid fields")
    for field in ("runner_identity", "runner_version", "execution_host"):
        if not isinstance(runner_pins[field], str) or not runner_pins[field]:
            raise PublisherError(f"Trusted runner {field} pin is invalid")
    for field in ("runner_image_id", "postgres_image_id", "firewall_image_id"):
        if not isinstance(runner_pins[field], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", runner_pins[field]):
            raise PublisherError(f"Trusted runner {field} pin is invalid")
    for field in ("firewall_policy_sha256", "trusted_entrypoint_sha256", "network_probe_sha256"):
        if not isinstance(runner_pins[field], str) or not re.fullmatch(r"[0-9a-f]{64}", runner_pins[field]):
            raise PublisherError(f"Trusted runner {field} pin is invalid")
    environment = runner_pins["environment_allowlist"]
    limits = runner_pins["resource_limits"]
    if (not isinstance(environment, list) or not environment
            or any(not isinstance(value, str) for value in environment)
            or not isinstance(limits, dict)
            or set(limits) != {"cpu_millis", "memory_bytes", "pids", "timeout_seconds"}
            or any(type(value) is not int or value <= 0 for value in limits.values())):
        raise PublisherError("Trusted runner resource or environment pins are invalid")
    return keys, active, config["publisher_commit"], runner_pins


def _verify_trusted_publisher_source(source_root: Path, expected_commit: str) -> None:
    """Require publisher code to run from the pinned, clean trusted checkout.

    The source root is the installed publisher checkout (where this module
    lives); the candidate checkout is a separate argument to publication.
    """
    _require_oid(expected_commit, "Trusted publisher source commit")
    source_root = source_root.resolve()
    try:
        top = Path(_run(["git", "rev-parse", "--show-toplevel"], source_root)).resolve()
        head = _run(["git", "rev-parse", "HEAD"], source_root)
        status = _run(["git", "status", "--porcelain", "--untracked-files=all"], source_root)
    except (OSError, PublisherError) as error:
        raise PublisherError("Trusted publisher source checkout could not be verified") from error
    if top != source_root or head != expected_commit or status:
        raise PublisherError("Publisher source is not the clean root-pinned trusted commit")
    module_path = Path(__file__).resolve()
    try:
        relative = module_path.relative_to(source_root).as_posix()
    except ValueError:
        raise PublisherError("Publisher module is outside the trusted source checkout") from None
    tracked = _run(["git", "ls-tree", "--name-only", expected_commit, "--", relative], source_root)
    if tracked != relative:
        raise PublisherError("Publisher module is not tracked by the pinned source commit")


def _key(remote: str, target_ref: str) -> str:
    return hashlib.sha256((remote + "\0" + target_ref).encode()).hexdigest()


def _read_intent(path: Path) -> dict | None:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PublisherError("Publication intent is not a private owned file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(256 * 1024 + 1)
    except FileNotFoundError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > 256 * 1024:
        raise PublisherError("Publication intent is too large")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != "skybuild.publication-intent.v1":
        raise PublisherError("Publication intent is invalid")
    return value


def _write_intent(path: Path, value: dict) -> None:
    directory = path.parent
    fd, temporary = tempfile.mkstemp(prefix=".intent-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _lock(path: Path):
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(descriptor)
        raise PublisherError("Publication lock is not a private owned file")
    handle = os.fdopen(descriptor, "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _same_bundle(intent: dict, *, remote: str, target_ref: str,
                 expected_base: str, candidate: str, tree: str, pr: int,
                 bundle_id: str) -> bool:
    return all(intent.get(key) == value for key, value in {
        "remote": remote, "target_ref": target_ref, "expected_base": expected_base,
        "candidate": candidate, "tree": tree, "pr": pr, "bundle_id": bundle_id,
    }.items())


def _pr_is_exact(pr_view: dict, *, pr: int, target_ref: str,
                 expected_base: str, candidate: str) -> bool:
    return (isinstance(pr_view, dict) and pr_view.get("number") == pr
            and pr_view.get("state") == "OPEN" and pr_view.get("isDraft") is False
            and pr_view.get("headRefOid") == candidate
            and pr_view.get("baseRefOid") == expected_base
            and pr_view.get("baseRefName") == target_ref.removeprefix("refs/heads/")
            and pr_view.get("headRepository") == _REPOSITORY
            and pr_view.get("reviewDecision") == "APPROVED"
            and pr_view.get("checks") == "PASS")


def publish_bundle(*, checkout: Path, intent_dir: Path, remote: str,
                   frozen_bundle: dict, pr: int,
                   qualification: dict,
                   trusted_gate_keys: Mapping[str, Path], expected_gate_key_id: str,
                   trusted_runner_pins: Mapping[str, object],
                   trusted_source_root: Path, trusted_publisher_commit: str,
                   observe_pr: Callable[[int], dict] = observe_github_pr) -> dict:
    """Attempt exactly one CAS for a fully-qualified frozen candidate.

    ``qualification`` must be a host-signed receipt from the isolated full-test
    runner. Ordinary candidate subprocess output cannot satisfy verification.
    """
    checkout = checkout.resolve()
    _verify_trusted_publisher_source(trusted_source_root, trusted_publisher_commit)
    verify_skybuild(checkout)
    verify_skybuild_remote(checkout, remote)
    store = _private_store(intent_dir)
    if store.is_relative_to(checkout):
        raise PublisherError("Publication intent must be stored outside the candidate checkout")
    try:
        bundle_id = frozen_bundle["bundle_id"]
        manifest_sha = frozen_bundle["manifest_sha256"]
        target_ref = frozen_bundle["target_ref"]
        expected_base = frozen_bundle["base_commit"]
        candidate = frozen_bundle["candidate_commit"]
        candidate_tree = frozen_bundle["candidate_tree"]
        members = frozen_bundle["members"]
    except (KeyError, TypeError):
        raise PublisherError("Frozen bundle evidence is incomplete") from None
    if not _REF.fullmatch(target_ref) or target_ref.endswith("/") or ".." in target_ref.split("/"):
        raise PublisherError("Target must be an explicit branch ref")
    _run(["git", "check-ref-format", target_ref], checkout)
    for value, label in ((expected_base, "expected base"), (candidate, "candidate"),
                         (candidate_tree, "candidate tree")):
        _require_oid(value, label)
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        raise PublisherError("PR number must be positive")
    if (not isinstance(manifest_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha)
            or bundle_id != "bundle-" + manifest_sha[:24]
            or not isinstance(members, list) or not 1 <= len(members) <= 20):
        raise PublisherError("Frozen bundle identity or membership is invalid")
    member_tasks = set()
    for member in members:
        if (not isinstance(member, dict) or not isinstance(member.get("task_id"), str)
                or not member["task_id"].strip() or member["task_id"] in member_tasks):
            raise PublisherError("Frozen bundle has invalid or duplicate task members")
        if (not isinstance(member.get("reviewer"), str) or not member["reviewer"].strip()
                or not isinstance(member.get("source_branch"), str)
                or not member["source_branch"].startswith("refs/heads/")):
            raise PublisherError("Frozen bundle member review/source binding is incomplete")
        _run(["git", "check-ref-format", member["source_branch"]], checkout)
        _require_oid(member.get("source_head"), "member source head")
        if (not isinstance(member.get("review_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", member["review_sha256"])):
            raise PublisherError("Frozen bundle member review evidence is invalid")
        _run(["git", "merge-base", "--is-ancestor", member["source_head"], candidate], checkout)
        member_tasks.add(member["task_id"])
    key = _key(remote, target_ref)
    prior = _read_intent(store / f"{key}.json")
    if prior and prior.get("state") not in _TERMINAL:
        if not _same_bundle(prior, remote=remote, target_ref=target_ref,
                            expected_base=expected_base, candidate=candidate,
                            tree=candidate_tree, pr=pr, bundle_id=bundle_id):
            raise PublisherError("An unresolved publication intent owns this target")
        return reconcile_bundle(checkout=checkout, intent_dir=store,
                                remote=remote, target_ref=target_ref,
                                pr=pr, observe_pr=observe_pr)
    if _run(["git", "rev-parse", candidate + "^{tree}"], checkout) != candidate_tree:
        raise PublisherError("Candidate tree differs from frozen bundle")
    _run(["git", "merge-base", "--is-ancestor", expected_base, candidate], checkout)
    archive_sha256 = _candidate_archive_sha256(checkout, candidate)
    try:
        policy_sha = frozen_bundle["policy_sha256"]
    except KeyError:
        raise PublisherError("Frozen bundle policy digest is missing") from None
    expected = {
        "bundle_id": bundle_id, "pr_number": pr, "target_ref": target_ref,
        "target_base": expected_base, "candidate_commit": candidate,
        "candidate_tree": candidate_tree, "candidate_archive_sha256": archive_sha256,
        "gate_argv": DEFAULT_GATE_COMMAND,
        "gate_command_sha256": DEFAULT_GATE_COMMAND_SHA256,
        "gate_policy_sha256": policy_sha,
    }
    if (not isinstance(trusted_runner_pins, Mapping)
            or set(trusted_runner_pins) != _RUNNER_PIN_FIELDS):
        raise PublisherError("Trusted runner pin set is missing or has unknown fields")
    expected.update(trusted_runner_pins)
    if (not isinstance(trusted_gate_keys, Mapping) or expected_gate_key_id not in trusted_gate_keys
            or any(key_path.resolve().is_relative_to(checkout)
                   for key_path in trusted_gate_keys.values())):
        raise PublisherError("Trusted gate keys must be configured outside the SkyBuild checkout")
    try:
        verify_attestation(qualification, trusted_keys=trusted_gate_keys,
                           expected_predicate=expected,
                           expected_key_id=expected_gate_key_id)
    except (ValueError, OSError) as error:
        raise PublisherError("Trusted full-test gate attestation rejected: " + str(error)) from error
    if _remote_oid(checkout, remote, target_ref) != expected_base:
        raise PublisherError("Remote target changed before publication")
    if not _pr_is_exact(observe_pr(pr), pr=pr, target_ref=target_ref,
                        expected_base=expected_base, candidate=candidate):
        raise PublisherError("Bundle PR is not the exact approved candidate with passing required checks")

    intent_path = store / f"{key}.json"
    lock_handle = _lock(store / f"{key}.lock")
    try:
        intent = _read_intent(intent_path)
        if intent and intent.get("state") not in _TERMINAL:
            if not _same_bundle(intent, remote=remote, target_ref=target_ref,
                                expected_base=expected_base, candidate=candidate,
                                tree=candidate_tree, pr=pr, bundle_id=bundle_id):
                raise PublisherError("An unresolved publication intent owns this target")
            return _reconcile_locked(checkout=checkout, intent_path=intent_path,
                                     remote=remote, target_ref=target_ref,
                                     pr=pr, observe_pr=observe_pr)
        if intent and intent.get("state") == "confirmed":
            if _same_bundle(intent, remote=remote, target_ref=target_ref,
                            expected_base=expected_base, candidate=candidate,
                            tree=candidate_tree, pr=pr, bundle_id=bundle_id):
                return intent
            raise PublisherError("A confirmed target intent must be archived before another publication")
        intent = {"schema": "skybuild.publication-intent.v1", "remote": remote,
                  "target_ref": target_ref, "expected_base": expected_base,
                  "candidate": candidate, "tree": candidate_tree, "pr": pr,
                  "bundle_id": bundle_id, "members": members,
                  "archive_sha256": archive_sha256,
                  "state": "sending", "attempts": 1}
        _write_intent(intent_path, intent)
        # A lease is the actual remote race fence. We independently prove the
        # update is a fast-forward before allowing this single lease-protected push.
        try:
            result = subprocess.run(["git", "push", "--porcelain",
                                     f"--force-with-lease={target_ref}:{expected_base}",
                                     remote, f"{candidate}:{target_ref}"], cwd=checkout,
                                    text=True, capture_output=True, timeout=120,
                                    env={key: value for key, value in os.environ.items()
                                         if not key.startswith("GIT_")} | {"GIT_TERMINAL_PROMPT": "0"})
        except (OSError, subprocess.SubprocessError):
            intent["state"] = "unknown"
            intent["send_error"] = "push command did not confirm completion"
            _write_intent(intent_path, intent)
            raise PublisherError("Publication outcome is unknown; do not resend; reconcile by reads")
        if result.returncode:
            intent["state"] = "unknown"
            intent["send_error"] = "push command did not confirm completion"
            _write_intent(intent_path, intent)
            raise PublisherError("Publication outcome is unknown; do not resend; reconcile by reads")
        intent["state"] = "unknown"
        intent["send_ack"] = "success"
        _write_intent(intent_path, intent)
        return _reconcile_locked(checkout=checkout, intent_path=intent_path,
                                 remote=remote, target_ref=target_ref,
                                 pr=pr, observe_pr=observe_pr)
    finally:
        lock_handle.close()


def reconcile_bundle(*, checkout: Path, intent_dir: Path, remote: str,
                     target_ref: str, pr: int,
                     observe_pr: Callable[[int], dict] = observe_github_pr) -> dict:
    """GET-only reconciliation. This function contains no publication command."""
    checkout = checkout.resolve()
    verify_skybuild(checkout)
    verify_skybuild_remote(checkout, remote)
    store = _private_store(intent_dir)
    if store.is_relative_to(checkout):
        raise PublisherError("Publication intent must be stored outside the candidate checkout")
    if not _REF.fullmatch(target_ref) or target_ref.endswith("/") or ".." in target_ref.split("/"):
        raise PublisherError("Target must be an explicit branch ref")
    _run(["git", "check-ref-format", target_ref], checkout)
    key = _key(remote, target_ref)
    path = store / f"{key}.json"
    lock_handle = _lock(store / f"{key}.lock")
    try:
        return _reconcile_locked(checkout=checkout, intent_path=path,
                                 remote=remote, target_ref=target_ref,
                                 pr=pr, observe_pr=observe_pr)
    finally:
        lock_handle.close()


def _reconcile_locked(*, checkout: Path, intent_path: Path, remote: str,
                      target_ref: str, pr: int,
                      observe_pr: Callable[[int], dict]) -> dict:
    intent = _read_intent(intent_path)
    if not intent:
        raise PublisherError("No publication intent exists")
    if intent.get("state") in _TERMINAL:
        return intent
    if intent.get("state") not in {"sending", "unknown"}:
        raise PublisherError("Publication intent has invalid state")
    intent["state"] = "unknown"
    observed = _remote_oid(checkout, remote, target_ref)
    if observed is None:
        intent["last_observation"] = "target missing"
    else:
        _run(["git", "fetch", "--no-tags", remote, target_ref], checkout)
        members_in_candidate = all(
            subprocess.run(["git", "merge-base", "--is-ancestor", member["source_head"],
                            intent["candidate"]], cwd=checkout, capture_output=True,
                           timeout=30,
                           env={key: value for key, value in os.environ.items()
                                if not key.startswith("GIT_")} | {"GIT_TERMINAL_PROMPT": "0"}).returncode == 0
            for member in intent.get("members", []))
        candidate_in_target = subprocess.run(
            ["git", "merge-base", "--is-ancestor", intent["candidate"], "FETCH_HEAD"],
            cwd=checkout, capture_output=True, timeout=30,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")} |
                {"GIT_TERMINAL_PROMPT": "0"}).returncode == 0
        view = observe_pr(pr)
        if (members_in_candidate and candidate_in_target and view.get("state") == "MERGED"
                and view.get("number") == intent["pr"]
                and view.get("headRefOid") == intent["candidate"]
                and view.get("baseRefName") == intent["target_ref"].removeprefix("refs/heads/")
                and view.get("headRepository") == _REPOSITORY
                and view.get("mergeCommitOid") == intent["candidate"]
                and _run(["git", "rev-parse", intent["candidate"] + "^{tree}"], checkout) == intent["tree"]):
            intent["state"] = "confirmed"
            intent["observed_target"] = observed
        else:
            intent["last_observation"] = "publication not conclusively confirmed; resend forbidden"
    _write_intent(intent_path, intent)
    return intent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("publish", "reconcile"))
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--prepared", type=Path,
                        help="Existing frozen output from scripts/prepare_bundle.py")
    parser.add_argument("--attestation", type=Path,
                        help="Private host-signed isolated full-test gate result")
    parser.add_argument("--target-ref", help="Required only for GET-only reconcile")
    args = parser.parse_args(argv)
    root = args.checkout.resolve()
    if args.action == "publish":
        if args.prepared is None or args.attestation is None or args.target_ref is not None:
            parser.error("publish requires --prepared and --attestation; omit --target-ref")
        from manual_integration_receipt import prepared_bundle
        frozen = prepared_bundle(root, args.prepared.resolve())
        receipt = _read_attestation(args.attestation.absolute(), root)
        keys, active_key_id, publisher_commit, runner_pins = _read_keyring()
        result = publish_bundle(checkout=root, intent_dir=_INTENT_DIR, remote="origin",
                                frozen_bundle=frozen, pr=args.pr, qualification=receipt,
                                trusted_gate_keys=keys, expected_gate_key_id=active_key_id,
                                trusted_runner_pins=runner_pins,
                                trusted_source_root=Path(__file__).resolve().parents[1],
                                trusted_publisher_commit=publisher_commit)
    else:
        if args.target_ref is None or args.prepared is not None or args.attestation is not None:
            parser.error("reconcile requires --target-ref and accepts no prepared bundle or gate receipt")
        result = reconcile_bundle(checkout=root, intent_dir=_INTENT_DIR, remote="origin",
                                  target_ref=args.target_ref, pr=args.pr)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("state") == "confirmed" else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, PublisherError, ValueError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")), file=sys.stderr)
        raise SystemExit(1)
