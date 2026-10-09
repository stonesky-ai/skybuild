"""Synchronous client with bounded retries and stable mutation identity."""

import time
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from .contracts import valid_identifier


class ClientError(Exception):
    def __init__(self, code: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


class Client:
    def __init__(self, base_url: str, token: str, *, retries: int = 2, timeout: float = 10, transport: httpx.BaseTransport | None = None) -> None:
        if not 0 <= retries <= 5 or not 0 < timeout <= 120:
            raise ValueError("Retries must be 0–5 and timeout must be 0–120 seconds")
        if not token or "\n" in token or "\r" in token:
            raise ValueError("A bearer token is required")
        url = httpx.URL(base_url)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("Use an HTTP service URL without credentials, query, or fragment")
        self.retries = retries
        self.http = httpx.Client(base_url=base_url.rstrip("/") + "/", headers={"Authorization": f"Bearer {token}"}, timeout=timeout, transport=transport, follow_redirects=False)

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

    def request(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None, idempotency_key: str | None = None, revision: int | None = None) -> Any:
        if httpx.URL(path).is_absolute_url or path.startswith("//"):
            raise ValueError("Requests must use relative service paths")
        headers = {}
        if method.upper() in {"POST", "PATCH"}:
            headers["Idempotency-Key"] = idempotency_key or str(uuid4())
        if revision is not None:
            headers["If-Match"] = str(revision)
        for attempt in range(self.retries + 1):
            try:
                response = self.http.request(method, path, json=body, params=params, headers=headers)
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

    def create_task(self, project_id: str, body: dict, *, idempotency_key: str | None = None) -> dict:
        return self.request("POST", self._path(project_id, "tasks"), body=body, idempotency_key=idempotency_key)

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

    def inbox(self, project_id: str, *, limit: int = 100, offset: int = 0) -> list:
        return self.request("GET", self._path(project_id, "cord/inbox"), params={"limit": limit, "offset": offset})

    def message_action(self, project_id: str, message_id: str, action: str, body: dict | None = None, *, idempotency_key: str | None = None) -> dict:
        if action not in {"receipt", "handle", "reply"}:
            raise ValueError("Unknown message action")
        return self.request("POST", self._path(project_id, f"cord/messages/{self._segment(message_id)}/{action}"), body=body or {}, idempotency_key=idempotency_key)
