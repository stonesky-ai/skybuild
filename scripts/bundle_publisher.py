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
from skybuild.client import Client
from skybuild.integration_workflow import satisfactory_validation
from skybuild.workflow import ResultState, TaskToken, ValidationStage, _result_current
from skybuild.contracts import valid_identifier


class PublisherError(RuntimeError):
    pass


_OID = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REF = re.compile(r"refs/heads/[A-Za-z0-9._/-]+")
_TERMINAL = {"confirmed"}
_REPOSITORY = "stonesky-ai/skybuild"
_DEFAULT_TRUST_CONFIG = Path.home() / ".config/skybuild/trusted-publisher.json"
_DEFAULT_INTENT_DIR = Path.home() / ".local/state/skybuild/publication-intents"


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


def _gh_api(path: str) -> dict:
    result = subprocess.run(["gh", "api", path], text=True, capture_output=True,
                            timeout=30,
                            env={key: value for key, value in os.environ.items()
                                 if not key.startswith("GIT_")} | {"GH_PROMPT_DISABLED": "1"})
    if result.returncode:
        raise PublisherError("GitHub read failed")
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise PublisherError("GitHub returned invalid JSON") from None
    if not isinstance(value, dict):
        raise PublisherError("GitHub returned an invalid object")
    return value


def _github_checks(head: str) -> str:
    """Reject any pending or failed checks; an empty list relies on the signed full gate."""
    checks = _gh_api(f"repos/{_REPOSITORY}/commits/{head}/check-runs?per_page=100")
    status = _gh_api(f"repos/{_REPOSITORY}/commits/{head}/status")
    runs = checks.get("check_runs")
    contexts = status.get("statuses")
    if (not isinstance(runs, list) or not isinstance(contexts, list)
            or type(checks.get("total_count")) is not int
            or checks["total_count"] != len(runs)
            or type(status.get("total_count")) is not int
            or status["total_count"] != len(contexts)):
        return "FAIL"
    if any(not isinstance(run, dict) or run.get("status") != "completed"
           or run.get("conclusion") not in {"success", "neutral"} for run in runs):
        return "FAIL"
    if contexts and (status.get("state") != "success"
                     or any(not isinstance(context, dict) or context.get("state") != "success"
                            for context in contexts)):
        return "FAIL"
    return "PASS"


def observe_github_pr(pr: int) -> dict:
    """Read the bundle PR and its current commit checks through GitHub REST."""
    if type(pr) is not int or not 0 < pr < 2**31:
        raise PublisherError("PR number is invalid")
    raw = _gh_api(f"repos/{_REPOSITORY}/pulls/{pr}")
    try:
        head, base = raw["head"], raw["base"]
        head_repo, base_repo = head["repo"]["full_name"], base["repo"]["full_name"]
        merged = raw["merged"] is True
        return {"number": raw["number"], "state": "MERGED" if merged else str(raw["state"]).upper(),
                "isDraft": raw["draft"], "headRefOid": head["sha"], "headRefName": head["ref"],
                "baseRefOid": base["sha"], "baseRefName": base["ref"],
                "headRepository": head_repo, "baseRepository": base_repo,
                "author": raw["user"]["login"],
                "checks": _github_checks(head["sha"]),
                "mergeCommitOid": raw.get("merge_commit_sha")}
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
    "network_probe_sha256", "attestation_signer_sha256", "execution_host",
    "environment_allowlist", "resource_limits",
}
_TRUST_CONFIG_FIELDS = {"schema", "active_key_id", "publisher_commit", "runner_pins", "keys",
                        "skybuild_client"}


