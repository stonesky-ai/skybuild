"""Synchronous client with bounded retries and stable mutation identity."""

import hashlib
import os
import ssl
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from .contracts import valid_identifier


def _ca_material(ca_file: Path | str) -> bytes:
    """Read one bounded regular CA file without following a replacement symlink."""
    try:
        descriptor = os.open(ca_file, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 1_048_576:
                raise ValueError("CA file must be a bounded regular PEM file")
            material = os.read(descriptor, 1_048_577)
        finally:
            os.close(descriptor)
        if not material or len(material) > 1_048_576:
            raise ValueError("CA file must be a bounded regular PEM file")
        return material
    except OSError:
        raise ValueError("CA file cannot be read safely") from None


def ca_file_sha256(ca_file: Path | str) -> str:
    """Bind a manual dispatch to the exact installation trust material."""
    return hashlib.sha256(_ca_material(ca_file)).hexdigest()


class ClientError(Exception):
    def __init__(self, code: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


class Client:
    def __init__(self, base_url: str, token: str, *, retries: int = 2, timeout: float = 10,
                 transport: httpx.BaseTransport | None = None, trust_env: bool = True,
                 ca_file: Path | str | None = None, expected_ca_sha256: str | None = None) -> None:
        if not isinstance(retries, int) or isinstance(retries, bool) or not 0 <= retries <= 5 or not 0 < timeout <= 120:
            raise ValueError("Retries must be 0–5 and timeout must be 0–120 seconds")
        if not token or "\n" in token or "\r" in token:
            raise ValueError("A bearer token is required")
        url = httpx.URL(base_url)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("Use an HTTP service URL without credentials, query, or fragment")
        verify: bool | ssl.SSLContext = True
        if ca_file is not None:
            if url.scheme != "https":
                raise ValueError("An installation CA requires HTTPS")
            material = _ca_material(ca_file)
            if expected_ca_sha256 is not None and hashlib.sha256(material).hexdigest() != expected_ca_sha256:
                raise ValueError("Installation CA differs from durable dispatch intent")
            verify = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            verify.check_hostname = True
            verify.verify_mode = ssl.CERT_REQUIRED
            try:
                verify.load_verify_locations(cadata=material.decode("ascii"))
            except (UnicodeError, ssl.SSLError):
                raise ValueError("CA file must contain valid PEM certificates") from None
            # Installation trust cannot be redirected by ambient proxy or CA settings.
            trust_env = False
        elif expected_ca_sha256 is not None:
            raise ValueError("A pinned installation CA file is required")
        self.retries = retries
        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/", headers={"Authorization": f"Bearer {token}"}, timeout=timeout,
                                 transport=transport, follow_redirects=False, trust_env=trust_env, verify=verify)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @staticmethod
    def _segment(identifier: str) -> str:
        if not valid_identifier(identifier):
            raise ValueError("Identifier is not addressable")
        return quote(identifier, safe="")

    @staticmethod
    def _path(project_id: str, suffix: str) -> str:
        return f"api/v1/projects/{Client._segment(project_id)}/{suffix}"

    def request(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None, idempotency_key: str | None = None, revision: int | None = None, timeout: float | httpx.Timeout | None = None) -> Any:
        if httpx.URL(path).is_absolute_url or path.startswith("//"):
            raise ValueError("Requests must use relative service paths")
        headers = {}
        if method.upper() in {"POST", "PATCH"}:
            headers["Idempotency-Key"] = idempotency_key or str(uuid4())
        if revision is not None:
            headers["If-Match"] = str(revision)
        for attempt in range(self.retries + 1):
            try:
                response = self.http.request(method, path, json=body, params=params, headers=headers,
                                             **({"timeout": timeout} if timeout is not None else {}))
            except httpx.TransportError:
                if attempt == self.retries:
                    raise ClientError("unavailable", "SkyBuild service unavailable") from None
            else:
                if response.status_code not in {429, 502, 503, 504} or attempt == self.retries:
                    try:
                        result = response.json()
                    except ValueError:
                        raise ClientError("invalid_response", "Invalid service response", response.status_code) from None
                    if response.is_error:
                        error = result.get("error", {}) if isinstance(result, dict) else {}
                        if not isinstance(error, dict):
                            error = {}
                        raise ClientError(error.get("code", "http_error"), error.get("message", "Request failed"), response.status_code)
                    if not response.is_success:
                        raise ClientError("http_error", "Unexpected service response", response.status_code)
                    return result
            time.sleep(min(0.1 * (2 ** attempt), 1.0))

    def cpu_control_status(self, project_id: str) -> dict:
        return self.request("GET", self._path(project_id, "cpu-controls"))

    def configure_cpu_pool(self, project_id: str, capacity: int, enabled: bool, expected_generation: int,
                           *, reason: str, idempotency_key: str | None = None) -> dict:
        return self.request("POST", self._path(project_id, "cpu-controls/central"),
                            body={"capacity": capacity, "enabled": enabled,
                                  "expected_generation": expected_generation, "reason": reason},
                            idempotency_key=idempotency_key)

    def set_cpu_local_control(self, project_id: str, enabled: bool, expected_generation: int,
                              *, reason: str, idempotency_key: str | None = None) -> dict:
        return self.request("POST", self._path(project_id, "cpu-controls/local"),
                            body={"enabled": enabled, "expected_generation": expected_generation, "reason": reason},
                            idempotency_key=idempotency_key)

    def explain_cpu(self, project_id: str, request: dict) -> dict:
        return self.request("POST", self._path(project_id, "cpu-reservations/explain"), body=request)

    def reserve_cpu(self, project_id: str, request: dict) -> dict:
        return self.request("POST", self._path(project_id, "cpu-reservations"), body=request)

    def cancel_cpu_reservation(self, project_id: str, action_id: str, *, reason: str) -> dict:
        return self.request("POST", self._path(project_id, f"cpu-reservations/{action_id}/cancel"),
                            body={"reason": reason})

    def prepare_cpu_worker_dispatch(self, project_id: str, request: dict) -> dict:
        return self.request("POST", self._path(project_id, "cpu-worker-dispatches/prepare"), body=request)

    def begin_cpu_worker_dispatch(self, project_id: str, operation_id: str) -> dict:
        return self.request("POST", self._path(project_id, f"cpu-worker-dispatches/{operation_id}/begin"), body={})

    def get_cpu_worker_dispatch(self, project_id: str, operation_id: str) -> dict:
        return self.request("GET", self._path(project_id, f"cpu-worker-dispatches/{operation_id}"))

    def record_cpu_worker_invocation(self, project_id: str, operation_id: str, request: dict) -> dict:
        return self.request("POST", self._path(project_id, f"cpu-worker-dispatches/{operation_id}/invocation"), body=request)

    def observe_cpu_worker_dispatch(self, project_id: str, operation_id: str, request: dict) -> dict:
        return self.request("POST", self._path(project_id, f"cpu-worker-dispatches/{operation_id}/observations"), body=request)

    def settle_cpu_worker_dispatch(self, project_id: str, operation_id: str, *, observation_id: str) -> dict:
        return self.request("POST", self._path(project_id, f"cpu-worker-dispatches/{operation_id}/settle"),
                            body={"observation_id": observation_id})

    def create_task(self, project_id: str, body: dict, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", self._path(project_id, "tasks"), body=body, idempotency_key=idempotency_key)

    def whoami(self) -> dict:
        return self.request("GET", "api/v1/me")

    def list_tasks(self, project_id: str, *, limit: int = 100, offset: int = 0,
                   after_task_id: str | None = None, by_id: bool = False) -> list:
        if after_task_id is not None and not valid_identifier(after_task_id):
            raise ValueError("Cursor task ID is not addressable")
        params = {"limit": limit, "offset": offset}
        if after_task_id is not None:
            params["after_task_id"] = after_task_id
        if by_id:
            params["by_id"] = "true"
        return self.request("GET", self._path(project_id, "tasks"), params=params)

    def get_task(self, project_id: str, task_id: str) -> dict:
        return self.request("GET", self._path(project_id, f"tasks/{self._segment(task_id)}"))

    def claim_task(self, project_id: str, task_id: str, *, expected_revision: int,
                   idempotency_key: str | None = None, lease_seconds: int = 60) -> dict:
        """Claim work through the existing atomic claim endpoint."""
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise ValueError("Claim lease must be 1–300 whole seconds")
        return self.request("POST", self._path(project_id, f"tasks/{self._segment(task_id)}/claim"),
                            body={"lease_seconds": lease_seconds}, revision=expected_revision,
                            idempotency_key=idempotency_key)

    def task_workflow(self, project_id: str, task_id: str) -> dict:
        """Read task state and permitted actions without external operations."""
        return self.request("GET", self._path(project_id, f"tasks/{self._segment(task_id)}/workflow"))

    def workflow_transition(self, project_id: str, task_id: str, event: str, body: dict | None = None, *,
                            expected_revision: int, idempotency_key: str | None = None) -> dict:
        """Submit caller details. Store computes all guards in one transaction."""
        from .workflow import TRANSITIONS

        if not isinstance(event, str) or event not in {spec.event for spec in TRANSITIONS} | {"initialize"}:
            raise ValueError("Unknown workflow event")
        if body is not None and (not isinstance(body, dict) or "event" in body):
            raise ValueError("Workflow details cannot replace the event")
        return self.request("POST", self._path(project_id, f"tasks/{self._segment(task_id)}/workflow"),
                            body={"event": event, **(body or {})}, revision=expected_revision,
                            idempotency_key=idempotency_key)

    def manual_integration(self, project_id: str, task_id: str, event: str, evidence: dict, *,
                           expected_revision: int, idempotency_key: str) -> dict:
        """Submit an owner's manual attestation; never grant publisher authority."""
        if event not in {"freeze", "accept", "integration_progress"}:
            raise ValueError("Unsupported manual integration event")
        return self.request("POST", self._path(project_id, f"tasks/{self._segment(task_id)}/manual-integration"),
                            body={"event": event, "evidence": evidence}, revision=expected_revision,
                            idempotency_key=idempotency_key)

    def trusted_integration(self, project_id: str, task_id: str, event: str, evidence: dict, *,
                            expected_revision: int, idempotency_key: str) -> dict:
        """Submit one signed trusted-host statement to the configured verifier."""
        if event not in {"freeze", "accept"}:
            raise ValueError("Unsupported trusted integration event")
        return self.request("POST", self._path(project_id, f"tasks/{self._segment(task_id)}/trusted-integration"),
                            body={"event": event, "evidence": evidence}, revision=expected_revision,
                            idempotency_key=idempotency_key)

    def execution_status(self, project_id: str, task_id: str, *, limit: int = 20) -> dict:
        """Read one task's bounded cached evidence; never probe or reconcile."""
        return self.request("GET", self._path(project_id, f"tasks/{self._segment(task_id)}/execution-status"),
                            params={"limit": limit})

    def update_task(self, project_id: str, task_id: str, body: dict, *, expected_revision: int, idempotency_key: str | None = None) -> dict:
        return self.request("PATCH", self._path(project_id, f"tasks/{self._segment(task_id)}"), body=body, revision=expected_revision, idempotency_key=idempotency_key)

    def task_history(self, project_id: str, task_id: str, *, limit: int = 100, offset: int = 0) -> list:
        return self.request("GET", self._path(project_id, f"tasks/{self._segment(task_id)}/history"), params={"limit": limit, "offset": offset})

    def task_action(self, project_id: str, task_id: str, action: str, body: dict, *, expected_revision: int,
                    idempotency_key: str | None = None) -> dict:
        if action not in {"rework", "reassess", "defer", "resume", "ready"}:
            raise ValueError("Unknown task action")
        return self.request("POST", self._path(project_id, f"tasks/{self._segment(task_id)}/actions/{action}"),
                            body=body, revision=expected_revision, idempotency_key=idempotency_key)

    def split_task(self, project_id: str, task_id: str, body: dict, *, expected_revision: int,
                   idempotency_key: str | None = None) -> dict:
        return self.request("POST", self._path(project_id, f"tasks/{self._segment(task_id)}/split"),
                            body=body, revision=expected_revision, idempotency_key=idempotency_key)

    def merge_tasks(self, project_id: str, body: dict, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", self._path(project_id, "tasks/merge"), body=body,
                            idempotency_key=idempotency_key)

    def task_lineage(self, project_id: str, task_id: str) -> list[dict]:
        return self.request("GET", self._path(project_id, f"tasks/{self._segment(task_id)}/lineage"))

    def reconcile_due_deferrals(self, project_id: str, *, limit: int = 100, after_task_id: str | None = None,
                                idempotency_key: str | None = None) -> dict:
        if after_task_id is not None and not valid_identifier(after_task_id):
            raise ValueError("Cursor task ID is not addressable")
        return self.request("POST", self._path(project_id, "tasks/reconcile-due"), body={},
                            params={"limit": limit, **({"after_task_id": after_task_id} if after_task_id is not None else {})},
                            idempotency_key=idempotency_key)

    def send_message(self, project_id: str, body: dict, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", self._path(project_id, "cord/messages"), body=body, idempotency_key=idempotency_key)

    def inbox(self, project_id: str, *, limit: int = 100, offset: int = 0, wait_seconds: int = 0) -> list:
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 25:
            raise ValueError("Inbox wait must be 0–25 whole seconds")
        params = {"limit": limit, "offset": offset}
        if wait_seconds:
            params["wait_seconds"] = wait_seconds
            timeout = httpx.Timeout(self.http.timeout)
            timeout.read = max(timeout.read or 0, wait_seconds + 5)
            return self.request("GET", self._path(project_id, "cord/inbox"), params=params,
                                timeout=timeout)
        return self.request("GET", self._path(project_id, "cord/inbox"), params=params)

    def message_action(self, project_id: str, message_id: str, action: str, body: dict | None = None, *, idempotency_key: str | None = None) -> dict:
        if action not in {"receipt", "handle", "reply"}:
            raise ValueError("Unknown message action")
        return self.request("POST", self._path(project_id, f"cord/messages/{self._segment(message_id)}/{action}"), body=body or {}, idempotency_key=idempotency_key)
