#!/usr/bin/env python3
"""Build and submit one signed integration statement from reviewed host evidence.

The packet contains no caller-supplied guard facts. Unknown POST outcomes retain
their intent and require read-only journal reconciliation; this CLI never retries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parent)]

from skybuild.client import Client, ca_file_sha256
from skybuild.completion import generation
from skybuild.fleet_preflight import _token_from_file, _resolved_addresses
from skybuild.manual_dispatch import _private_endpoint
from skybuild.store import Store
from skybuild.trusted_integration import (SCHEMA, _checked_packet, _key, sign,
                                          verify_signature)
from skybuild.manual_integration import binding, digest, validation_digest, DEFAULT_GATE_COMMAND, DEFAULT_GATE_COMMAND_SHA256
from skybuild.workflow import Place
from manual_integration_receipt import prepared_bundle, read_json


class ProducerError(ValueError):
    pass


def _source(checkout: Path, expected_head: str) -> None:
    """Bind this loaded trusted producer to one clean reviewed source commit."""
    if not isinstance(expected_head, str) or len(expected_head) != 40 or any(
            char not in "0123456789abcdef" for char in expected_head):
        raise ProducerError("Trusted source commit is invalid")
    root = Path(__file__).resolve().parents[1]
    if checkout.resolve() != root or checkout.is_symlink():
        raise ProducerError("Producer and integration checkout differ")
    if any(name.startswith("GIT_") and name != "GIT_PAGER" for name in os.environ):
        raise ProducerError("Git environment overrides are forbidden")
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL="/dev/null",
                       GIT_CONFIG_SYSTEM="/dev/null")

    def git(*arguments: str) -> bytes:
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null",
                                 "-c", "core.fsmonitor=false", *arguments],
                                cwd=root, env=environment, capture_output=True, timeout=15)
        if result.returncode:
            raise ProducerError("Trusted source Git verification failed")
        return result.stdout

    if git("rev-parse", "HEAD").decode().strip() != expected_head or git(
            "status", "--porcelain", "--untracked-files=all"):
        raise ProducerError("Trusted producer checkout is not the clean approved commit")
    loaded = [sys.modules[__name__], sys.modules[Client.__module__],
              sys.modules[Store.__module__], sys.modules[sign.__module__],
              sys.modules[binding.__module__]]
    loaded.extend(sys.modules[name] for name in ("manual_integration_receipt", "_repo_guard",
                                                  "bundle_publisher", "trusted_gate_attestation")
                  if name in sys.modules)
    launcher = sys.modules.get("__main__")
    if launcher is not None and isinstance(getattr(launcher, "__file__", None), str):
        if Path(launcher.__file__).resolve().is_relative_to(root):
            loaded.append(launcher)
    loaded.extend(module for module in tuple(sys.modules.values())
                  if isinstance(getattr(module, "__file__", None), str)
                  and Path(module.__file__).resolve().is_relative_to(root)
                  and Path(module.__file__).resolve().suffix == ".py"
                  and Path(module.__file__).resolve().relative_to(root).parts[0] in {"src", "scripts"})
    checked = set()
    for module in loaded:
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(root):
            raise ProducerError("Loaded producer module is outside trusted source")
        relative = str(path.relative_to(root))
        if relative in checked:
            continue
        checked.add(relative)
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(
                git("show", expected_head + ":" + relative)).digest():
            raise ProducerError("Loaded producer source bytes differ from approved commit")


def _save(path: Path, value: dict) -> None:
    if not path.is_absolute() or path != path.resolve() or path.exists():
        raise ProducerError("Use one new canonical private evidence path")
    parent = path.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ProducerError("Producer evidence directory must be private and owned")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read(path: Path) -> dict:
    if not path.is_absolute() or path != path.resolve():
        raise ProducerError("Evidence path must be canonical")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= 65536):
            raise ProducerError("Evidence must be a bounded owned private file")
        value = json.loads(os.read(descriptor, 65537))
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ProducerError("Evidence must be a JSON object")
    return value


def _bundle(checkout: Path, prepared: Path) -> dict:
    frozen = prepared_bundle(checkout, prepared)
    inputs = read_json(prepared / "inputs.json")["inputs"]
    if len(inputs["members"]) != len(frozen["members"]):
        raise ProducerError("Frozen member count differs")
    members = []
    for source, member in zip(inputs["members"], frozen["members"], strict=True):
        review = source["review"]
        if (source["task_id"] != member["task_id"] or source["sha"] != member["source_head"]
                or review["sha256"] != member["review_sha256"]):
            raise ProducerError("Frozen member review differs")
        members.append({**member, "review_artifact": review["path"] + "#sha256=" + review["sha256"]})
    return {name: frozen[name] for name in ("bundle_id", "manifest_sha256", "policy_sha256",
                                            "target_ref", "base_commit", "candidate_commit",
                                            "candidate_tree")} | {
        "members": members}


def _base_packet(view: dict, bundle: dict, *, event: str, operation_id: str,
                 producer: str, key_id: str) -> dict:
    token = Store.workflow_token(view["task"])
    if view.get("token") != token.to_dict() or token.revision != view["task"]["revision"]:
        raise ProducerError("Workflow projection differs from current task")
    return {"schema": SCHEMA, "event": event, "operation_id": operation_id,
            "expected_revision": token.revision, "producer": producer, "key_id": key_id,
            "binding": binding(token), "validation_sha256": validation_digest(token),
            "bundle": bundle, "freeze_sha256": None, "gate": None,
            "publication": None, "completion": None, "signature": None}


def freeze(view: dict, bundle: dict, *, operation_id: str, producer: str,
           key_id: str, key: bytes) -> dict:
    packet = sign(_base_packet(view, bundle, event="freeze", operation_id=operation_id,
                               producer=producer, key_id=key_id), key=key)
    _checked_packet(packet, event="freeze", operation_id=operation_id,
                    revision=packet["expected_revision"], producer=producer,
                    key_id=key_id, key=key, token=Store.workflow_token(view["task"]))
    return packet


def accept(view: dict, bundle: dict, frozen: dict, *, checkout: Path, prepared: Path,
           attestation_path: Path, pr: int, trust_config: Path, trust_sha256: str,
           intent_dir: Path, operation_id: str, producer: str, key_id: str,
           key: bytes, source_head: str) -> dict:
    from bundle_publisher import (_candidate_archive_sha256, _read_attestation,
                                  _read_trust_config, reconcile_bundle)
    from trusted_gate_attestation import verify_attestation
    verify_signature(frozen, key=key, key_id=key_id)
    token = Store.workflow_token(view["task"])
    if (token.place != Place.INTEGRATING or frozen["event"] != "freeze"
            or frozen["binding"] != binding(token) or frozen["bundle"] != bundle
            or frozen["validation_sha256"] != validation_digest(token)):
        raise ProducerError("Current Integrating task differs from signed freeze")
    keys, active, publisher_head, runner_pins, _client = _read_trust_config(
        trust_config, trust_sha256, checkout)
    if publisher_head != source_head:
        raise ProducerError("Publisher trust source differs from approved producer source")
    receipt = _read_attestation(attestation_path, checkout)
    expected = {"bundle_id": bundle["bundle_id"], "pr_number": pr,
                "target_ref": bundle["target_ref"], "target_base": bundle["base_commit"],
                "candidate_commit": bundle["candidate_commit"],
                "candidate_tree": bundle["candidate_tree"],
                "candidate_archive_sha256": _candidate_archive_sha256(checkout, bundle["candidate_commit"]),
                "gate_argv": DEFAULT_GATE_COMMAND,
                "gate_command_sha256": DEFAULT_GATE_COMMAND_SHA256,
                "gate_policy_sha256": bundle["policy_sha256"], **runner_pins}
    predicate = verify_attestation(receipt, trusted_keys=keys, expected_predicate=expected,
                                   expected_key_id=active)
    confirmed = reconcile_bundle(checkout=checkout, intent_dir=intent_dir, remote="origin",
                                 target_ref=bundle["target_ref"], pr=pr)
    if (confirmed.get("state") != "confirmed" or confirmed.get("bundle_id") != bundle["bundle_id"]
            or confirmed.get("candidate") != bundle["candidate_commit"]
            or confirmed.get("tree") != bundle["candidate_tree"]
            or confirmed.get("expected_base") != bundle["base_commit"]
            or confirmed.get("pr") != pr):
        raise ProducerError("Publisher CAS is not conclusively confirmed")
    own = next(member for member in bundle["members"] if member["task_id"] == token.task_id)
    intent_sha = digest(confirmed)
    reference = "trusted-integration:" + intent_sha
    packet = _base_packet(view, bundle, event="accept", operation_id=operation_id,
                          producer=producer, key_id=key_id)
    packet["freeze_sha256"] = digest(frozen)
    packet["gate"] = {"attestation_sha256": hashlib.sha256(attestation_path.read_bytes()).hexdigest(),
                      "predicate": predicate}
    packet["publication"] = {name: confirmed[name] for name in
                             ("state", "candidate", "tree", "expected_base", "target_ref",
                              "observed_target", "pr", "bundle_id")} | {"intent_sha256": intent_sha}
    packet["completion"] = {
        "reason": "Signed gate and confirmed CAS publication include the exact reviewed task head",
        "generation": generation(view["task"]), "source_head": token.source_head,
        "author": token.responsible, "policy_ref": token.policy_version,
        "acceptance": [{"criterion": criterion, "evidence_ref": reference}
                       for criterion in view["task"]["acceptance_criteria"]],
        "checks": [{"name": "required-validation", "source_head": token.source_head,
                    "result": "passed", "evidence_ref": "petri-validation:" + packet["validation_sha256"]}],
        "review": {"reviewer": own["reviewer"], "session_ref": "trusted-review:" + own["review_sha256"],
                   "source_head": token.source_head, "result": "passed",
                   "unresolved_blocking_findings": 0,
                   "evidence_ref": "trusted-review:" + own["review_sha256"]},
        "publication": {"source_head": token.source_head,
                        "candidate_commit": bundle["candidate_commit"],
                        "base_commit": bundle["base_commit"],
                        "target_commit": confirmed["candidate"],
                        "target_ref": bundle["target_ref"], "result": "confirmed",
                        "inclusion_evidence_ref": reference}}
    signed = sign(packet, key=key)
    _checked_packet(signed, event="accept", operation_id=operation_id,
                    revision=signed["expected_revision"], producer=producer,
                    key_id=key_id, key=key, token=token)
    return signed


def submit(client: Client, project: str, task_id: str, packet_path: Path,
           intent_path: Path) -> dict:
    """Send once after a private durable intent; existing intent allows GET only."""
    packet = _read(packet_path)
    ownership = {"project_id": project, "task_id": task_id,
                 "packet_sha256": digest(packet), "operation_id": packet["operation_id"],
                 "revision": packet["expected_revision"], "event": packet["event"]}
    if intent_path.exists():
        if _read(intent_path) != ownership:
            raise ProducerError("Retained operation intent belongs to different inputs")
        return reconcile_submission(client, project, task_id, packet)
    _save(intent_path, ownership)
    response = client.trusted_integration(project, task_id, packet["event"], packet,
                                          expected_revision=packet["expected_revision"],
                                          idempotency_key=packet["operation_id"])
    checked = reconcile_submission(client, project, task_id, packet)
    return {"submitted": True, **checked, "response": response}


def reconcile_submission(client: Client, project: str, task_id: str, packet: dict) -> dict:
    """Read the exact journal receipt; an absent receipt never authorizes a resend."""
    revision = packet["expected_revision"] + 1
    history = client.task_history(project, task_id, limit=100, offset=max(0, revision - 100))
    matches = [row for row in history if row.get("revision") == revision]
    expected = {"authority": "trusted_signed_publisher", "sha256": digest(packet),
                "evidence": packet}
    if len(matches) == 1 and (matches[0].get("operation") == "workflow." + packet["event"]
                              and matches[0].get("event_facts", {}).get("integration_receipt") == expected):
        view = client.task_workflow(project, task_id)
        token = Store.workflow_token(view["task"])
        if (view.get("token") != token.to_dict() or token.revision < revision
                or token.place not in ({Place.INTEGRATING, Place.DONE} if packet["event"] == "freeze"
                                       else {Place.DONE})):
            raise ProducerError("Journal receipt exists but current workflow differs")
        return {"confirmed": True, "journal_revision": revision, "current_revision": token.revision}
    if matches:
        raise ProducerError("Retained integration intent conflicts with task journal")
    return {"confirmed": False, "unknown": True,
            "next_action": "Read exact journal again; never resend this operation"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "accept", "submit"))
    for name in ("checkout", "prepared", "token-file", "ca-file", "key-file", "output",
                 "packet", "intent", "attestation", "trust-config", "intent-dir"):
        parser.add_argument("--" + name, type=Path)
    for name in ("url", "project", "task-id", "producer", "key-id", "operation-id",
                 "source-head",
                 "trust-config-sha256"):
        parser.add_argument("--" + name)
    parser.add_argument("--pr", type=int)
    args = parser.parse_args(argv)
    try:
        if args.checkout is None or args.source_head is None:
            raise ProducerError("Exact trusted source checkout and head are required")
        _source(args.checkout, args.source_head)
        required = ("url", "project", "task_id", "token_file", "ca_file")
        if any(getattr(args, name) is None for name in required):
            raise ProducerError("Private API identity inputs are required")
        endpoint = _private_endpoint(args.url, _resolved_addresses)
        digest_ca = ca_file_sha256(args.ca_file)
        with Client(endpoint, _token_from_file(args.token_file), retries=0, timeout=10,
                    trust_env=False, ca_file=args.ca_file, expected_ca_sha256=digest_ca) as client:
            if args.action == "submit":
                if args.packet is None or args.intent is None:
                    raise ProducerError("Submission needs retained packet and new intent path")
                _source(args.checkout, args.source_head)
                result = submit(client, args.project, args.task_id, args.packet, args.intent)
            else:
                if any(getattr(args, name) is None for name in
                       ("checkout", "prepared", "key_file", "output", "producer", "key_id", "operation_id")):
                    raise ProducerError("Preparation needs pinned bundle and signing inputs")
                identity = client.whoami()
                if identity.get("principal_id") != args.producer or identity.get("is_admin") is not False:
                    raise ProducerError("Dedicated integration producer identity differs")
                view = client.task_workflow(args.project, args.task_id)
                bundle = _bundle(args.checkout, args.prepared)
                key = _key(args.key_file)
                if args.action == "freeze":
                    packet = freeze(view, bundle, operation_id=args.operation_id,
                                    producer=args.producer, key_id=args.key_id, key=key)
                else:
                    if any(getattr(args, name) is None for name in
                           ("packet", "attestation", "pr", "trust_config", "trust_config_sha256", "intent_dir")):
                        raise ProducerError("Acceptance needs original freeze, gate and publisher inputs")
                    packet = accept(view, bundle, _read(args.packet), checkout=args.checkout,
                                    prepared=args.prepared, attestation_path=args.attestation,
                                    pr=args.pr, trust_config=args.trust_config,
                                    trust_sha256=args.trust_config_sha256, intent_dir=args.intent_dir,
                                    operation_id=args.operation_id, producer=args.producer,
                                    key_id=args.key_id, key=key, source_head=args.source_head)
                _source(args.checkout, args.source_head)
                _save(args.output, packet)
                result = {"prepared": True, "packet_sha256": digest(packet), "output": str(args.output)}
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("unknown") is not True else 3
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
