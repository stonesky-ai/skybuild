#!/usr/bin/env python3
"""Trusted connectivity probes executed in the candidate network namespace."""
from __future__ import annotations

import errno
import json
import select
import socket
import sys
import time


BLOCKED_ERRORS = {errno.ETIMEDOUT, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EHOSTDOWN}


def _connect(host: str, port: int, family: int = socket.AF_INET) -> tuple[bool | None, str]:
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    deadline = time.monotonic() + 1.0
    try:
        if family == socket.AF_INET6:
            result = sock.connect_ex((host, port, 0, 0))
        else:
            result = sock.connect_ex((host, port))
        if result == 0:
            return True, "connected"
        if result in BLOCKED_ERRORS:
            return False, "blocked"
        if result in {errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK, errno.EAGAIN}:
            remaining = max(0.0, deadline - time.monotonic())
            _readable, writable, exceptional = select.select([], [sock], [sock], remaining)
            if not writable and not exceptional:
                # A pending connect that did not finish inside the bounded
                # probe interval did not establish external connectivity.
                return False, "timeout"
            completion_error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if writable and completion_error == 0:
                return True, "connected"
            if completion_error in BLOCKED_ERRORS:
                return False, "blocked"
            # Unknown completion errors stay indeterminate. The caller treats
            # this as a failed check, not as proof of blocking or connectivity.
            status = errno.errorcode.get(completion_error, "unknown")
            if not writable and completion_error == 0:
                status = "exceptional"
            return None, status
        # Unknown connect_ex results are indeterminate. The caller requires an
        # explicit boolean result for both allowed and blocked connections.
        return None, errno.errorcode.get(result, "unknown")
    finally:
        sock.close()


def _dns_blocked() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    try:
        # Minimal DNS question for an external-only name; a response would mean
        # the candidate namespace can reach Docker's embedded resolver.
        question = (b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                    b"\x07example\x03com\x00\x00\x01\x00\x01")
        sock.sendto(question, ("127.0.0.11", 53))
        try:
            sock.recvfrom(512)
        except TimeoutError:
            return True
        return False
    except OSError as error:
        # Linux reports an iptables OUTPUT DROP to a local UDP destination as
        # EPERM on sendto. Keep this exception local to the DNS send check;
        # other unclassified errors must fail the isolation check.
        return (error.errno in BLOCKED_ERRORS or error.errno == errno.EPERM
                or isinstance(error, TimeoutError))
    finally:
        sock.close()


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        return 2
    mode, postgres_ip, gateway_ip, listener_port_text = argv[1:]
    if mode not in {"candidate", "postgres"}:
        return 2
    listener_port = int(listener_port_text)
    if not (1 <= listener_port <= 65535):
        return 2
    pg_ok, pg_status = (_connect(postgres_ip, 5432) if mode == "candidate" else (True, "not_applicable"))
    host_ok, host_status = _connect(gateway_ip, listener_port)
    external4_ok, external4_status = _connect("1.1.1.1", 443)
    external6_ok, external6_status = _connect("2001:4860:4860::8888", 443, socket.AF_INET6)
    dns_ok = _dns_blocked()
    result = {
        "postgres_tcp_allowed": pg_ok is True,
        "postgres_status": pg_status,
        "dns_blocked": dns_ok,
        "external_ipv4_blocked": external4_ok is False,
        "external_ipv4_status": external4_status,
        "external_ipv6_blocked": external6_ok is False,
        "external_ipv6_status": external6_status,
        "host_gateway_listener_blocked": host_ok is False,
        "host_listener_status": host_status,
        "host_listener_port": listener_port,
        "namespace": mode,
    }
    print("GATE_NETWORK_PROBE=" + json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if all(result[key] is True for key in (
        "postgres_tcp_allowed", "dns_blocked", "external_ipv4_blocked",
        "external_ipv6_blocked", "host_gateway_listener_blocked")) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
