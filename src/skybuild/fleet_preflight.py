"""Read-only worker-side check for a private SkyBuild API and scoped Cord token."""

import json
import os
import ipaddress
import socket
import stat
from pathlib import Path
from collections.abc import Callable, Iterable

import httpx

from .client import Client, ClientError
from .contracts import DomainError, valid_identifier
from .workflow import TaskToken


class PreflightError(ValueError):
    pass


_WORKER_SCOPES = {"tasks:read", "cord:read", "cord:send", "cord:handle"}
_REQUIRED_SCOPES = {"tasks:read", "cord:read", "cord:send", "cord:handle"}
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _resolved_addresses(host: str) -> set[str]:
    try:
        return {entry[4][0] for entry in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except OSError as error:
        raise PreflightError("Private API hostname cannot be resolved") from error


def _token_from_file(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PreflightError("Token file must be a private regular file owned by this user")
            if info.st_size < 32 or info.st_size > 4097:
                raise PreflightError("Token file size is invalid")
            token = os.read(descriptor, 4098).decode("utf-8").removesuffix("\n")
        finally:
            os.close(descriptor)
    except (OSError, UnicodeError) as error:
        raise PreflightError("Token file cannot be read safely") from error
    if not 32 <= len(token) <= 4096 or any(char.isspace() for char in token):
        raise PreflightError("Token file content is invalid")
    return token


def _validate_workflow_pins(task_id: str | None, *,
                            expected_task_revision: int | None,
                            expected_input_generation: int | None,
                            expected_definition_revision: int | None,
                            expected_policy_version: str | None) -> dict:
    expectations = {
        "revision": expected_task_revision,
        "input_generation": expected_input_generation,
        "definition_revision": expected_definition_revision,
        "policy_version": expected_policy_version,
    }
    if task_id is None:
        if any(value is not None for value in expectations.values()):
            raise PreflightError("Workflow expectations require a selected task")
        return expectations
    if not valid_identifier(task_id):
        raise PreflightError("Selected task identifier is invalid")
    for name, value in expectations.items():
        if value is None:
            continue
        if name == "policy_version":
            if not isinstance(value, str) or not value or len(value) > 200 or "\x00" in value:
                raise PreflightError("Expected workflow policy is invalid")
        elif type(value) is not int or value < (1 if name == "revision" else 0) or value >= 2**63:
            raise PreflightError("Expected workflow revision is invalid")
    return expectations


def _selected_workflow(client: Client, project_id: str, task_id: str | None,
                       expectations: dict) -> dict | None:
    if task_id is None:
        return None

    view = client.task_workflow(project_id, task_id)
    if not isinstance(view, dict):
        raise PreflightError("Selected task workflow is malformed")
    raw_token = view.get("token")
    task = view.get("task")
    if not isinstance(raw_token, dict) or not isinstance(task, dict):
        raise PreflightError("Selected task workflow is absent or malformed")
    required_token_fields = {"project_id", "task_id", "place", "revision",
                             "input_generation", "definition_revision", "policy_version"}
    if not required_token_fields <= raw_token.keys():
        raise PreflightError("Selected task workflow is unsupported or malformed")
    try:
        token = TaskToken.from_dict(raw_token)
    except (DomainError, TypeError, ValueError, KeyError, RecursionError):
        raise PreflightError("Selected task workflow is unsupported or malformed") from None
    if (token.project_id != project_id or token.task_id != task_id
            or task.get("project_id") != project_id or task.get("task_id") != task_id
            or type(task.get("revision")) is not int or task["revision"] < 1
            or task["revision"] != token.revision or not token.policy_version):
        raise PreflightError("Selected task workflow identity is inconsistent")
    if (not isinstance(view.get("available_actions"), list)
            or not all(isinstance(action, str) for action in view["available_actions"])
            or not isinstance(view.get("transitions"), list)
            or not all(isinstance(transition, dict)
                       and isinstance(transition.get("event"), str)
                       and isinstance(transition.get("sources"), list)
                       and isinstance(transition.get("guard"), str)
                       and (transition.get("destination") is None
                            or isinstance(transition.get("destination"), str))
                       for transition in view["transitions"])
            or not isinstance(view.get("disabled_actions"), dict)
            or not all(isinstance(event, str) and isinstance(reason, str)
                       for event, reason in view["disabled_actions"].items())):
        raise PreflightError("Selected task workflow is unsupported or malformed")
    for name, expected in expectations.items():
        if expected is not None and getattr(token, name) != expected:
            raise PreflightError("Selected task workflow differs from expected inputs")
    return {"task_id": token.task_id, "revision": token.revision,
            "input_generation": token.input_generation,
            "definition_revision": token.definition_revision,
            "policy_version": token.policy_version, "place": token.place.value,
            "compatible": True, "execution_authorized": False}


def probe_private_api(url: str, project_id: str, token_file: Path, expected_principal: str,
                      *, transport: httpx.BaseTransport | None = None,
                      ca_file: Path | None = None,
                      resolve: Callable[[str], Iterable[str]] = _resolved_addresses,
                      workflow: bool = False,
                      task_id: str | None = None,
                      expected_task_revision: int | None = None,
                      expected_input_generation: int | None = None,
                      expected_definition_revision: int | None = None,
                      expected_policy_version: str | None = None) -> dict:
    """Require trusted HTTPS, ready service, exact worker identity and narrow grants."""
    if not valid_identifier(project_id) or not valid_identifier(expected_principal):
        raise PreflightError("Project or principal identifier is invalid")
    expectations = _validate_workflow_pins(
        task_id, expected_task_revision=expected_task_revision,
        expected_input_generation=expected_input_generation,
        expected_definition_revision=expected_definition_revision,
        expected_policy_version=expected_policy_version)
    endpoint = httpx.URL(url)
    if (endpoint.scheme != "https" or not endpoint.host or not endpoint.host.endswith(".ts.net")
            or endpoint.userinfo or endpoint.query or endpoint.fragment or endpoint.path not in {"", "/"}):
        raise PreflightError("Use the private Tailscale HTTPS service root")
    addresses = list(resolve(endpoint.host))
    try:
        parsed = [ipaddress.ip_address(address) for address in addresses]
    except ValueError as error:
        raise PreflightError("API hostname returned an invalid address") from error
    if not parsed or any(address not in (_TAILNET_V4 if address.version == 4 else _TAILNET_V6)
                         for address in parsed):
        raise PreflightError("API hostname must resolve only to Tailscale addresses")
    token = _token_from_file(token_file)
    try:
        with Client(url, token, retries=0, timeout=10, transport=transport, trust_env=False, ca_file=ca_file) as client:
            ready = client.request("GET", "health/ready")
            if ready != {"status": "ready"}:
                raise PreflightError("SkyBuild API is not ready")
            identity = client.whoami()
            if not isinstance(identity, dict) or identity.get("principal_id") != expected_principal:
                raise PreflightError("Token identifies another principal")
            grants = identity.get("grants")
            if identity.get("is_admin") is not False or not isinstance(grants, dict) or set(grants) != {project_id}:
                raise PreflightError("Token is not confined to this worker project")
            scopes = grants[project_id]
            permitted = _WORKER_SCOPES | ({"tasks:claim", "tasks:write"} if workflow else set())
            required = _REQUIRED_SCOPES | ({"tasks:claim", "tasks:write"} if workflow else set())
            if (not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes)
                    or len(scopes) != len(set(scopes))
                    or not required <= set(scopes) <= permitted):
                raise PreflightError("Token grants are missing or broader than the pilot contract")
            if not isinstance(client.inbox(project_id, limit=1), list):
                raise PreflightError("Cord inbox response is invalid")
            tasks = client.list_tasks(project_id, limit=1, by_id=True)
            if not isinstance(tasks, list) or len(tasks) > 1:
                raise PreflightError("Task list response is invalid")
            selected = _selected_workflow(client, project_id, task_id, expectations)
    except ClientError as error:
        raise PreflightError(f"SkyBuild API check failed ({error.code})") from None
    result = {"ready": True, "project_id": project_id, "principal_id": expected_principal,
              "scopes": sorted(scopes), "inbox_access": True, "task_list_access": True,
              "host": endpoint.host}
    if selected is not None:
        result["workflow_probe"] = selected
    return result


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--workflow", action="store_true", help="Require the explicit Petri worker scope profile")
    parser.add_argument("--task-id", help="Read and validate one selected task workflow")
    parser.add_argument("--expected-task-revision", type=int)
    parser.add_argument("--expected-input-generation", type=int)
    parser.add_argument("--expected-definition-revision", type=int)
    parser.add_argument("--expected-policy-version")
    args = parser.parse_args()
    try:
        print(json.dumps(probe_private_api(args.url, args.project, args.token_file, args.principal,
                                           ca_file=args.ca_file, workflow=args.workflow,
                                           task_id=args.task_id,
                                           expected_task_revision=args.expected_task_revision,
                                           expected_input_generation=args.expected_input_generation,
                                           expected_definition_revision=args.expected_definition_revision,
                                           expected_policy_version=args.expected_policy_version), sort_keys=True))
        return 0
    except (PreflightError, ValueError, httpx.HTTPError):
        print(json.dumps({"ready": False, "reason": "Private API or worker scope check failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
