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
from .contracts import valid_identifier


class PreflightError(ValueError):
    pass


_WORKER_SCOPES = {"tasks:read", "cord:read", "cord:send", "cord:handle"}
_REQUIRED_SCOPES = {"cord:read", "cord:send", "cord:handle"}
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


def probe_private_api(url: str, project_id: str, token_file: Path, expected_principal: str,
                      *, transport: httpx.BaseTransport | None = None,
                      ca_file: Path | None = None,
                      resolve: Callable[[str], Iterable[str]] = _resolved_addresses) -> dict:
    """Require trusted HTTPS, ready service, exact worker identity and narrow grants."""
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
    if not valid_identifier(project_id) or not valid_identifier(expected_principal):
        raise PreflightError("Project or principal identifier is invalid")
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
            if (not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes)
                    or len(scopes) != len(set(scopes))
                    or not _REQUIRED_SCOPES <= set(scopes) <= _WORKER_SCOPES):
                raise PreflightError("Token grants are missing or broader than the pilot contract")
            if not isinstance(client.inbox(project_id, limit=1), list):
                raise PreflightError("Cord inbox response is invalid")
    except ClientError as error:
        raise PreflightError(f"SkyBuild API check failed ({error.code})") from None
    return {"ready": True, "project_id": project_id, "principal_id": expected_principal,
            "scopes": sorted(scopes), "inbox_access": True, "host": endpoint.host}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--principal", required=True)
    parser.add_argument("--ca-file", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(probe_private_api(args.url, args.project, args.token_file, args.principal,
                                           ca_file=args.ca_file), sort_keys=True))
        return 0
    except (PreflightError, ValueError, httpx.HTTPError):
        print(json.dumps({"ready": False, "reason": "Private API or worker scope check failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
