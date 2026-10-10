"""Verify a private, signed, one-use permit for two frozen worker submissions.

This module performs no model calls, network access, hooks, or candidate imports.
The owner provisions every signing key and the consumed-permit directory. A
consumption record is never removed, including after incomplete preparation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

POLICY = "skybuild.two-task-gate-policy.v1"
INPUT = "skybuild.two-task-gate-input.v1"
TRUST = "skybuild.two-task-gate-trust.v1"
APPROVAL = "skybuild.gate-policy-approval.v1"
INTEGRATION = "skybuild.gate-integration.v1"
REVIEW = "skybuild.independent-review.v1"
FOCUSED_INPUT = "skybuild.focused-validation-input.v1"
FOCUSED_RESULT = "skybuild.focused-validation.v1"
PROFILE = "bounded-trusted-cpu-patch-v1"
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
WORKFLOW = {"attempt_id", "claim_fence", "input_generation", "definition_revision",
            "policy_version", "source_sha", "base_sha"}
SOURCE = {"commit", "tree", "script_sha256", "entrypoint_sha256", "network_probe_sha256",
          "attestation_signer_sha256", "policy_module_sha256"}
PROFILE_FIELDS = {"gate_argv", "gate_command_sha256", "gate_policy_sha256", "runner_identity",
                  "runner_version", "firewall_policy_sha256", "network_probe_sha256",
                  "trusted_entrypoint_sha256", "attestation_signer_sha256", "candidate_blocked_until_probe"}
FULL_COMMAND = ["uv", "run", "--extra", "test", "python", "-m", "pytest", "-q"]
FOCUSED_COMMANDS = {
    "petri-client-unit-v1": FULL_COMMAND + ["tests/test_petri_client.py"],
    "petri-client-long-v1": FULL_COMMAND + ["tests/test_petri_workers.py", "tests/test_manual_cord.py"],
    "session-scan-unit-v1": FULL_COMMAND + ["tests/test_session_failure_scan.py", "-k",
                                           "reassembles or prefilter or concatenated or keeps_complete or quoted"],
    "session-scan-long-v1": FULL_COMMAND + ["tests/test_session_failure_scan.py", "-k",
                                           "not (reassembles or prefilter or concatenated or keeps_complete or quoted)"],
}
FOCUSED_FIELDS = {"schema", "permit_id", "policy_sha256", "consumption_sha256", "conductor_intent_sha256",
                  "input_sha256", "project_id", "task_id", "assignment_id", "worker_id", "brief_sha256",
                  "approved_patch_sha256", "source_head", "source_tree", "base_sha", "workflow", "stage",
                  "profile", "command", "command_sha256", "runner_source", "execution_host", "images",
                  "counts", "verdict", "isolation"}


class PolicyError(RuntimeError):
    """Authorization is invalid, expired, consumed, or no longer safe."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fields(value: object, expected: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise PolicyError("Policy object has missing or unknown fields")
    return value


def text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise PolicyError("Policy text is invalid")
    return value


def sha(value: object, *, git: bool = False) -> str:
    if not isinstance(value, str) or not (COMMIT if git else SHA).fullmatch(value):
        raise PolicyError("Policy digest is invalid")
    return value


def timestamp(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise PolicyError("Policy timestamp is invalid") from error
    if result.tzinfo is None:
        raise PolicyError("Policy timestamp must include its timezone")
    return result.astimezone(timezone.utc)


def private(path: Path, maximum: int = 1024 * 1024, *, observation: bool = False) -> bytes:
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise PolicyError("Trusted paths must be absolute and contain no symlinks")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        allowed = not (info.st_mode & 0o022) if observation else stat.S_IMODE(info.st_mode) == 0o600
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or not allowed or info.st_size > maximum):
            raise PolicyError("Trusted input must be an owned private bounded regular file")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise PolicyError("Trusted input exceeded its byte limit")
    return data


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PolicyError("Duplicate JSON fields are forbidden")
        value[key] = item
    return value


def parse(data: bytes) -> dict:
    try:
        value = json.loads(data, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(PolicyError("Nonfinite JSON")))
    except (UnicodeError, ValueError) as error:
        raise PolicyError("Trusted input is not JSON") from error
    if not isinstance(value, dict):
        raise PolicyError("Trusted input must be a JSON object")
    return value


def key_spec(value: object) -> dict:
    value = fields(value, {"key_id", "principal", "key_path"})
    for item in value.values():
        text(item)
    return value