def _read_owner_private(path: Path, *, maximum: int, label: str) -> bytes:
    if not path.is_absolute() or path.resolve() != path:
        raise PublisherError(f"{label} path must be absolute and canonical")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > maximum):
            raise PublisherError(f"{label} must be an owned regular mode-0600 file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(maximum + 1)
    except OSError as error:
        raise PublisherError(f"{label} could not be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > maximum:
        raise PublisherError(f"{label} exceeds its size limit")
    return raw


def _read_trust_config(path: Path, expected_sha256: str, checkout: Path
                       ) -> tuple[dict[str, Path], str, str, dict, dict]:
    """Load owner-private pins; the trusted launcher pins these exact bytes."""
    if (not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or path.resolve().is_relative_to(checkout)):
        raise PublisherError("Trusted config digest or location is invalid")
    raw = _read_owner_private(path, maximum=16 * 1024, label="Trusted publisher config")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PublisherError("Trusted publisher config differs from the launcher-pinned SHA-256")
    try:
        config = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise PublisherError("Trusted publisher config is invalid JSON") from error
    if (not isinstance(config, dict)
            or set(config) != _TRUST_CONFIG_FIELDS
            or config["schema"] != "skybuild.publisher-trust.v1"
            or not isinstance(config["active_key_id"], str)
            or not isinstance(config["publisher_commit"], str)
            or not _OID.fullmatch(config["publisher_commit"])
            or not isinstance(config["keys"], dict) or not config["keys"]
            or len(config["keys"]) > 16):
        raise PublisherError("Trusted publisher config has invalid fields")
    keys = {}
    for key_id, key_path in config["keys"].items():
        if (not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", key_id)
                or not isinstance(key_path, str) or not Path(key_path).is_absolute()
                or Path(key_path).resolve().is_relative_to(checkout)):
            raise PublisherError("Trusted gate key entry is invalid")
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
    for field in ("firewall_policy_sha256", "trusted_entrypoint_sha256", "network_probe_sha256",
                  "attestation_signer_sha256"):
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
    client = config["skybuild_client"]
    if (not isinstance(client, dict)
            or set(client) != {"base_url", "token_path", "ca_file", "ca_sha256"}
            or not isinstance(client["base_url"], str)
            or not client["base_url"].startswith("https://")
            or not isinstance(client["token_path"], str)
            or not Path(client["token_path"]).is_absolute()
            or Path(client["token_path"]).resolve().is_relative_to(checkout)):
        raise PublisherError("Trusted read-only SkyBuild client config is invalid")
    if client["ca_file"] is None:
        if client["ca_sha256"] is not None:
            raise PublisherError("CA digest requires a pinned CA file")
    elif (not isinstance(client["ca_file"], str) or not Path(client["ca_file"]).is_absolute()
          or Path(client["ca_file"]).resolve().is_relative_to(checkout)
          or not isinstance(client["ca_sha256"], str)
          or not re.fullmatch(r"[0-9a-f]{64}", client["ca_sha256"])):
        raise PublisherError("Trusted SkyBuild client CA pin is invalid")
    return keys, active, config["publisher_commit"], runner_pins, client


def _observe_skybuild_task(config: dict, project_id: str, task_id: str,
                           checkout: Path) -> dict:
    client_config = config
    token_path = Path(client_config["token_path"])
    if token_path.resolve().is_relative_to(checkout):
        raise PublisherError("SkyBuild observer credential must be outside the candidate checkout")
    raw = _read_owner_private(token_path, maximum=8192, label="SkyBuild observer token")
    try:
        token = raw.decode("utf-8").strip()
        if not token:
            raise ValueError
        with Client(client_config["base_url"], token, retries=0, timeout=15,
                    ca_file=client_config["ca_file"],
                    expected_ca_sha256=client_config["ca_sha256"]) as client:
            return client.task_workflow(project_id, task_id)
    except Exception as error:
        raise PublisherError("Read-only SkyBuild workflow observation failed") from error


def _read_prepared_json(path: Path, checkout: Path) -> dict:
    if (not path.is_absolute() or path.resolve() != path
            or path.resolve().is_relative_to(checkout)):
        raise PublisherError("Frozen preparation files must be canonical and outside the candidate")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_size > 512 * 1024):
            raise PublisherError("Frozen preparation file is not an owned bounded regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(512 * 1024 + 1)
    except OSError as error:
        raise PublisherError("Frozen preparation file could not be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > 512 * 1024:
        raise PublisherError("Frozen preparation file exceeds its size limit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise PublisherError("Frozen preparation file is invalid JSON") from error
    if not isinstance(value, dict):
        raise PublisherError("Frozen preparation file must be an object")
    return value


def _augment_frozen_workflow(checkout: Path, prepared: Path, frozen_bundle: dict) -> dict:
    """Add validated workflow pins without changing the manual receipt schema."""
    prepared = prepared.absolute()
    if prepared.resolve() != prepared or prepared.resolve().is_relative_to(checkout.resolve()):
        raise PublisherError("Prepared bundle must be canonical and outside the candidate checkout")
    directory = prepared.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid():
        raise PublisherError("Prepared bundle directory must be an owned real directory")
    owner = _read_prepared_json(prepared / "inputs.json", checkout.resolve())
    report = _read_prepared_json(prepared / "report.json", checkout.resolve())
    inputs = owner.get("inputs")
    if not isinstance(inputs, dict):
        raise PublisherError("Frozen preparation inputs are missing")
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    if (owner.get("fingerprint") != fingerprint or report.get("fingerprint") != fingerprint
            or report.get("inputs") != inputs or report.get("ok") is not True
            or report.get("schema") != "skybuild.bundle-preparation.v1"
            or fingerprint != frozen_bundle.get("manifest_sha256")):
        raise PublisherError("Frozen preparation bytes differ from the published bundle fingerprint")
    if (inputs.get("target", {}).get("sha") != frozen_bundle.get("base_commit")
            or inputs.get("target", {}).get("ref") != frozen_bundle.get("target_ref")
            or not isinstance(inputs.get("members"), list)
            or len(inputs["members"]) != len(frozen_bundle.get("members", []))):
        raise PublisherError("Frozen preparation member set differs from the bundle")
    augmented = dict(frozen_bundle)
    members = []
    for source, parsed in zip(inputs["members"], frozen_bundle["members"], strict=True):
        if (not isinstance(source, dict) or not isinstance(source.get("workflow"), dict)
                or source.get("task_id") != parsed.get("task_id")
                or source.get("sha") != parsed.get("source_head")
                or source.get("ref") != parsed.get("source_branch")
                or source.get("reviewer") != parsed.get("reviewer")
                or not isinstance(source.get("review"), dict)):
            raise PublisherError("Frozen input lacks the reviewed member workflow binding")
        review = source["review"]
        artifact_path, review_sha = review.get("path"), review.get("sha256")
        if (not isinstance(artifact_path, str) or not Path(artifact_path).is_absolute()
                or not isinstance(review_sha, str) or not _SHA256.fullmatch(review_sha)
                or review_sha != parsed.get("review_sha256")):
            raise PublisherError("Frozen review artifact reference is invalid")
        try:
            review_bytes = Path(artifact_path).read_bytes()
        except OSError as error:
            raise PublisherError("Frozen review artifact is unavailable") from error
        if hashlib.sha256(review_bytes).hexdigest() != review_sha:
            raise PublisherError("Frozen review artifact bytes changed")
        member = dict(parsed)
        member["workflow"] = source["workflow"]
        member["review_artifact"] = artifact_path + "#sha256=" + review_sha
        members.append(member)
    augmented["members"] = members
    return augmented


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
            and isinstance(pr_view.get("headRefName"), str) and pr_view["headRefName"]
            and pr_view.get("headRepository") == _REPOSITORY
            and pr_view.get("baseRefName") == target_ref.removeprefix("refs/heads/")
            and pr_view.get("baseRepository") == _REPOSITORY
            and pr_view.get("checks") == "PASS")


def _verify_member_workflow(member: dict,
                            observe_task: Callable[[str, str], dict]) -> None:
    """Bind a frozen review artifact to current authenticated SkyBuild evidence."""
    workflow = member.get("workflow")
    fields = {"project_id", "attempt_id", "claim_fence", "input_generation",
              "definition_revision", "policy_version", "target_base"}
    if not isinstance(workflow, dict) or set(workflow) != fields:
        raise PublisherError("Frozen bundle member lacks exact workflow binding")
    if (not valid_identifier(workflow.get("project_id"))
            or not valid_identifier(workflow.get("attempt_id"))
            or not valid_identifier(workflow.get("policy_version"))
            or any(type(workflow.get(key)) is not int or workflow[key] <= 0
                   for key in ("claim_fence", "input_generation", "definition_revision"))
            or not isinstance(workflow.get("target_base"), str)
            or not _OID.fullmatch(workflow["target_base"])):
        raise PublisherError("Frozen member workflow tuple is invalid")
    artifact = member.get("review_artifact")
    digest = member.get("review_sha256")
    if (not isinstance(artifact, str) or not artifact or len(artifact) > 4096
            or any(ord(char) < 32 for char in artifact) or artifact.count("#sha256=") != 1
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            or not artifact.endswith("#sha256=" + digest)):
        raise PublisherError("Frozen member review artifact digest is invalid")
    project, task_id = workflow["project_id"], member["task_id"]
    try:
        response = observe_task(project, task_id)
        token_raw = response["token"]
        token = TaskToken.from_dict(token_raw)
    except Exception as error:
        raise PublisherError("Current SkyBuild task evidence could not be read") from error
    expected = {
        "project_id": token.project_id, "attempt_id": token.attempt_id,
        "claim_fence": token.claim_fence, "input_generation": token.input_generation,
        "definition_revision": token.definition_revision, "policy_version": token.policy_version,
        "target_base": token.target_base,
    }
    if (token.task_id != task_id or token.source_head != member["source_head"]
            or token.source_branch != member["source_branch"] or token.superseded
            or token.pending_action is not None or not satisfactory_validation(token)
            or workflow != expected):
        raise PublisherError("Current SkyBuild task differs from frozen review binding")
    reviews = [item for item in token.evidence
               if item.stage == ValidationStage.CODE_REVIEW
               and item.state == ResultState.PASSED and _result_current(token, item)
               and item.producer == member["reviewer"]
               and item.producer != token.responsible
               and member["review_artifact"] in item.artifacts]
    if len(reviews) != 1:
        raise PublisherError("Current accepted independent code review does not match frozen artifact")


def publish_bundle(*, checkout: Path, intent_dir: Path, remote: str,
                   frozen_bundle: dict, pr: int,
                   qualification: dict,
                   trusted_gate_keys: Mapping[str, Path], expected_gate_key_id: str,
                   trusted_runner_pins: Mapping[str, object],
                   trusted_source_root: Path, trusted_publisher_commit: str,
                   observe_pr: Callable[[int], dict] = observe_github_pr,
                   observe_task: Callable[[str, str], dict]) -> dict:
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
        member_fields = {"task_id", "source_head", "source_branch", "reviewer",
                         "review_sha256", "review_artifact", "workflow"}
        if (not isinstance(member, dict) or set(member) != member_fields
                or not isinstance(member.get("task_id"), str)
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
        if (not isinstance(member["workflow"], dict)
                or member["workflow"].get("target_base") != expected_base):
            raise PublisherError("Frozen member review uses another target base")
        _verify_member_workflow(member, observe_task)
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
        raise PublisherError("Bundle PR is not the exact frozen candidate with passing checks")

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
                and view.get("baseRepository") == _REPOSITORY
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
    parser.add_argument("--trust-config", type=Path, default=_DEFAULT_TRUST_CONFIG,
                        help="Owner-private trust config outside the candidate checkout")
    parser.add_argument("--trust-config-sha256",
                        help="Digest pinned by the trusted launcher; required for publication")
    parser.add_argument("--intent-dir", type=Path, default=_DEFAULT_INTENT_DIR,
                        help="Owner-private durable publication intent directory")
    args = parser.parse_args(argv)
    root = args.checkout.resolve()
    if args.action == "publish":
        if (args.prepared is None or args.attestation is None or args.target_ref is not None
                or args.trust_config_sha256 is None):
            parser.error("publish requires --prepared, --attestation and --trust-config-sha256; omit --target-ref")
        from manual_integration_receipt import prepared_bundle
        prepared_path = args.prepared.absolute()
        frozen = prepared_bundle(root, prepared_path)
        frozen = _augment_frozen_workflow(root, prepared_path, frozen)
        receipt = _read_attestation(args.attestation.absolute(), root)
        keys, active_key_id, publisher_commit, runner_pins, client_config = _read_trust_config(
            args.trust_config.absolute(), args.trust_config_sha256, root)
        result = publish_bundle(checkout=root, intent_dir=args.intent_dir.absolute(), remote="origin",
                                frozen_bundle=frozen, pr=args.pr, qualification=receipt,
                                trusted_gate_keys=keys, expected_gate_key_id=active_key_id,
                                trusted_runner_pins=runner_pins,
                                trusted_source_root=Path(__file__).resolve().parents[1],
                                trusted_publisher_commit=publisher_commit,
                                observe_task=lambda project_id, task_id:
                                    _observe_skybuild_task(client_config, project_id, task_id, root))
    else:
        if args.target_ref is None or args.prepared is not None or args.attestation is not None:
            parser.error("reconcile requires --target-ref and accepts no prepared bundle or gate receipt")
        result = reconcile_bundle(checkout=root, intent_dir=args.intent_dir.absolute(), remote="origin",
                                  target_ref=args.target_ref, pr=args.pr)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("state") == "confirmed" else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, PublisherError, ValueError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")), file=sys.stderr)
        raise SystemExit(1)
