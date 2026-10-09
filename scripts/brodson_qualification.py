"""Explicitly authorized Brodson metadata/counting spike; never a worker adapter."""

import argparse
import asyncio
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

import httpx

from scripts import brodson_spike as spike
from skybuild.manual_assignment import _git, verify_assignment
from skybuild.manual_dispatch import _state_directory


ROOT = Path(__file__).resolve().parents[1]
BRIEF = "docs/design/assignments/brodson-qualification-20261009.json"
ASSIGNMENT = "MWP-20261009-BRODSON-QUALIFICATION-011"
MODEL = "qwen3.5-think"
PROFILE = "owner-confirmed-zero-charge-brodson"
ORIGIN = "https://llm.brodson.net"
LIMITS = {"http_attempts": 14, "metadata_gets": 2, "count_posts": 6, "generation_posts": 6,
          "input_tokens": 2048, "output_tokens": 512, "reserved_tokens": 15360,
          "request_bytes": 8192, "metadata_bytes": 65536, "count_bytes": 4096,
          "generation_bytes": 16384, "metadata_seconds": 10, "count_seconds": 10,
          "generation_seconds": 60, "metadata_fresh_seconds": 1800, "concurrency": 1}
REVIEWED_BUILDS = {"b11371-99b95488c"}
ROUTES = {"/models": "GET", "/props?model=" + MODEL + "&autoload=false": "GET",
          "/v1/chat/completions/input_tokens?autoload=false": "POST",
          "/v1/chat/completions?autoload=false": "POST"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


class QualificationError(ValueError):
    """Only fixed reason codes reach artifacts; remote exception text is private."""


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def private_read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or not 0 < info.st_size <= limit):
            raise QualificationError("unsafe_private_file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise QualificationError("private_file_too_large")
    return data


def persist(path, value):
    payload = encode(value)
    if len(payload) > 1_048_576 or path.is_symlink():
        raise QualificationError("unsafe_state")
    fd, temporary = tempfile.mkstemp(prefix=".qualification-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_authorization(auth, assignment, checkout, *, now):
    fields = {"schema", "run_id", "phase", "reviewed_head", "review_artifact_sha256",
              "rest_assignment_sha256", "rest_receipt_id", "operator_approval_id", "approved_until",
              "zero_charge_profile", "state_dir", "env_file", "limits", "metadata_sha256",
              "template_sha256", "deterministic_template_reviewed", "profile_unchanged_since_metadata",
              "approved_build_info"}
    if not isinstance(auth, dict) or set(auth) != fields or auth["schema"] != "brodson-operator-authorization-v1":
        raise QualificationError("explicit_operator_authorization_required")
    if (auth["phase"] not in {"metadata", "spike"} or auth["limits"] != LIMITS
            or auth["zero_charge_profile"] != PROFILE
            or any(not isinstance(auth[key], str) or not ID.fullmatch(auth[key])
                   for key in ("run_id", "operator_approval_id", "rest_receipt_id"))
            or not isinstance(auth["review_artifact_sha256"], str)
            or not SHA.fullmatch(auth["review_artifact_sha256"])):
        raise QualificationError("invalid_operator_bounds")
    deadline = datetime.fromisoformat(auth["approved_until"].replace("Z", "+00:00"))
    if deadline.tzinfo is None or deadline.utcoffset().total_seconds() != 0 or deadline.timestamp() <= now:
        raise QualificationError("approval_expired_or_not_utc")
    if (auth["rest_assignment_sha256"] != digest(encode(assignment))
            or assignment.get("assignment_id") != ASSIGNMENT or assignment.get("brief_path") != BRIEF):
        raise QualificationError("wrong_rest_assignment")
    verify_assignment(assignment, checkout, worker="wonko")
    head = _git(checkout, "rev-parse", "HEAD").decode().strip()
    if (head != auth["reviewed_head"] or _git(checkout, "status", "--porcelain", "--untracked-files=all").strip()
            or _git(checkout, "show", f"HEAD:{BRIEF}") != _git(checkout, "show", f"{assignment['base_sha']}:{BRIEF}")):
        raise QualificationError("clean_reviewed_head_and_unchanged_brief_required")
    if auth["phase"] == "spike" and (any(not isinstance(auth[key], str) or not SHA.fullmatch(auth[key])
                                           for key in ("metadata_sha256", "template_sha256"))
            or auth["deterministic_template_reviewed"] is not True
            or auth["profile_unchanged_since_metadata"] is not True
            or auth["approved_build_info"] not in REVIEWED_BUILDS):
        raise QualificationError("reviewed_metadata_and_deterministic_template_required")
    for key in ("state_dir", "env_file"):
        path = Path(auth[key])
        if not path.is_absolute() or path != path.resolve() or checkout == path or checkout in path.parents:
            raise QualificationError("external_canonical_operator_paths_required")
    return deadline.timestamp()


class Transport:
    """The only secret reader and HTTP implementation; no retries or fallback."""
    def __init__(self, env_file):
        self.env_file = env_file

    async def request(self, method, path, body, *, seconds, byte_limit):
        # The runner has already fsynced the consumed attempt before this method.
        if ROUTES.get(path) != method or (body is None) != (method == "GET"):
            raise QualificationError("unapproved_route")
        entries = {}
        for line in private_read(self.env_file, 65536).decode().splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key in {"SKYBUILD_INFERENCE_BASE_URL", "SKYBUILD_INFERENCE_SECRET_FILE"}:
                if key in entries:
                    raise QualificationError("duplicate_profile_reference")
                entries[key] = value.strip("\"'")
        if entries.get("SKYBUILD_INFERENCE_BASE_URL", "").rstrip("/") != ORIGIN:
            raise QualificationError("profile_origin_mismatch")
        secret_path = Path(entries.get("SKYBUILD_INFERENCE_SECRET_FILE", ""))
        if not secret_path.is_absolute():
            raise QualificationError("absolute_secret_reference_required")
        secret = private_read(secret_path, 4097).removesuffix(b"\n")
        if not 32 <= len(secret) <= 4096 or any(chr(char).isspace() for char in secret):
            raise QualificationError("invalid_secret_file")
        headers = {"Authorization": "Bearer " + secret.decode("ascii"), "Accept-Encoding": "identity"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with asyncio.timeout(seconds):
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                            transport=httpx.AsyncHTTPTransport(retries=0), timeout=seconds) as client:
                    async with client.stream(method, ORIGIN + path, content=body, headers=headers) as response:
                        chunks = bytearray()
                        async for chunk in response.aiter_raw(chunk_size=4096):
                            chunks.extend(chunk)
                            if len(chunks) > byte_limit:
                                raise QualificationError("response_too_large")
                        payload = bytes(chunks)
                        if secret in payload:
                            raise QualificationError("secret_echo_refused")
                        if response.headers.get("content-encoding", "identity") != "identity":
                            raise QualificationError("compressed_response_refused")
                        if response.status_code != 200:
                            raise QualificationError("http_status_" + str(response.status_code))
                        return payload
        except (httpx.HTTPError, TimeoutError):
            raise QualificationError("transport_timeout_or_ambiguous_failure") from None


def metadata_models(value):
    rows = value.get("data") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise QualificationError("invalid_models_metadata")
    matches = [row for row in rows if isinstance(row, dict) and
               (row.get("id") == MODEL or MODEL in (row.get("aliases") if isinstance(row.get("aliases"), list) else []))]
    if len(matches) != 1:
        raise QualificationError("target_model_not_unique")
    row = matches[0]
    status = row.get("status", {})
    if not isinstance(status, dict):
        raise QualificationError("invalid_model_status")
    args = status.get("args", [])
    sleep_values = []
    if isinstance(args, list) and all(isinstance(item, str) for item in args):
        for index, item in enumerate(args):
            if item == "--sleep-idle-seconds":
                sleep_values.append(args[index + 1] if index + 1 < len(args) else None)
            elif item.startswith("--sleep-idle-seconds="):
                sleep_values.append("unsupported_equals_syntax")
    return {"id": row.get("id"), "aliases": row.get("aliases", []), "meta": row.get("meta", {}),
            "status": status.get("value"), "explicit_sleep_disabled": sleep_values == ["-1"]}


def metadata_props(value):
    if not isinstance(value, dict):
        raise QualificationError("invalid_props_metadata")
    keys = ("model_alias", "model_ftype", "total_slots", "chat_template", "chat_template_caps",
            "bos_token", "eos_token", "build_info", "is_sleeping", "default_generation_settings")
    return {key: value.get(key) for key in keys}


def qualify_metadata(metadata, auth):
    models, props = metadata["models"], metadata["props"]
    if models["status"] != "loaded" or not models["explicit_sleep_disabled"] or props["is_sleeping"] is not False:
        raise QualificationError("loaded_target_and_explicit_disabled_sleep_required")
    if props["model_alias"] != MODEL:
        raise QualificationError("props_target_mismatch")
    settings = props["default_generation_settings"]
    if (not isinstance(settings, dict) or not spike.integer(settings.get("n_ctx"), 2560, 1_048_576)
            or not spike.integer(props["total_slots"], 1, 1024)):
        raise QualificationError("reported_context_or_slot_limit_unqualified")
    build = props["build_info"]
    if not isinstance(build, str) or build not in REVIEWED_BUILDS or build != auth["approved_build_info"]:
        raise QualificationError("current_server_build_not_reviewed")
    template = props["chat_template"]
    if (not isinstance(template, str) or not template or digest(template.encode()) != auth["template_sha256"]
            or digest(encode(metadata)) != auth["metadata_sha256"]):
        raise QualificationError("metadata_or_template_review_mismatch")
    return build


class Runner:
    """One locked durable run, metadata first; continuation never renews counters."""
    def __init__(self, path, auth, transport, *, deadline, boot_id, clock=time.time, monotonic=time.monotonic,
                 approval_monotonic_limit=float("inf")):
        self.path, self.auth, self.transport = path, auth, transport
        self.clock, self.monotonic, self.deadline = clock, monotonic, deadline
        self.state = None
        self.boot_id = boot_id
        self.approval_monotonic_limit = approval_monotonic_limit

    def save(self):
        persist(self.path, self.state)

    def remaining(self):
        return min(self.deadline - self.clock(), self.state["approval_monotonic_deadline"] - self.monotonic(),
                   self.approval_monotonic_limit - self.monotonic())

    def begin(self):
        binding = {key: self.auth[key] for key in ("run_id", "reviewed_head", "rest_assignment_sha256",
                                                   "state_dir", "env_file", "zero_charge_profile", "limits")}
        if self.path.exists() or self.path.is_symlink():
            self.state = json.loads(private_read(self.path, 1_048_576))
            if self.state.get("binding") != binding or self.state.get("boot_id") != self.boot_id:
                raise QualificationError("run_binding_or_boot_changed")
            if self.deadline > self.state["original_approval_deadline"]:
                raise QualificationError("approval_cannot_be_extended")
            if any(row["status"] == "pending" for row in self.state["attempts"]):
                raise QualificationError("uncertain_attempt_no_restart")
            if self.state["status"] not in {"metadata_ready", "complete", "stopped"}:
                raise QualificationError("interrupted_phase_no_restart")
        else:
            if self.auth["phase"] != "metadata":
                raise QualificationError("metadata_phase_required_first")
            mono, wall = self.monotonic(), self.clock()
            self.state = {"schema": "brodson-qualification-run-v1", "binding": binding, "boot_id": self.boot_id,
                          "original_approval_deadline": self.deadline,
                          "approval_monotonic_deadline": min(self.approval_monotonic_limit,
                                                             mono + max(0, self.deadline - wall)),
                          "status": "new", "attempts": [], "reserved_tokens": 0, "returned_tokens": 0,
                          "authorizations": [], "cases": []}
            self.save()
        if self.remaining() <= 0:
            raise QualificationError("approval_expired")

    async def request(self, kind, path, body=None, reservation=0, normalize=None):
        if self.remaining() <= 0 or len(self.state["attempts"]) >= LIMITS["http_attempts"]:
            raise QualificationError("approval_or_http_budget_exhausted")
        if kind != "metadata" and self.monotonic() - self.state["metadata_monotonic"] > LIMITS["metadata_fresh_seconds"]:
            raise QualificationError("metadata_stale_new_authorization_required")
        cap = {"metadata": 2, "count": 6, "generation": 6}[kind]
        if sum(row["kind"] == kind for row in self.state["attempts"]) >= cap:
            raise QualificationError("attempt_kind_budget_exhausted")
        if self.state["reserved_tokens"] + reservation > LIMITS["reserved_tokens"]:
            raise QualificationError("aggregate_reservation_exhausted")
        data = None if body is None else encode(body)
        if data is not None and len(data) > LIMITS["request_bytes"]:
            raise QualificationError("request_bytes_exceeded")
        row = {"number": len(self.state["attempts"]) + 1, "kind": kind, "path": path,
               "method": "GET" if data is None else "POST", "body": body,
               "request_sha256": digest(data or b""), "reserved_tokens": reservation, "status": "pending",
               "wall_limit_seconds": LIMITS[kind + "_seconds"], "response_limit_bytes": LIMITS[kind + "_bytes"]}
        self.state["attempts"].append(row)
        self.state["reserved_tokens"] += reservation
        self.save()  # A crash after this point cannot refund or redispatch this attempt.
        started = self.monotonic()
        try:
            if self.remaining() <= 0:
                raise QualificationError("approval_expired_before_send")
            seconds = min(LIMITS[kind + "_seconds"], self.remaining())
            async with asyncio.timeout(seconds):
                payload = await self.transport.request(row["method"], path, data, seconds=seconds,
                                                       byte_limit=LIMITS[kind + "_bytes"])
            row["wall_latency_ms"] = round((self.monotonic() - started) * 1000, 3)
            if not isinstance(payload, bytes) or len(payload) > LIMITS[kind + "_bytes"]:
                raise QualificationError("response_too_large")
            row["response_sha256"] = digest(payload)
            value = json.loads(payload)
            row["response"] = normalize(value) if normalize else value
            row["status"] = "complete"
            self.save()
            return row["response"]
        except (Exception, asyncio.CancelledError) as error:
            row["wall_latency_ms"] = round((self.monotonic() - started) * 1000, 3)
            row["error"] = str(error) if isinstance(error, QualificationError) else "request_failed_or_invalid_response"
            # Keep pending even for known errors: this run stops without retry.
            self.state["status"] = "stopped"
            self.save()
            raise QualificationError(row["error"]) from None

    async def run(self, manifest):
        self.begin()
        if self.state["status"] in {"complete", "stopped"}:
            return self.state
        phase = self.auth["phase"]
        if phase == "metadata" and self.state["status"] == "metadata_ready":
            return self.state
        self.state["authorizations"].append({"phase": phase, "sha256": digest(encode(self.auth)),
                                              "operator_approval_id": self.auth["operator_approval_id"]})
        self.save()
        try:
            if phase == "metadata":
                models = await self.request("metadata", "/models", normalize=metadata_models)
                if models["status"] != "loaded":
                    raise QualificationError("target_is_not_loaded")
                props = await self.request("metadata", "/props?model=" + MODEL + "&autoload=false", normalize=metadata_props)
                self.state.update(metadata={"models": models, "props": props},
                                  metadata_monotonic=self.monotonic(), status="metadata_ready")
                self.state["metadata_sha256"] = digest(encode(self.state["metadata"]))
                self.save()
                return self.state  # Always stop before count/generation for separate operator review.
            if self.state["status"] != "metadata_ready" or len(self.state["attempts"]) != 2:
                raise QualificationError("fresh_metadata_phase_required")
            if self.monotonic() - self.state["metadata_monotonic"] > LIMITS["metadata_fresh_seconds"]:
                raise QualificationError("metadata_stale_new_authorization_required")
            fingerprint = qualify_metadata(self.state["metadata"], self.auth)
            self.state["status"] = "spike_running"
            self.save()
            for case in manifest["cases"]:
                messages = [{"role": "user", "content": case["prompt"]}]
                case_result = {"id": case["id"], "attempts": []}
                self.state["cases"].append(case_result)
                for number in range(2):
                    body = {"model": MODEL, "messages": messages, "temperature": 0, "max_tokens": 512,
                            "stream": False, "n": 1, "chat_template_kwargs": {"enable_thinking": False}}
                    count = await self.request("count", "/v1/chat/completions/input_tokens?autoload=false", body)
                    if (not isinstance(count, dict) or count.get("object") != "response.input_tokens"
                            or not spike.integer(count.get("input_tokens"), 1, 2048)):
                        raise QualificationError("input_count_unknown_or_exceeded")
                    response = await self.request("generation", "/v1/chat/completions?autoload=false", body,
                                                  reservation=count["input_tokens"] + 512)
                    candidate, content, usage = generation(response, count["input_tokens"], fingerprint)
                    self.state["returned_tokens"] += usage["total_tokens"]
                    if self.state["returned_tokens"] > 15360:
                        raise QualificationError("returned_token_budget_exceeded")
                    started = self.monotonic()
                    verdict = spike.evaluate(case, candidate)
                    case_result["attempts"].append({"number": number + 1, **verdict, "usage": usage,
                                                   "validation_wall_ms": round((self.monotonic() - started) * 1000, 3)})
                    self.save()
                    if verdict["accepted"]:
                        break
                    if number == 1:
                        raise QualificationError("second_defective_proposal")
                    messages = messages + [{"role": "assistant", "content": content},
                                           {"role": "user", "content": "Independent validation failed: " + verdict["reason"] +
                                            ". Correct your JSON response under the original constraints. Return JSON only."}]
            self.state["status"] = "complete"
            self.save()
        except QualificationError as error:
            self.state.update(status="stopped", stop_reason=str(error))
            self.save()
        return self.state


def generation(response, counted, fingerprint):
    if (not isinstance(response, dict) or response.get("model") != MODEL
            or response.get("system_fingerprint") != fingerprint or response.get("error")):
        raise QualificationError("generation_identity_or_error")
    choices, usage = response.get("choices"), response.get("usage")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise QualificationError("invalid_generation_choices")
    choice = choices[0]
    message = choice.get("message")
    if choice.get("finish_reason") != "stop" or not isinstance(message, dict) or message.get("tool_calls"):
        raise QualificationError("incomplete_or_tool_response")
    if (not isinstance(usage, dict) or usage.get("prompt_tokens") != counted
            or type(usage.get("prompt_tokens")) is not int
            or not spike.integer(usage.get("completion_tokens"), 1, 512)
            or type(usage.get("total_tokens")) is not int
            or usage["total_tokens"] != counted + usage["completion_tokens"]):
        raise QualificationError("unknown_or_mismatched_usage")
    content = message.get("content")
    if not isinstance(content, str):
        raise QualificationError("missing_candidate")
    try:
        candidate = json.loads(content)
    except (ValueError, RecursionError):
        raise QualificationError("malformed_candidate") from None
    if not isinstance(candidate, dict):
        raise QualificationError("candidate_object_required")
    return candidate, content, usage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--assignment", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        armed_at, wall_at = time.monotonic(), time.time()
        auth = json.loads(private_read(args.authorization, 32768))
        assignment = json.loads(private_read(args.assignment, 32768))
        deadline = validate_authorization(auth, assignment, ROOT, now=wall_at)
        manifest = spike.read_json(spike.FIXTURES / "cases.json")
        spike.validate_manifest(manifest, spike.read_json(spike.FIXTURES / "recorded-responses.json"))
        directory = _state_directory(Path(auth["state_dir"]), ROOT)
        descriptor = os.open(directory / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runner = Runner(directory / "run.json", auth, Transport(Path(auth["env_file"])), deadline=deadline,
                            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                            clock=time.time, monotonic=time.monotonic,
                            approval_monotonic_limit=armed_at + max(0, deadline - wall_at))
            state = asyncio.run(runner.run(manifest))
        finally:
            os.close(descriptor)
        print(json.dumps({"status": state["status"], "http_attempts": len(state["attempts"]),
                          "reserved_tokens": state["reserved_tokens"], "metadata_sha256": state.get("metadata_sha256")}))
        return 0 if state["status"] in {"metadata_ready", "complete"} else 2
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print(json.dumps({"status": "blocked", "reason": "Qualification refused; preserve private evidence"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