def verify(envelope: object, domain: str, key: dict) -> dict:
    envelope = fields(envelope, {"schema", "key_id", "principal", "payload", "signature"})
    if (envelope["schema"] != domain or envelope["key_id"] != key["key_id"]
            or envelope["principal"] != key["principal"] or not isinstance(envelope["payload"], dict)):
        raise PolicyError("Signed evidence principal, key, or domain differs from trust config")
    material = private(Path(key["key_path"]), 4096)
    if not 32 <= len(material) <= 4096:
        raise PolicyError("Signing key length is invalid")
    expected = hmac.new(material, domain.encode() + b"\x00" + canonical(envelope["payload"]),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sha(envelope["signature"])):
        raise PolicyError("Signed evidence signature does not verify")
    return envelope["payload"]


def sign_focused(isolation: dict, authorization: "Authorization", key_path: Path, key_id: str,
                 counts: dict) -> dict:
    focused = authorization.focused
    if focused is None:
        raise PolicyError("Focused signing requires a consumed focused stage")
    key = authorization.trust["validation"]
    material = private(key_path, 4096)
    if key_id != key["key_id"] or not hmac.compare_digest(material, private(Path(key["key_path"]), 4096)):
        raise PolicyError("Focused signer differs from owner-configured validation key")
    task = focused["task"]
    payload = {"schema": FOCUSED_RESULT, "permit_id": authorization.policy["permit_id"],
               "policy_sha256": authorization.policy_sha256,
               "consumption_sha256": digest(private(authorization.record)),
               "conductor_intent_sha256": focused["conductor_intent_sha256"],
               "input_sha256": authorization.input_sha256, "project_id": authorization.policy["project_id"],
               **{name: task[name] for name in ("task_id", "assignment_id", "worker_id", "brief_sha256", "approved_patch_sha256")},
               "source_head": focused["source_head"], "source_tree": isolation["candidate_tree"],
               "base_sha": authorization.policy["base_sha"], "workflow": focused["workflow"],
               "stage": focused["stage"], "profile": focused["profile"],
               "command": FOCUSED_COMMANDS[focused["profile"]],
               "command_sha256": isolation["gate_command_sha256"],
               "runner_source": authorization.policy["runner_source"],
               "execution_host": authorization.policy["execution_host"], "images": authorization.policy["images"],
               "counts": counts, "verdict": "pass" if (isolation["result"]["exit_code"] == 0
                   and counts["passed"] > 0 and not any(counts[name] for name in
                       ("failed", "errors", "skipped", "xfailed", "xpassed"))) else "fail",
               "isolation": isolation}
    signature = hmac.new(material, FOCUSED_RESULT.encode() + b"\0" + canonical(payload), hashlib.sha256).hexdigest()
    return {"schema": FOCUSED_RESULT, "key_id": key_id, "principal": key["principal"],
            "payload": payload, "signature": signature}


def verify_focused(envelope: dict, key: dict, expected: dict) -> dict:
    payload = fields(verify(envelope, FOCUSED_RESULT, key), FOCUSED_FIELDS)
    if payload["schema"] != FOCUSED_RESULT or any(payload.get(name) != value for name, value in expected.items()):
        raise PolicyError("Focused result differs from exact approved task/source/stage")
    counts = fields(payload["counts"], {"collected", "selected", "passed", "failed", "errors", "skipped", "deselected", "xfailed", "xpassed"})
    if (any(type(value) is not int or value < 0 for value in counts.values())
            or counts["selected"] != sum(counts[name] for name in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed"))
            or counts["collected"] != counts["selected"] + counts["deselected"]
            or counts["passed"] < 1 or any(counts[name] for name in ("failed", "errors", "skipped", "xfailed", "xpassed"))
            or payload["verdict"] != "pass"):
        raise PolicyError("Focused PASS requires actual nonempty passing collection")
    isolation = payload["isolation"]
    if (not isinstance(isolation, dict) or isolation.get("result", {}).get("exit_code") != 0
            or isolation.get("cleanup", {}).get("status") != "confirmed"
            or isolation.get("gate_argv") != payload["command"]
            or isolation.get("candidate_commit") != payload["source_head"]
            or isolation.get("candidate_tree") != payload["source_tree"]
            or payload["profile"] not in FOCUSED_COMMANDS
            or payload["command"] != FOCUSED_COMMANDS[payload["profile"]]
            or payload["command_sha256"] != digest(json.dumps(payload["command"]).encode())):
        raise PolicyError("Focused result lacks exact command, exit, source, or cleanup evidence")
    return payload


def outside(path: Path, checkout: Path) -> None:
    if path.resolve().is_relative_to(checkout.resolve()):
        raise PolicyError("Trusted authorization material must be outside candidate checkout")


def consume(directory: Path, permit: dict, policy_sha256: str, input_sha256: str,
            conductor_intent_sha256: str, stage: str = "gate") -> Path:
    if not directory.is_absolute() or directory.resolve() != directory:
        raise PolicyError("Consumption directory must be an absolute nonsymlink path")
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise PolicyError("Consumption directory must be owned and mode 0700")
    # The permit ID, not its bytes, is the replay key. Re-signing the same ID
    # cannot create another run. O_EXCL arbitrates concurrent trusted callers.
    if stage not in {"gate", "focused-0-unit", "focused-0-long", "focused-1-unit", "focused-1-long"}:
        raise PolicyError("Unknown one-use validation stage")
    name = digest(text(permit["permit_id"]).encode()) + "." + stage + ".consumed.json"
    parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical({"schema": "skybuild.gate-permit-consumption.v1",
                                    "permit_id": permit["permit_id"], "policy_sha256": policy_sha256,
                                    "input_sha256": input_sha256, "state": "consumed_hold_on_unknown",
                                    "conductor_intent_sha256": conductor_intent_sha256,
                                    "stage": stage,
                                    "consumed_at": datetime.now(timezone.utc).isoformat()}))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
    except FileExistsError as error:
        raise PolicyError("Permit already consumed; partial and unknown runs cannot replay") from error
    finally:
        os.close(parent)
    return directory / name


def _path(value: object) -> str:
    value = text(value)
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or ".." in path.parts
            or path.parts[0] == ".git" or "\n" in value or "\t" in value):
        raise PolicyError("Approved owned path is unsafe")
    return value


def _git(checkout: Path, *args: str, environment: dict | None = None, data: bytes | None = None) -> bytes:
    # No inherited Git configuration, hooks, attributes filters, credentials,
    # signing program, replacement objects, or alternates participate.
    env = {"PATH": os.defpath, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1",
           "GIT_TERMINAL_PROMPT": "0"}
    if environment:
        env.update(environment)
    def limit():
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(checkout),
                                 "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                 *args], env=env, input=data, stdout=output,
                                stderr=subprocess.DEVNULL, timeout=30, check=False,
                                preexec_fn=limit)
        if result.returncode or output.tell() >= 4 * 1024 * 1024:
            raise PolicyError("Frozen source proof failed or exceeded its bound")
        output.seek(0)
        return output.read()


def _parents(checkout: Path, head: str) -> list[str]:
    return _git(checkout, "show", "-s", "--format=%P", head).decode().strip().split()


def _tree(checkout: Path, head: str) -> str:
    return _git(checkout, "rev-parse", head + "^{tree}").decode().strip()


def _apply(checkout: Path, index: Path, tree: str, patch: bytes, objects: Path) -> str:
    source_objects = Path(_git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip())
    if (not source_objects.is_absolute() or source_objects.resolve() != source_objects
            or not source_objects.is_dir() or ":" in str(source_objects)
            or source_objects.stat().st_uid != os.geteuid() or source_objects.stat().st_mode & 0o022
            or (source_objects / "info/alternates").exists() or (source_objects / "info/alternates").is_symlink()):
        raise PolicyError("Source object directory is unsafe or contains untrusted alternates")
    env = {"GIT_INDEX_FILE": str(index), "GIT_OBJECT_DIRECTORY": str(objects),
           "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(source_objects)}
    _git(checkout, "read-tree", tree, environment=env)
    _git(checkout, "apply", "--cached", "--whitespace=nowarn", "-", environment=env, data=patch)
    return _git(checkout, "write-tree", environment=env).decode().strip()


def _verify_sources(checkout: Path, policy: dict, frozen: dict, reviewers: list[dict],
                    validation: dict, policy_sha256: str, state_dir: Path) -> None:
    members = frozen["members"]
    if not isinstance(members, list) or len(members) != 2:
        raise PolicyError("Exactly two ordered frozen members are required")
    base = policy["base_sha"]
    candidate = frozen["candidate_commit"]
    second_parents = _parents(checkout, candidate)
    if len(second_parents) != 2:
        raise PolicyError("Candidate must be the second exact two-parent merge")
    first_merge = second_parents[0]
    first_parents = _parents(checkout, first_merge)
    if first_parents != [base, members[0].get("head_sha")] or second_parents[1] != members[1].get("head_sha"):
        raise PolicyError("Candidate merge chain differs from the two frozen members")
    with tempfile.TemporaryDirectory(prefix="skybuild-policy-source-") as temporary:
        scratch = Path(temporary)
        objects = scratch / "objects"
        objects.mkdir()
        base_tree = _tree(checkout, base)
        combined = base_tree
        for position, (task, member) in enumerate(zip(policy["tasks"], members, strict=True)):
            fields(member, {"task_id", "assignment_id", "worker_id", "brief_sha256", "head_sha", "workflow",
                            "code_review", "review", "review_artifact_path", "focused_results"})
            head = sha(member["head_sha"], git=True)
            if any(member[name] != task[name] for name in ("task_id", "assignment_id", "worker_id", "brief_sha256")):
                raise PolicyError("Frozen task identity differs from approved assignment")
            workflow = fields(member["workflow"], WORKFLOW)
            if (workflow["source_sha"] != head or workflow["base_sha"] != base
                    or workflow["definition_revision"] != task["definition_revision"]
                    or type(workflow["definition_revision"]) is not int or workflow["definition_revision"] < 1
                    or workflow["policy_version"] != task["policy_version"]
                    or type(workflow["claim_fence"]) is not int or workflow["claim_fence"] < 1
                    or type(workflow["input_generation"]) is not int or workflow["input_generation"] < 1):
                raise PolicyError("Frozen workflow tuple differs from approved task/source")
            text(workflow["attempt_id"])
            evidence = fields(member["code_review"], {"kind", "producer", "artifact_uri", "artifact_sha256"})
            principal = evidence["producer"]
            keys = [key for key in reviewers if key["principal"] == principal]
            if (len(keys) != 1 or principal == task["worker_id"]
                    or evidence["kind"] != "independent_code_review"):
                raise PolicyError("Review principal is not independent and trusted")
            review = verify(member["review"], REVIEW, keys[0])
            fields(review, {"project_id", "task_id", "source_head", "source_branch", "base_sha",
                            *WORKFLOW, "reviewer_principal", "verdict", "artifact_uri",
                            "artifact_sha256", "issued_at"})
            expected = {"project_id": policy["project_id"], "task_id": task["task_id"],
                        "source_head": head, "source_branch": task["task_branch"], "base_sha": base,
                        **workflow, "reviewer_principal": principal, "verdict": "pass",
                        "artifact_uri": evidence["artifact_uri"], "artifact_sha256": evidence["artifact_sha256"]}
            if any(review[name] != value for name, value in expected.items()):
                raise PolicyError("Independent signed review does not bind frozen API evidence")
            if not timestamp(policy["issued_at"]) <= timestamp(review["issued_at"]) <= timestamp(frozen["frozen_at"]):
                raise PolicyError("Independent review is outside approved frozen interval")
            artifact = Path(member["review_artifact_path"])
            outside(artifact, checkout)
            if digest(private(artifact)) != sha(review["artifact_sha256"]):
                raise PolicyError("Actual independent review artifact differs from signed digest")
            patch_path = Path(task["patch_path"])
            outside(patch_path, checkout)
            patch = private(patch_path)
            if digest(patch) != task["approved_patch_sha256"]:
                raise PolicyError("Approved patch bytes changed")
            # A worker submission is one commit on the exact approved base.
            if _parents(checkout, head) != [base]:
                raise PolicyError("Worker submission must be one commit on the approved base")
            paths = _git(checkout, "diff-tree", "--no-commit-id", "--no-renames", "-r", "--name-only", "-z", base, head)
            actual_paths = [item.decode("utf-8") for item in paths.split(b"\x00") if item]
            if not actual_paths or any(_path(path) not in task["owned_paths"] for path in actual_paths):
                raise PolicyError("Worker diff exceeds exact approved owned paths")
            member_tree = _apply(checkout, scratch / ("member-" + str(position)), base_tree, patch, objects)
            if member_tree != _tree(checkout, head):
                raise PolicyError("Worker tree differs from trusted application of approved patch")
            results = fields(member["focused_results"], {"unit", "long"})
            for stage in ("unit", "long"):
                reference = fields(results[stage], {"path", "sha256"})
                receipt_path = Path(reference["path"])
                outside(receipt_path, checkout)
                receipt_data = private(receipt_path)
                if digest(receipt_data) != sha(reference["sha256"]):
                    raise PolicyError("Actual focused receipt differs from frozen API evidence")
                stage_name = "focused-" + str(position) + "-" + stage
                intent_path = state_dir / (digest(policy["permit_id"].encode()) + "." + stage_name + ".consumed.json")
                intent_data = private(intent_path)
                intent = fields(parse(intent_data), {"schema", "permit_id", "policy_sha256", "input_sha256",
                                                     "conductor_intent_sha256", "state", "consumed_at", "stage"})
                if (intent["schema"] != "skybuild.gate-permit-consumption.v1" or intent["permit_id"] != policy["permit_id"]
                        or intent["policy_sha256"] != policy_sha256 or intent["stage"] != stage_name
                        or intent["state"] != "consumed_hold_on_unknown"
                        or intent["conductor_intent_sha256"] != frozen["conductor_intent_sha256"]):
                    raise PolicyError("Focused receipt stage does not have its immutable linked intent")
                verify_focused(parse(receipt_data), validation, {
                    "permit_id": policy["permit_id"], "policy_sha256": policy_sha256,
                    "consumption_sha256": digest(intent_data), "input_sha256": intent["input_sha256"],
                    "conductor_intent_sha256": frozen["conductor_intent_sha256"],
                    "project_id": policy["project_id"], "task_id": task["task_id"],
                    "assignment_id": task["assignment_id"], "worker_id": task["worker_id"],
                    "brief_sha256": task["brief_sha256"], "approved_patch_sha256": task["approved_patch_sha256"],
                    "source_head": head, "source_tree": member_tree, "base_sha": policy["base_sha"],
                    "workflow": workflow, "stage": stage, "profile": task["focused_profiles"][stage],
                    "runner_source": policy["runner_source"], "execution_host": policy["execution_host"],
                    "images": policy["images"],
                })
            combined = _apply(checkout, scratch / ("combined-" + str(position)), combined, patch, objects)
            if position == 0 and combined != _tree(checkout, first_merge):
                raise PolicyError("First merge tree differs from approved patch application")
        if combined != frozen["candidate_tree"] or combined != _tree(checkout, candidate):
            raise PolicyError("Candidate tree differs from both approved patches")


class Authorization:
    """A consumed permit plus continuously checked CPU-only admission evidence."""

    def __init__(self, policy: dict, record: Path, input_sha256: str):
        self.policy = policy
        self.record = record
        self.input_sha256 = input_sha256
        self.failure: Exception | None = None
        self.done = threading.Event()
        self.thread: threading.Thread | None = None
        self.focused: dict | None = None
        self.trust: dict = {}
        self.policy_sha256 = ""
        self.predicate_sha256 = ""
        now = datetime.now(timezone.utc)
        expiry_values = [policy[name] for name in ("expires_at",) if name in policy]
        if policy.get("usage", {}).get("valid_until"):
            expiry_values.append(policy["usage"]["valid_until"])
        self.deadline = time.monotonic() + min(
            [(timestamp(value) - now).total_seconds() for value in expiry_values], default=0)

    def check(self, *, starting: bool = False) -> None:
        if self.failure:
            raise PolicyError("Continuous policy resource/usage watch failed") from self.failure
        now = datetime.now(timezone.utc)
        if now >= timestamp(self.policy["expires_at"]) or time.monotonic() >= self.deadline:
            raise PolicyError("One-shot approval expired")
        usage_pin = self.policy["usage"]
        usage_data = private(Path(usage_pin["path"]), observation=True)
        usage = parse(usage_data)
        if (digest(usage_data) != usage_pin["sha256"] or usage.get("production_must_drain") is not False
                or type(usage.get("weekly_used_percent")) not in (int, float)
                or not 0 <= usage["weekly_used_percent"] < 50
                or usage.get("stop_production_percent") != 50
                or usage.get("valid_until") != usage_pin["valid_until"]
                or not timestamp(usage["confirmed_at"]) <= now < timestamp(usage["valid_until"])):
            raise PolicyError("Fresh owner usage evidence is absent or production must drain")
        watch = self.policy["host_watch"]
        host = parse(private(Path(watch["path"]), observation=True))
        age = (now - timestamp(host.get("sampled_at"))).total_seconds()
        required = watch["required_available_bytes"] if starting else watch["reserve_bytes"]
        if (not 0 <= age <= watch["max_age_seconds"] or host.get("status") != "ok"
                or host.get("reserve_bytes") != watch["reserve_bytes"]
                or type(host.get("available_bytes")) is not int or host["available_bytes"] < required):
            raise PolicyError("Fresh host-watch admission or reserve is absent")
        capacity = host.get("capacity")
        if starting and (not isinstance(capacity, dict) or capacity.get("status") != "ok"
                         or type(capacity.get("max_new_jobs")) is not int or capacity["max_new_jobs"] < 1):
            raise PolicyError("Host-watch capacity cannot admit one isolated gate")
        available = None
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) * 1024
                break
        if available is None or available < required:
            raise PolicyError("Actual host memory fails policy admission/reserve")
        if shutil.disk_usage(watch["disk_path"]).free < watch["disk_reserve_bytes"]:
            raise PolicyError("Actual host disk reserve fails policy admission")

    def start(self) -> None:
        self.check(starting=True)
        def watch():
            while not self.done.wait(2):
                try:
                    self.check()
                except Exception as error:
                    self.failure = error
                    return
        self.thread = threading.Thread(target=watch, name="gate-policy-resource-watch", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.done.set()
        if self.thread:
            self.thread.join(timeout=3)


def _authorize_focused(checkout: Path, predicate: dict, key_id: str, policy: dict, frozen: dict,
                       stage: str, record: Path, input_sha256: str, policy_sha256: str,
                       conductor_digest: str, trust: dict) -> Authorization:
    fields(frozen, {"schema", "policy_sha256", "project_id", "target_ref", "base_sha", "task_id",
                    "assignment_id", "worker_id", "brief_sha256", "head_sha", "workflow",
                    "candidate_tree", "candidate_archive_sha256", "candidate_history_sha256",
                    "conductor_intent_sha256", "submitted_at", "stage"})
    match = re.fullmatch(r"focused-([01])-(unit|long)", stage)
    if match is None:
        raise PolicyError("Focused stage is outside the reviewed four-stage whitelist")
    task = policy["tasks"][int(match[1])]
    workflow = fields(frozen["workflow"], WORKFLOW)
    now = datetime.now(timezone.utc)
    if (frozen["schema"] != FOCUSED_INPUT or frozen["policy_sha256"] != policy_sha256
            or frozen["stage"] != match[2] or frozen["conductor_intent_sha256"] != conductor_digest
            or not timestamp(policy["issued_at"]) <= timestamp(frozen["submitted_at"]) <= now
            or any(frozen[name] != task[name] for name in ("task_id", "assignment_id", "worker_id", "brief_sha256"))
            or any(frozen[name] != policy[name] for name in ("project_id", "target_ref", "base_sha"))
            or workflow["source_sha"] != frozen["head_sha"] or workflow["base_sha"] != policy["base_sha"]
            or workflow["definition_revision"] != task["definition_revision"]
            or workflow["policy_version"] != task["policy_version"]
            or type(workflow["definition_revision"]) is not int
            or type(workflow["claim_fence"]) is not int or workflow["claim_fence"] < 1
            or type(workflow["input_generation"]) is not int or workflow["input_generation"] < 1
            or key_id != trust["validation"]["key_id"]):
        raise PolicyError("Focused input differs from owner task/patch/stage authorization")
    text(workflow["attempt_id"])
    if (predicate["candidate_commit"] != sha(frozen["head_sha"], git=True)
            or predicate["target_base"] != policy["base_sha"]
            or predicate["execution_host"] != policy["execution_host"]
            or any(predicate[name] != frozen[name] for name in ("candidate_tree", "candidate_archive_sha256", "candidate_history_sha256"))
            or any(predicate[name] != value for name, value in policy["images"].items())):
        raise PolicyError("Focused predicate differs from signed frozen submission")
    head = frozen["head_sha"]
    if _parents(checkout, head) != [policy["base_sha"]]:
        raise PolicyError("Focused worker submission must be one commit on approved base")
    paths = _git(checkout, "diff-tree", "--no-commit-id", "--no-renames", "-r", "--name-only", "-z", policy["base_sha"], head)
    actual = [item.decode("utf-8") for item in paths.split(b"\x00") if item]
    if not actual or any(_path(path) not in task["owned_paths"] for path in actual):
        raise PolicyError("Focused worker diff exceeds approved exact owned paths")
    patch_path = Path(task["patch_path"])
    outside(patch_path, checkout)
    patch = private(patch_path)
    if digest(patch) != task["approved_patch_sha256"]:
        raise PolicyError("Focused approved patch bytes changed")
    with tempfile.TemporaryDirectory(prefix="skybuild-focused-source-") as temporary:
        scratch = Path(temporary)
        objects = scratch / "objects"
        objects.mkdir()
        actual_tree = _apply(checkout, scratch / "index", _tree(checkout, policy["base_sha"]), patch, objects)
        if actual_tree != _tree(checkout, head) or actual_tree != frozen["candidate_tree"]:
            raise PolicyError("Focused worker tree differs from approved patch application")
    authorization = Authorization(policy, record, input_sha256)
    authorization.trust = trust
    authorization.policy_sha256 = policy_sha256
    authorization.predicate_sha256 = digest(canonical(predicate))
    authorization.focused = {"task": task, "workflow": workflow, "stage": match[2],
                             "profile": task["focused_profiles"][match[2]], "source_head": head,
                             "conductor_intent_sha256": conductor_digest}
    authorization.check(starting=True)
    return authorization


def authorize(checkout: Path, predicate: dict, key_id: str, policy_path: Path,
              policy_sha256: str, trust_path: Path, trust_sha256: str, input_path: Path,
              runner_source: dict, resource_limits: dict, *, focused_stage: str | None = None) -> Authorization:
    for path in (policy_path, trust_path, input_path):
        outside(path, checkout)
    trust_data = private(trust_path)
    if digest(trust_data) != sha(trust_sha256):
        raise PolicyError("Owner trust config bytes differ from approved pin")
    trust = fields(parse(trust_data), {"schema", "approval", "integration", "reviewers", "validation", "consume_dir"})
    if trust["schema"] != TRUST or not isinstance(trust["reviewers"], list) or not trust["reviewers"]:
        raise PolicyError("Owner trust config schema is invalid")
    approval, integration = key_spec(trust["approval"]), key_spec(trust["integration"])
    reviewers = [key_spec(key) for key in trust["reviewers"]]
    validation = key_spec(trust["validation"])
    keys = [approval, integration, validation, *reviewers]
    if (len({key["principal"] for key in keys}) != len(keys)
            or len({key["key_id"] for key in keys}) != len(keys)
            or len({key["key_path"] for key in keys}) != len(keys)):
        raise PolicyError("Approval, integration, and review keys/principals must be separate")
    for key in keys:
        outside(Path(key["key_path"]), checkout)
    policy_data = private(policy_path)
    if digest(policy_data) != sha(policy_sha256):
        raise PolicyError("Owner permit bytes differ from approved pin")
    policy = verify(parse(policy_data), APPROVAL, approval)
    fields(policy, {"schema", "permit_id", "project_id", "target_ref", "base_sha", "tasks",
                    "source_profile", "runner_source", "images", "execution_host", "resource_limits",
                    "attestation_key_id", "gate_profile", "delivery", "usage", "host_watch", "issued_at", "expires_at", "max_runs"})
    now = datetime.now(timezone.utc)
    if (policy["schema"] != POLICY or policy["project_id"] != "skybuild" or policy["max_runs"] != 1
            or type(policy["max_runs"]) is not int or policy["source_profile"] != PROFILE
            or not timestamp(policy["issued_at"]) <= now < timestamp(policy["expires_at"])):
        raise PolicyError("One-shot permit schema, profile, run limit, or validity is invalid")
    text(policy["permit_id"])
    # Consume before parsing the frozen manifest, Git preparation, image
    # inspection, listeners, Docker, or any other candidate-specific effect.
    input_data = private(input_path)
    consume_dir = Path(trust["consume_dir"])
    outside(consume_dir, checkout)
    conductor_path = consume_dir / (digest(policy["permit_id"].encode()) + ".conductor.consumed.json")
    conductor_data = private(conductor_path)
    conductor_digest = digest(conductor_data)
    record = consume(consume_dir, policy, policy_sha256, digest(input_data), conductor_digest,
                     focused_stage or "gate")
    delivery = fields(policy["delivery"], {"conductor_source_sha", "integration_source_sha",
                                          "publisher_source_sha", "publisher_trust_sha256",
                                          "gate_trust_sha256", "validation_runtime_sha256", "pr_source_ref", "state_dir"})
    for name in ("conductor_source_sha", "integration_source_sha", "publisher_source_sha"):
        sha(delivery[name], git=True)
    sha(delivery["publisher_trust_sha256"])
    sha(delivery["validation_runtime_sha256"])
    if (delivery["gate_trust_sha256"] != trust_sha256 or delivery["state_dir"] != str(consume_dir)
            or not text(delivery["pr_source_ref"]).startswith("task/")):
        raise PolicyError("Stage trust, state root, or exact PR source branch differs from permit")
    conductor = fields(parse(conductor_data), {"schema", "permit_id", "policy_sha256",
                                               "conductor_source_sha", "integration_source_sha",
                                               "consumed_at", "state"})
    if (conductor["schema"] != "skybuild.conductor-permit-consumption.v1"
            or conductor["permit_id"] != policy["permit_id"] or conductor["policy_sha256"] != policy_sha256
            or conductor["state"] != "consumed_hold_on_unknown"
            or any(conductor[name] != delivery[name] for name in ("conductor_source_sha", "integration_source_sha"))
            or not timestamp(policy["issued_at"]) <= timestamp(conductor["consumed_at"]) <= now):
        raise PolicyError("Immutable conductor intent differs from owner stage authorization")
    fields(policy["runner_source"], SOURCE)
    if policy["runner_source"] != runner_source or policy["resource_limits"] != resource_limits:
        raise PolicyError("Reviewed source or resource limits differ from owner permit")
    fields(policy["images"], {"runner_image_id", "postgres_image_id", "firewall_image_id"})
    fields(policy["gate_profile"], PROFILE_FIELDS)
    if any(predicate[name] != value for name, value in policy["gate_profile"].items()):
        raise PolicyError("Frozen gate profile differs from owner permit")
    fields(policy["usage"], {"path", "sha256", "valid_until"})
    fields(policy["host_watch"], {"path", "max_age_seconds", "reserve_bytes", "required_available_bytes",
                                 "disk_path", "disk_reserve_bytes"})
    watch = policy["host_watch"]
    if (any(type(watch[name]) is not int for name in ("max_age_seconds", "reserve_bytes", "required_available_bytes", "disk_reserve_bytes"))
            or watch["max_age_seconds"] != 120 or watch["reserve_bytes"] != 8 * 1024**3
            or watch["required_available_bytes"] != 14 * 1024**3):
        raise PolicyError("Host-watch policy must preserve 8 GiB beyond all 6 GiB container caps")
    if watch["disk_reserve_bytes"] != 4 * 1024**3 or not Path(watch["disk_path"]).is_absolute():
        raise PolicyError("Host disk reserve must preserve at least the reviewed 4 GiB")
    for observation in (policy["usage"], watch):
        outside(Path(observation["path"]), checkout)
    if not isinstance(policy["tasks"], list) or len(policy["tasks"]) != 2:
        raise PolicyError("Owner permit must approve exactly two tasks")
    seen = {name: set() for name in ("task_id", "assignment_id", "worker_id", "task_branch")}
    for task in policy["tasks"]:
        fields(task, {"task_id", "assignment_id", "worker_id", "task_branch", "brief_sha256",
                      "approved_patch_sha256", "patch_path", "owned_paths", "base_sha",
                      "definition_revision", "policy_version", "focused_profiles", "brief_path"})
        for name, values in seen.items():
            if text(task[name]) in values:
                raise PolicyError("Approved task identities must be distinct")
            values.add(task[name])
        if task["base_sha"] != sha(policy["base_sha"], git=True):
            raise PolicyError("Approved worker base differs from target base")
        sha(task["brief_sha256"])
        brief_path = Path(task["brief_path"])
        outside(brief_path, checkout)
        if digest(private(brief_path)) != task["brief_sha256"]:
            raise PolicyError("Actual amended task brief differs from approved owner digest")
        sha(task["approved_patch_sha256"])
        if type(task["definition_revision"]) is not int or task["definition_revision"] < 1:
            raise PolicyError("Approved definition revision must be a positive integer")
        text(task["policy_version"])
        profiles = fields(task["focused_profiles"], {"unit", "long"})
        if (any(profiles[stage] not in FOCUSED_COMMANDS or not profiles[stage].endswith("-" + stage + "-v1")
                for stage in ("unit", "long"))
                or profiles["unit"].split("-unit-", 1)[0] != profiles["long"].split("-long-", 1)[0]):
            raise PolicyError("Focused commands must select one reviewed fixed task profile")
        if (not isinstance(task["owned_paths"], list) or not task["owned_paths"]
                or len(task["owned_paths"]) > 32 or len(set(task["owned_paths"])) != len(task["owned_paths"])):
            raise PolicyError("Owned paths must be a bounded exact list")
        for path in task["owned_paths"]:
            _path(path)
    if set(policy["tasks"][0]["owned_paths"]) & set(policy["tasks"][1]["owned_paths"]):
        raise PolicyError("Two approved tasks must own disjoint paths")
    frozen = verify(parse(input_data), INTEGRATION, integration)
    if focused_stage is not None:
        return _authorize_focused(checkout, predicate, key_id, policy, frozen, focused_stage,
                                  record, digest(input_data), policy_sha256, conductor_digest, trust)
    fields(frozen, {"schema", "policy_sha256", "project_id", "target_ref", "base_sha", "members",
                    "prepared_manifest_sha256", "candidate_commit", "candidate_tree",
                    "candidate_archive_sha256", "candidate_history_sha256", "bundle_id", "pr_number",
                    "pr_head_sha", "pr_base_sha", "pr_source_ref", "conductor_intent_sha256", "frozen_at"})
    if (frozen["schema"] != INPUT or frozen["policy_sha256"] != policy_sha256
            or not timestamp(policy["issued_at"]) <= timestamp(frozen["frozen_at"]) <= now):
        raise PolicyError("Signed frozen integration input differs from approved permit")
    for name in ("project_id", "target_ref", "base_sha"):
        if frozen[name] != policy[name]:
            raise PolicyError("Signed integration target differs from approved permit")
    for name in ("candidate_commit", "candidate_tree", "candidate_archive_sha256",
                 "candidate_history_sha256", "bundle_id", "pr_number", "target_ref"):
        if frozen[name] != predicate[name]:
            raise PolicyError("Expected predicate differs from signed frozen integration input")
    if (predicate["target_base"] != policy["base_sha"] or frozen["pr_base_sha"] != policy["base_sha"]
            or frozen["pr_source_ref"] != delivery["pr_source_ref"]
            or frozen["conductor_intent_sha256"] != conductor_digest
            or frozen["pr_head_sha"] != frozen["candidate_commit"]
            or predicate["execution_host"] != policy["execution_host"]
            or key_id != policy["attestation_key_id"]
            or any(predicate[name] != value for name, value in policy["images"].items())):
        raise PolicyError("Gate host, images, signer, or PR tuple differs from permit")
    sha(frozen["prepared_manifest_sha256"])
    sha(frozen["candidate_commit"], git=True)
    sha(frozen["candidate_tree"], git=True)
    _verify_sources(checkout, policy, frozen, reviewers, validation, policy_sha256, consume_dir)
    authorization = Authorization(policy, record, digest(input_data))
    authorization.trust = trust
    authorization.policy_sha256 = policy_sha256
    authorization.predicate_sha256 = digest(canonical(predicate))
    authorization.check(starting=True)
    return authorization
