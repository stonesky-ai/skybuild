from __future__ import annotations

import hashlib
import errno
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

import isolated_full_test_gate as gate
import build_isolated_gate_images as image_builder
import gate_network_probe as network_probe


def test_network_connect_probe_requires_completed_connection(monkeypatch):
    class FakeSocket:
        def __init__(self, *_args):
            self.closed = False

        def settimeout(self, _timeout):
            pass

        def connect_ex(self, _address):
            return errno.EINPROGRESS

        def getsockopt(self, _level, _option):
            return 0

        def close(self):
            self.closed = True

    sock = FakeSocket()
    monkeypatch.setattr(network_probe.socket, "socket", lambda *_args: sock)
    monkeypatch.setattr(network_probe.time, "monotonic", iter((10.0, 10.1)).__next__)
    monkeypatch.setattr(network_probe.select, "select", lambda *_args: ([], [], []))

    assert network_probe._connect("192.0.2.10", 5432) == (False, "timeout")
    assert sock.closed


def test_network_connect_probe_accepts_only_completed_success(monkeypatch):
    class FakeSocket:
        def __init__(self, completion_error):
            self.completion_error = completion_error
            self.closed = False

        def settimeout(self, _timeout):
            pass

        def connect_ex(self, _address):
            return errno.EINPROGRESS

        def getsockopt(self, _level, _option):
            return self.completion_error

        def close(self):
            self.closed = True

    sock = FakeSocket(0)
    monkeypatch.setattr(network_probe.socket, "socket", lambda *_args: sock)
    monkeypatch.setattr(network_probe.time, "monotonic", iter((10.0, 10.1)).__next__)
    monkeypatch.setattr(network_probe.select, "select", lambda *_args: ([], [sock], []))

    assert network_probe._connect("192.0.2.10", 5432) == (True, "connected")
    assert sock.closed


def test_network_connect_probe_keeps_unknown_completion_errors_indeterminate(monkeypatch):
    class FakeSocket:
        def settimeout(self, _timeout):
            pass

        def connect_ex(self, _address):
            return errno.EINPROGRESS

        def getsockopt(self, _level, _option):
            return errno.ECONNREFUSED

        def close(self):
            pass

    sock = FakeSocket()
    monkeypatch.setattr(network_probe.socket, "socket", lambda *_args: sock)
    monkeypatch.setattr(network_probe.time, "monotonic", iter((10.0, 10.1)).__next__)
    monkeypatch.setattr(network_probe.select, "select", lambda *_args: ([], [sock], []))

    # An unknown result must fail the blocking requirement. It is not proof
    # that the firewall blocked the connection or that a listener accepted it.
    assert network_probe._connect("192.0.2.10", 5432) == (None, "ECONNREFUSED")


def test_network_connect_probe_requires_writable_completion(monkeypatch):
    class FakeSocket:
        def settimeout(self, _timeout):
            pass

        def connect_ex(self, _address):
            return errno.EINPROGRESS

        def getsockopt(self, _level, _option):
            return 0

        def close(self):
            pass

    sock = FakeSocket()
    monkeypatch.setattr(network_probe.socket, "socket", lambda *_args: sock)
    monkeypatch.setattr(network_probe.time, "monotonic", iter((10.0, 10.1)).__next__)
    monkeypatch.setattr(network_probe.select, "select", lambda *_args: ([], [], [sock]))

    assert network_probe._connect("192.0.2.10", 5432) == (None, "exceptional")


def test_dns_probe_counts_sendto_eperm_as_blocked(monkeypatch):
    class FakeSocket:
        closed = False

        def settimeout(self, _timeout):
            pass

        def sendto(self, _payload, _address):
            raise PermissionError(errno.EPERM, "blocked by firewall")

        def recvfrom(self, _size):
            raise AssertionError("recvfrom must not run when sendto is blocked")

        def close(self):
            self.closed = True

    sock = FakeSocket()
    monkeypatch.setattr(network_probe.socket, "socket", lambda *_args: sock)

    assert network_probe._dns_blocked() is True
    assert sock.closed


def test_dns_probe_rejects_any_received_response(monkeypatch):
    class FakeSocket:
        def settimeout(self, _timeout):
            pass

        def sendto(self, payload, _address):
            return len(payload)

        def recvfrom(self, _size):
            return b"dns response", ("127.0.0.11", 53)

        def close(self):
            pass

    monkeypatch.setattr(network_probe.socket, "socket", lambda *_args: FakeSocket())

    assert network_probe._dns_blocked() is False


def test_dns_probe_does_not_treat_receive_eperm_as_firewall_block(monkeypatch):
    class FakeSocket:
        def settimeout(self, _timeout):
            pass

        def sendto(self, payload, _address):
            return len(payload)

        def recvfrom(self, _size):
            raise PermissionError(errno.EPERM, "unclassified receive failure")

        def close(self):
            pass

    monkeypatch.setattr(network_probe.socket, "socket", lambda *_args: FakeSocket())

    assert network_probe._dns_blocked() is False


def test_network_probe_does_not_turn_indeterminate_results_into_passes(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(network_probe, "_connect", lambda *_args: (None, "EINTR"))
    monkeypatch.setattr(network_probe, "_dns_blocked", lambda: True)
    monkeypatch.setattr(network_probe.sys, "stdout", output)

    exit_code = network_probe.main(
        ["probe", "candidate", "172.20.0.2", "172.20.0.1", "45001"]
    )

    result = json.loads(output.getvalue().removeprefix("GATE_NETWORK_PROBE="))
    assert exit_code == 1
    assert result["postgres_tcp_allowed"] is False
    assert result["host_gateway_listener_blocked"] is False
    assert result["external_ipv4_blocked"] is False
    assert result["external_ipv6_blocked"] is False


def test_gate_policy_is_the_existing_manual_full_suite_command():
    from skybuild.manual_integration import DEFAULT_GATE_COMMAND, DEFAULT_GATE_COMMAND_SHA256

    assert gate.DEFAULT_GATE_COMMAND == DEFAULT_GATE_COMMAND
    assert gate.DEFAULT_GATE_COMMAND_SHA256 == DEFAULT_GATE_COMMAND_SHA256


def test_candidate_environment_contains_only_synthetic_database_credentials():
    environment = gate._candidate_environment("synthetic-password")

    assert sorted(environment) == sorted(gate.ENV_ALLOWLIST)
    assert environment["SKYBUILD_TEST_DSN"] == (
        "postgresql://postgres:synthetic-password@db:5432/skybuild_test"
    )
    assert environment["UV_NO_SYNC"] == "1"
    assert environment["UV_OFFLINE"] == "1"
    assert environment["UV_PROJECT_ENVIRONMENT"] == "/scratch/workspace/.venv"
    assert all("TOKEN" not in name and "SECRET" not in name and "KEY" not in name
               for name in environment)


def test_trusted_probe_environment_overrides_image_baked_dsns_and_host_values():
    environment = gate._trusted_probe_environment()

    assert sorted(environment) == sorted(gate.ENV_ALLOWLIST)
    assert environment["SKYBUILD_TEST_DSN"] == ""
    assert environment["SKYBUILD_GATE_HOST_GATEWAY"] == ""
    assert environment["SKYBUILD_GATE_HOST_LISTENER_PORT"] == ""


def test_saved_firewall_rules_accept_exact_filter_table_and_reject_extra_tables():
    value = """# Generated by iptables-save v1.8.9 (nf_tables) on today
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A INPUT -i lo -j ACCEPT
COMMIT
# Completed on today
"""

    assert gate._saved_rules(value) == ["INPUT DROP", "FORWARD DROP", "OUTPUT DROP",
                                        "-A INPUT -i lo -j ACCEPT"]
    with pytest.raises(gate.GateError, match="unexpected table"):
        gate._saved_rules(value.replace("*filter", "*nat\n:PREROUTING ACCEPT [0:0]"))


def test_saved_firewall_rules_accept_ipv6_save_header_and_reject_unknown_header():
    value = """# Generated by ip6tables-save v1.8.11 (nf_tables) on today
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A INPUT -i lo -j ACCEPT
COMMIT
# Completed on today
"""

    assert gate._saved_rules(value) == ["INPUT DROP", "FORWARD DROP", "OUTPUT DROP",
                                        "-A INPUT -i lo -j ACCEPT"]
    with pytest.raises(gate.GateError, match="unexpected table or rule directive"):
        gate._saved_rules(value.replace("ip6tables-save", "ipxxtables-save"))


def test_firewall_builder_smoke_output_uses_gate_parser_and_exact_rules(monkeypatch):
    ipv4 = """# Generated by iptables-save v1.8.11 (nf_tables) on today
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A INPUT -i lo -j ACCEPT
-A INPUT -s 198.18.0.3/32 -p tcp -m tcp --dport 5432 -j ACCEPT
-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A OUTPUT -d 127.0.0.11/32 -j DROP
-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
COMMIT
# Completed on today
"""
    ipv6 = """# Generated by ip6tables-save v1.8.11 (nf_tables) on today
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A INPUT -i lo -j ACCEPT
-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
COMMIT
# Completed on today
"""
    result = SimpleNamespace(stdout=("SKYBUILD_IPV4_SAVE_BEGIN\n" + ipv4
                                    + "SKYBUILD_IPV6_SAVE_BEGIN\n" + ipv6))
    observed = {}

    def run(arguments, *, timeout=120, check=True):
        observed["arguments"] = arguments
        return result

    monkeypatch.setattr(image_builder, "_run", run)
    smoke = image_builder._smoke_firewall_image("sha256:" + "a" * 64)

    assert smoke["parser_contract"] == "exact dual-stack filter rules"
    assert observed["arguments"][-1].count("iptables-save -t filter") == 1
    assert observed["arguments"][-1].count("ip6tables-save -t filter") == 1


def test_saved_firewall_rules_reject_user_defined_chains():
    value = """*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
:CUSTOM - [0:0]
COMMIT
"""

    with pytest.raises(gate.GateError, match="user-defined chain"):
        gate._saved_rules(value)


def test_firewall_policy_render_and_inspect_match_both_namespaces(monkeypatch):
    postgres_ip, candidate_ip = "172.18.0.2", "172.18.0.3"

    def saved(postgres_namespace, ipv6):
        expected = gate._expected_firewall_rules(postgres_ip, candidate_ip,
                                                ipv6=ipv6, postgres_namespace=postgres_namespace)
        lines = ["*filter"]
        lines.extend(":" + row.split()[0] + " " + row.split()[1] + " [0:0]"
                     for row in expected[:3])
        lines.extend(expected[3:])
        lines.append("COMMIT")
        return "\n".join(lines) + "\n"

    calls = []
    current_namespace = [False]

    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0,
            saved(current_namespace[0], args[2] == "ip6tables-save"), "")

    monkeypatch.setattr(gate, "_docker", docker)
    for postgres_namespace in (False, True):
        current_namespace[:] = [postgres_namespace]
        evidence = gate._verify_firewall_rules("a" * 64, postgres_ip, candidate_ip,
                                               postgres_namespace=postgres_namespace)
        assert evidence == {"firewall_defaults_drop": True,
                            "firewall_ipv4_default_drop": True,
                            "firewall_ipv6_default_drop": True,
                            "firewall_policy_applied": True}
    assert len(calls) == 4


def test_firewall_sidecar_requires_a_bounded_writable_lock_tmpfs():
    row = {
        "Id": "a" * 64, "Name": "/candidate-firewall", "Image": "sha256:" + "b" * 64,
        "Config": {"Labels": {gate.RUN_ID_LABEL: "run", gate.KIND_LABEL: "candidate_firewall"},
                   "Cmd": ["-ec", gate._firewall_script("172.18.0.2", "172.18.0.3",
                                                         postgres_namespace=False)],
                   "Entrypoint": ["/bin/sh"]},
        "HostConfig": {
            "NetworkMode": "container:" + "c" * 64, "CapAdd": ["NET_ADMIN"], "CapDrop": ["ALL"],
            "Privileged": False, "ReadonlyRootfs": True,
            "Tmpfs": {"/run": "rw,nosuid,nodev,size=1m,mode=0755"},
            "Memory": 128 * 1024**2, "MemorySwap": 128 * 1024**2,
            "NanoCpus": 250_000_000, "PidsLimit": 32, "PortBindings": {},
            "SecurityOpt": ["no-new-privileges:true"],
        "LogConfig": {"Type": "local", "Config": {"max-size": "4m", "max-file": "2"}},
        },
        "Mounts": [{"Type": "tmpfs", "Destination": "/run", "RW": True}],
    }
    gate._check_firewall_inspect(row, name="candidate-firewall", run_id="run",
                                 container_id="a" * 64, kind="candidate_firewall",
                                 image_id="sha256:" + "b" * 64, namespace_id="c" * 64,
                                 postgres_ip="172.18.0.2", candidate_ip="172.18.0.3",
                                 postgres_namespace=False)
    row["Mounts"] = []
    row["HostConfig"]["SecurityOpt"] = ["no-new-privileges"]
    row["HostConfig"]["CapAdd"] = ["CAP_NET_ADMIN"]
    gate._check_firewall_inspect(row, name="candidate-firewall", run_id="run",
                                 container_id="a" * 64, kind="candidate_firewall",
                                 image_id="sha256:" + "b" * 64, namespace_id="c" * 64,
                                 postgres_ip="172.18.0.2", candidate_ip="172.18.0.3",
                                 postgres_namespace=False)
    row["HostConfig"]["CapAdd"] = ["CAP_NET_ADMIN", "CAP_NET_RAW"]
    with pytest.raises(gate.GateError):
        gate._check_firewall_inspect(row, name="candidate-firewall", run_id="run",
                                     container_id="a" * 64, kind="candidate_firewall",
                                     image_id="sha256:" + "b" * 64, namespace_id="c" * 64,
                                     postgres_ip="172.18.0.2", candidate_ip="172.18.0.3",
                                     postgres_namespace=False)
    row["HostConfig"]["Tmpfs"] = {}
    with pytest.raises(gate.GateError, match="identity, capabilities, command, or caps"):
        gate._check_firewall_inspect(row, name="candidate-firewall", run_id="run",
                                     container_id="a" * 64, kind="candidate_firewall",
                                     image_id="sha256:" + "b" * 64, namespace_id="c" * 64,
                                     postgres_ip="172.18.0.2", candidate_ip="172.18.0.3",
                                     postgres_namespace=False)


def test_postgres_inspection_requires_exact_tmpfs_and_no_extra_mounts():
    container_id = "a" * 64
    image_id = "sha256:" + "b" * 64
    network = "isolated-net"
    destinations = ("/var/lib/postgresql/data", "/var/run/postgresql", "/tmp")
    row = {
        "Id": container_id, "Name": "/postgres-run", "Image": image_id,
        "Config": {"Labels": {gate.RUN_ID_LABEL: "run", gate.KIND_LABEL: "postgres"},
                   "User": "999:999"},
        "HostConfig": {
            "NetworkMode": network, "Memory": 1536 * 1024**2,
            "MemorySwap": 1536 * 1024**2, "NanoCpus": 1_000_000_000,
            "PidsLimit": 128, "PortBindings": {}, "ReadonlyRootfs": True,
            "LogConfig": {"Type": "local", "Config": {"max-size": "32m", "max-file": "2"}},
            "CapAdd": [], "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
            "Tmpfs": {
                destinations[0]: "rw,nosuid,nodev,noexec,size=1073741824,uid=999,gid=999,mode=0700",
                destinations[1]: "rw,nosuid,nodev,noexec,size=16777216,uid=999,gid=999,mode=3775",
                destinations[2]: "rw,nosuid,nodev,noexec,size=134217728,uid=999,gid=999,mode=1777",
            },
        },
        "Mounts": [{"Type": "tmpfs", "Destination": path, "RW": True} for path in destinations],
        "NetworkSettings": {"Networks": {network: {"NetworkID": "c" * 64}}},
    }

    gate._check_postgres_inspect(row, name="postgres-run", run_id="run",
                                 container_id=container_id, image_id=image_id, network=network)
    row["Mounts"] = []
    row["HostConfig"]["SecurityOpt"] = ["no-new-privileges"]
    gate._check_postgres_inspect(row, name="postgres-run", run_id="run",
                                 container_id=container_id, image_id=image_id, network=network)
    row["Mounts"].append({"Type": "bind", "Destination": "/host", "RW": False})
    with pytest.raises(gate.GateError):
        gate._check_postgres_inspect(row, name="postgres-run", run_id="run",
                                     container_id=container_id, image_id=image_id, network=network)


def test_attestation_key_preflight_requires_owned_mode_0600_key(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)

    gate._validate_attestation_key(key, "runner-key-1")
    key.chmod(0o640)
    with pytest.raises(gate.GateError, match="mode 0600"):
        gate._validate_attestation_key(key, "runner-key-1")


def test_security_options_recognize_docker_normalized_no_new_privileges():
    assert gate._has_no_new_privileges(["no-new-privileges"])
    assert gate._has_no_new_privileges(["no-new-privileges:true"])
    assert not gate._has_no_new_privileges(["no-new-privileges:false"])
    assert not gate._has_no_new_privileges("no-new-privileges")


def test_signer_receives_keyword_key_arguments(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    seen = {}

    def sign(predicate, *, key_path, key_id):
        seen.update(predicate=predicate, key_path=key_path, key_id=key_id)
        return {"signed": True}

    monkeypatch.setitem(sys.modules, "trusted_gate_attestation", SimpleNamespace(sign_attestation=sign))
    predicate = {"result": 0}
    assert gate._sign_receipt(predicate, key, "key-1") == {"signed": True}
    assert seen == {"predicate": predicate, "key_path": key, "key_id": "key-1"}


def test_archive_extraction_rejects_parent_traversal(tmp_path):
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo("../escape")
        member.size = 4
        stream.addfile(member, io.BytesIO(b"oops"))

    with pytest.raises(gate.GateError, match="unsafe member"):
        gate._extract_archive(archive, tmp_path / "out")
    assert not (tmp_path / "escape").exists()


def test_archive_extraction_rejects_symlink_members(tmp_path):
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as stream:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        stream.addfile(link)

    with pytest.raises(gate.GateError, match="unsafe member"):
        gate._extract_archive(archive, tmp_path / "out")


def _candidate_row(tmp_path):
    archive = tmp_path / "source"
    archive.mkdir()
    fixture = tmp_path / "entrypoint.py"
    fixture.write_text("runner", encoding="utf-8")
    probe = tmp_path / "network_probe.py"
    probe.write_text("probe", encoding="utf-8")
    env = gate._candidate_environment("pw")
    mounts = [
        {"Type": "bind", "Source": str(archive), "Destination": "/candidate", "RW": False},
        {"Type": "tmpfs", "Source": "", "Destination": "/scratch", "RW": True},
        {"Type": "bind", "Source": str(fixture), "Destination": "/runner/entrypoint.py", "RW": False},
        {"Type": "bind", "Source": str(probe), "Destination": "/runner/network_probe.py", "RW": False},
    ]
    env_list = [name + "=" + value for name, value in env.items()]
    row = {
        "Id": "a" * 64,
        "Name": "/candidate-run",
        "Image": "sha256:" + "b" * 64,
        "Config": {"Labels": {gate.RUN_ID_LABEL: "run", gate.KIND_LABEL: "candidate"},
                   "Cmd": gate.DEFAULT_GATE_COMMAND, "Entrypoint": ["/runner/entrypoint.py"],
                   "User": "10001:10001", "Env": env_list},
        "HostConfig": {"ReadonlyRootfs": True, "NetworkMode": "internal-net",
                       "Memory": 4 * 1024**3, "MemorySwap": 4 * 1024**3,
                       "NanoCpus": 2_000_000_000, "PidsLimit": 256, "Init": True,
                       "ShmSize": 256 * 1024**2, "Privileged": False,
                       "LogConfig": {"Type": "local", "Config": {"max-size": "128m", "max-file": "2"}},
                       "PidMode": "private", "IpcMode": "private", "PortBindings": {},
                       "Tmpfs": {"/scratch": "rw,exec,nosuid,nodev,size=2g,uid=10001,gid=10001,mode=0700"},
                       "ExtraHosts": ["db:172.18.0.3"],
                       "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"]},
        "Mounts": mounts,
        "NetworkSettings": {"Networks": {"internal-net": {"NetworkID": "c" * 64,
                                                                  "IPAddress": "172.18.0.3"}}},
    }
    return row, archive, fixture, probe, env


def test_candidate_inspection_accepts_only_exact_mount_and_environment_policy(tmp_path):
    row, archive, fixture, probe, env = _candidate_row(tmp_path)
    result = gate._check_candidate_inspect(
        row, name="candidate-run", run_id="run", container_id="a" * 64,
        image_id="sha256:" + "b" * 64, archive_root=archive, fixture_path=fixture,
        probe_path=probe, env=env, network="internal-net", archive_sha256="d" * 64,
        fixture_sha256="e" * 64, probe_sha256="f" * 64,
        postgres_ip="172.18.0.3",
    )

    assert result["environment_allowlist"] == sorted(gate.ENV_ALLOWLIST)
    assert result["source_mount_readonly"] is True
    assert result["docker_socket_mounted"] is False
    assert result["host_credentials_mounted"] is False
    assert result["init_process_enabled"] is True
    assert {item["source"] for item in result["mounts"]} == {
        "sha256:" + "d" * 64, "tmpfs", "sha256:" + "e" * 64, "sha256:" + "f" * 64
    }


def test_candidate_inspection_accepts_docker_29_tmpfs_representation(tmp_path):
    row, archive, fixture, probe, env = _candidate_row(tmp_path)
    row["Mounts"] = [mount for mount in row["Mounts"] if mount["Destination"] != "/scratch"]
    row["HostConfig"]["SecurityOpt"] = ["no-new-privileges"]
    result = gate._check_candidate_inspect(
        row, name="candidate-run", run_id="run", container_id="a" * 64,
        image_id="sha256:" + "b" * 64, archive_root=archive, fixture_path=fixture,
        probe_path=probe, env=env, network="internal-net", archive_sha256="d" * 64,
        fixture_sha256="e" * 64, probe_sha256="f" * 64,
        postgres_ip="172.18.0.3",
    )
    assert any(item == {"target": "/scratch", "mode": "rw", "kind": "tmpfs",
                       "source_class": "scratch", "source": "tmpfs"}
               for item in result["mounts"])


def test_candidate_inspection_rejects_normalized_tmpfs_size(tmp_path):
    row, archive, fixture, probe, env = _candidate_row(tmp_path)
    row["HostConfig"]["Tmpfs"]["/scratch"] = (
        "rw,exec,nosuid,nodev,size=2147483648,uid=10001,gid=10001,mode=0700")
    with pytest.raises(gate.GateError, match='"field":"tmpfs_options"'):
        gate._check_candidate_inspect(
            row, name="candidate-run", run_id="run", container_id="a" * 64,
            image_id="sha256:" + "b" * 64, archive_root=archive, fixture_path=fixture,
            probe_path=probe, env=env, network="internal-net", archive_sha256="d" * 64,
            fixture_sha256="e" * 64, probe_sha256="f" * 64,
            postgres_ip="172.18.0.3",
        )


@pytest.mark.parametrize("mutation", ["extra_mount", "extra_env", "port_bind", "extra_network",
                                      "privileged", "init_disabled"])
def test_candidate_inspection_fails_closed_on_isolation_changes(tmp_path, mutation):
    row, archive, fixture, probe, env = _candidate_row(tmp_path)
    if mutation == "extra_mount":
        row["Mounts"].append({"Type": "bind", "Source": "/home/user", "Destination": "/home", "RW": False})
    elif mutation == "extra_env":
        row["Config"]["Env"].append("GH_TOKEN=private")
    elif mutation == "port_bind":
        row["HostConfig"]["PortBindings"] = {"5432/tcp": [{"HostPort": "5432"}]}
    elif mutation == "extra_network":
        row["NetworkSettings"]["Networks"]["bridge"] = {"NetworkID": "f" * 64}
    elif mutation == "init_disabled":
        row["HostConfig"]["Init"] = False
    else:
        row["HostConfig"]["Privileged"] = True

    with pytest.raises(gate.GateError):
        gate._check_candidate_inspect(
            row, name="candidate-run", run_id="run", container_id="a" * 64,
            image_id="sha256:" + "b" * 64, archive_root=archive, fixture_path=fixture,
            probe_path=probe, env=env, network="internal-net", archive_sha256="d" * 64,
            fixture_sha256="e" * 64, probe_sha256="f" * 64,
            postgres_ip="172.18.0.3",
        )


def test_container_create_inspection_error_does_not_claim_absence(monkeypatch):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "permission denied")

    monkeypatch.setattr(gate, "_docker", docker)
    with pytest.raises(gate.GateError, match="cannot be reconciled"):
        gate._container_info("owned-name", "run", None, "candidate")
    assert len(calls) == 1


def test_container_info_recognizes_docker_29_absence_by_name(monkeypatch):
    def docker(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, "", "Error response from daemon: No such container: owned-name"
        )

    monkeypatch.setattr(gate, "_docker", docker)
    assert gate._container_info("owned-name", "run", None, "candidate") is None


def test_network_info_recognizes_docker_29_absence(monkeypatch):
    def docker(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, "", "Error response from daemon: network owned-network not found"
        )

    monkeypatch.setattr(gate, "_docker", docker)
    assert gate._network_info("owned-network", "run", None) is None


def test_container_cleanup_reconciles_by_name_after_unknown_create(monkeypatch):
    owned = {
        "Id": "a" * 64, "Name": "/owned-name",
        "Config": {"Labels": {gate.RUN_ID_LABEL: "run", gate.KIND_LABEL: "candidate"}},
        "Image": "sha256:" + "b" * 64, "State": {"Running": False},
    }
    calls = []
    inspected = False

    def docker(*args, **kwargs):
        nonlocal inspected
        calls.append(args)
        if args[:3] == ("inspect", "--type", "container"):
            if not inspected:
                inspected = True
                return subprocess.CompletedProcess(args, 0, json.dumps([owned]), "")
            return subprocess.CompletedProcess(args, 1, "", f"Error: No such object: {args[-1]}")
        if args[0] == "rm":
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(gate, "_docker", docker)
    journal = type("JournalStub", (), {"event": lambda *args, **kwargs: None})()
    assert gate._remove_container("owned-name", "run", None, "candidate", journal)
    assert any(call[0] == "rm" for call in calls)


def test_log_redaction_streams_and_hashes_without_persisting_password(monkeypatch, tmp_path):
    class FakeProcess:
        def __init__(self, command, *, stdout, stderr, preexec_fn):
            self.stdout = stdout
            stdout.write(b"prefix-synthetic-")
            stdout.write(b"password-suffix\n")

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(gate.subprocess, "Popen", FakeProcess)
    log_path = tmp_path / "candidate.log"
    digest = gate._write_redacted_log("a" * 64, log_path, "synthetic-password")

    content = log_path.read_bytes()
    assert b"synthetic-password" not in content
    assert b"[REDACTED]" in content
    assert digest == hashlib.sha256(content).hexdigest()
    assert not log_path.with_suffix(".raw").exists()


@pytest.mark.parametrize("offset", [65536 - 2 * 17 - 5, 65536 - 5, 65536, 65536 + 4])
def test_log_redaction_covers_input_and_retained_prefix_boundaries(monkeypatch, tmp_path, offset):
    secret = b"synthetic-password"
    raw = b"x" * offset + secret + b"y" * 100

    class FakeProcess:
        def __init__(self, command, *, stdout, **kwargs):
            stdout.write(raw)

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(gate.subprocess, "Popen", FakeProcess)
    log = tmp_path / "log"
    gate._write_redacted_log("a" * 64, log, secret.decode())
    assert log.read_bytes() == raw.replace(secret, b"[REDACTED]")


def test_archive_producer_has_kernel_write_bound_and_reaps_timeout(monkeypatch, tmp_path):
    seen = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, command, *, stdout, preexec_fn, **kwargs):
            seen["preexec"] = preexec_fn
            stdout.write(b"archive")

        def communicate(self, **kwargs):
            if not seen.get("killed"):
                raise subprocess.TimeoutExpired("git", 120)

        def kill(self):
            seen["killed"] = True

    monkeypatch.setattr(gate.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(gate.resource, "setrlimit", lambda kind, limits: seen.update(limits=limits))
    with pytest.raises(gate.GateError, match="deadline"):
        gate._archive(tmp_path, "a" * 40, tmp_path / "archive")
    seen["preexec"]()
    assert seen["limits"] == (gate.MAX_ARCHIVE_BYTES, gate.MAX_ARCHIVE_BYTES)
    assert seen["killed"]


def test_archive_kernel_bound_rejects_actual_git_overflow(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "MAX_ARCHIVE_BYTES", 1024)
    destination = tmp_path / "bounded.tar"
    with pytest.raises(gate.GateError, match="byte limit"):
        gate._archive(Path(gate.__file__).resolve().parents[1], gate.HISTORY_COMMITS[0], destination)
    assert destination.stat().st_size <= 1024


def test_trusted_launcher_does_not_import_hostile_candidate_before_fixed_exec(tmp_path):
    import ast
    from importlib.machinery import PathFinder

    package = tmp_path / "skybuild"
    package.mkdir()
    marker = tmp_path / "executed"
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('hostile')\n"
        "import os\nos._exit(0)\n")
    spec = PathFinder.find_spec("skybuild", [str(tmp_path)])
    assert spec.origin == str(package / "__init__.py")
    assert not marker.exists()
    source = Path(gate.__file__).with_name("gate_container_entrypoint.py").read_text()
    tree = ast.parse(source)
    assert not any(isinstance(node, ast.Import) and any(name.name == "skybuild" for name in node.names)
                   for node in ast.walk(tree))
    assert "#!/usr/bin/env -S python3 -I" in source
    assert 'os.execvpe(argv[0], argv, os.environ.copy())' in source


def test_cli_rejects_null_attestation_even_after_zero_test_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "execute", lambda *args: {
        "exit_code": 0, "cleanup_confirmed": True, "attestation": None, "failure": "GateError"})
    assert gate.main(["--checkout", str(tmp_path), "--expected-predicate", "predicate",
                      "--output-dir", str(tmp_path), "--reviewed-go-record", "go",
                      "--attestation-key", "key", "--attestation-key-id", "key-1", "--execute"]) == 1


def test_sanitized_history_is_deterministic_fetchable_and_contains_no_host_metadata(tmp_path):
    checkout = Path(gate.__file__).resolve().parents[1]
    history = tmp_path / "source/.git"
    history.parent.mkdir()
    first, size = gate._history_fixture(checkout, history)
    second, other_size = gate._history_fixture(checkout, tmp_path / "repeat")
    assert first == second and size == other_size < gate.MAX_HISTORY_PACK_BYTES
    assert not (history / "hooks").exists()
    assert not (history / "objects/info/alternates").exists()
    assert "remote" not in (history / "config").read_text()
    environment = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    for commit in gate.HISTORY_COMMITS:
        result = subprocess.run(["git", "-C", str(history.parent), "show",
                                 commit + ":docs/design/mastertodo.md"], capture_output=True,
                                env=environment, timeout=10, check=True)
        assert b"SKYBUILD" in result.stdout
    target = tmp_path / "fetched"
    subprocess.run(["git", "init", "--quiet", str(target)], env=environment, check=True, timeout=10)
    subprocess.run(["git", "-C", str(target), "fetch", "--quiet", str(history.parent),
                    gate.HISTORY_COMMITS[0]], env=environment, check=True, timeout=10)


def test_hostile_package_cannot_exit_before_trusted_launcher_exec(monkeypatch, tmp_path):
    import gate_container_entrypoint as entrypoint

    source = tmp_path / "candidate"
    scratch = tmp_path / "scratch"
    (source / "src/skybuild").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / ".git").mkdir()
    (source / ".git/HEAD").write_text("fixture")
    marker = tmp_path / "hostile-ran"
    (source / "src/skybuild/__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nimport os\nos._exit(0)\n")
    scratch.mkdir()
    (scratch / ".firewall-ready").write_text("firewall-ready\n")
    original_write = Path.write_text

    def path(value):
        value = str(value)
        for prefix, replacement in (("/candidate", source), ("/scratch", scratch)):
            if value == prefix or value.startswith(prefix + "/"):
                return replacement / value.removeprefix(prefix).lstrip("/")
        return Path(value)

    def write(self, *args, **kwargs):
        if self.name == ".skybuild-readonly-probe":
            raise PermissionError("readonly mount")
        return original_write(self, *args, **kwargs)

    launched = []
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(entrypoint, "Path", path)
    monkeypatch.setattr(entrypoint, "os", SimpleNamespace(
        environ={"SKYBUILD_GATE_RELEASE_FILE": "/scratch/.firewall-ready"},
        chdir=lambda workspace: None,
        execvpe=lambda executable, argv, environment: launched.append(argv)))
    monkeypatch.setattr(entrypoint, "_network_preflight", lambda: None)
    monkeypatch.setattr(entrypoint, "_prepare_environment", lambda *args: None)
    assert entrypoint.main() == 127
    assert launched == [gate.DEFAULT_GATE_COMMAND]
    assert not marker.exists()


class _DockerModel:
    """Interpret real builder arguments, then expose Docker-shaped observations."""

    def __init__(self, predicate, mutation=None):
        self.predicate = predicate
        self.mutation = mutation
        self.rows = {}
        self.names = {}
        self.calls = []
        self.network = None
        self.released = False

    @staticmethod
    def _bytes(value):
        match = __import__("re").fullmatch(r"(\d+)([mg]?)", value)
        return int(match[1]) * {"": 1, "m": 1024**2, "g": 1024**3}[match[2]]

    def __call__(self, *args, **kwargs):
        self.calls.append(args)

        def result(value="", code=0, error=""):
            return subprocess.CompletedProcess(args, code, value, error)

        if args[:2] == ("image", "inspect"):
            labels = {
                "org.skybuild.full-test.policy-sha256": self.predicate["gate_policy_sha256"],
                "org.skybuild.full-test.uv-lock-sha256": hashlib.sha256(b"lock").hexdigest(),
                "org.skybuild.full-test.pyproject-sha256": hashlib.sha256(b"project").hexdigest(),
                "org.skybuild.full-test.command-sha256": gate.DEFAULT_GATE_COMMAND_SHA256,
                "org.skybuild.full-test.entrypoint-sha256": self.predicate["trusted_entrypoint_sha256"],
                "org.skybuild.full-test.network-probe-sha256": self.predicate["network_probe_sha256"],
                "org.skybuild.full-test.uv-version": gate.UV_VERSION,
                "org.skybuild.full-test.firewall-policy-sha256": gate.FIREWALL_POLICY_SHA256,
            }
            return result(json.dumps([{"Id": args[2], "Config": {"Labels": labels,
                "Env": ["PATH=/usr/local/bin:/usr/bin:/bin"]}}]))
        if args[:2] == ("network", "create"):
            label = args[args.index("--label") + 1].split("=", 1)
            self.network = {"Id": "e" * 64, "Internal": True, "EnableIPv6": False,
                "Labels": {label[0]: label[1]}, "IPAM": {"Config": [{"Gateway": "172.18.0.1"}]}}
            return result(self.network["Id"])
        if args[:2] == ("network", "inspect"):
            return result(json.dumps([self.network])) if self.network else result(
                code=1, error="Error: No such network: " + args[2])
        if args[:2] == ("network", "rm"):
            self.network = None
            return result()
        if args[0] == "run":
            options = {}
            index = 1
            flags = {"--detach", "--read-only", "--init"}
            while not args[index].startswith("sha256:"):
                item = args[index]
                if item in flags:
                    key, value = item, True
                elif "=" in item:
                    key, value = item.split("=", 1)
                else:
                    key, value = item, args[index + 1]
                    index += 1
                options.setdefault(key, []).append(value)
                index += 1
            image = args[index]
            command = list(args[index + 1:])
            single = lambda name, default=None: options.get(name, [default])[0]
            labels = dict(item.split("=", 1) for item in options["--label"])
            kind = labels[gate.KIND_LABEL]
            identity = str(len(self.rows) + 1) * 64
            mounts, tmpfs = [], {}
            for item in options.get("--mount", []):
                fields = dict((field.split("=", 1) if "=" in field else (field, True))
                              for field in item.split(","))
                mounts.append({"Type": fields["type"], "Source": fields["src"],
                               "Destination": fields["dst"], "RW": not fields.get("readonly", False)})
            for item in options.get("--tmpfs", []):
                target, value = item.split(":", 1)
                normalized = []
                for field in value.split(","):
                    normalized.append(field)
                tmpfs[target] = ",".join(normalized)
                mounts.append({"Type": "tmpfs", "Destination": target, "RW": True})
            network_mode = single("--network")
            address = "172.18.0.2" if kind == "postgres" else "172.18.0.3"
            row = {
                "Id": identity, "Name": "/" + single("--name"), "Image": image,
                "Config": {"Labels": labels, "User": single("--user", ""),
                           "Cmd": command, "Entrypoint": [single("--entrypoint")],
                           "Env": options.get("--env", [])},
                "HostConfig": {"ReadonlyRootfs": single("--read-only", False),
                    "Init": single("--init", False),
                    "NetworkMode": network_mode, "Memory": self._bytes(single("--memory")),
                    "MemorySwap": self._bytes(single("--memory-swap")),
                    "NanoCpus": int(float(single("--cpus")) * 1_000_000_000),
                    "PidsLimit": int(single("--pids-limit")),
                    "ShmSize": self._bytes(single("--shm-size", "64m")),
                    "Privileged": False, "PortBindings": {}, "Tmpfs": tmpfs,
                    "ExtraHosts": options.get("--add-host", []),
                    "CapAdd": options.get("--cap-add", []), "CapDrop": options.get("--cap-drop", []),
                    "SecurityOpt": [value + ":true" for value in options.get("--security-opt", [])],
                    "LogConfig": {"Type": single("--log-driver"),
                                  "Config": dict(value.split("=", 1) for value in options["--log-opt"])}},
                "Mounts": mounts, "State": {"Status": "running", "Running": True},
                "NetworkSettings": {"Networks": {network_mode: {
                    "NetworkID": "e" * 64, "IPAddress": address}}},
            }
            if self.mutation == "wrong_pg_ip" and kind == "candidate":
                row["HostConfig"]["ExtraHosts"] = ["db:" + address]
            if self.mutation == "helper_label" and kind == "postgres_firewall":
                labels[gate.KIND_LABEL] = "firewall"
            if kind.endswith("probe"):
                row["State"] = {"Status": "exited", "Running": False, "ExitCode": 0}
            self.rows[identity] = row
            self.names[single("--name")] = identity
            return result(identity)
        if args[:3] == ("inspect", "--type", "container"):
            identity = self.names.get(args[3], args[3])
            row = self.rows.get(identity)
            if row is None:
                return result(code=1, error="Error: No such object: " + args[3])
            if row["Config"]["Labels"][gate.KIND_LABEL] == "candidate" and self.released:
                row["State"] = {"Status": "exited", "Running": False,
                                "ExitCode": 2 if self.mutation == "test_exit" else 0}
            return result(json.dumps([row]))
        if args[0] == "exec":
            if args[2] in {"iptables-save", "ip6tables-save"}:
                assert args[3:] == ("-t", "filter")
                row = self.rows[args[1]]
                postgres = row["Config"]["Labels"][gate.KIND_LABEL] == "postgres_firewall"
                rules = gate._expected_firewall_rules("172.18.0.2", "172.18.0.3",
                    ipv6=args[2] == "ip6tables-save", postgres_namespace=postgres)
                text = ["*filter", *(":" + value + " [0:0]" for value in rules[:3]), *rules[3:], "COMMIT"]
                return result("\n".join(text))
            if args[2] == "python3":
                self.released = True
            return result()
        if args[0] == "logs":
            return result(self.log(args[1]))
        if args[0] == "stop":
            self.rows[args[-1]]["State"] = {"Running": False, "Status": "exited", "ExitCode": 1}
            return result()
        if args[0] == "rm":
            if self.mutation == "cleanup":
                raise gate.GateError("injected uncertain cleanup")
            del self.rows[args[-1]]
            return result()
        raise AssertionError(args)

    def log(self, identity):
        row = self.rows[identity]
        kind = row["Config"]["Labels"][gate.KIND_LABEL]
        if kind.endswith("probe"):
            port = int(row["Config"]["Cmd"][-1])
            evidence = {"namespace": kind.removesuffix("_probe"), "dns_blocked": True,
                "external_ipv4_blocked": True, "external_ipv6_blocked": True,
                "host_gateway_listener_blocked": True, "host_listener_port": port,
                "postgres_tcp_allowed": kind == "candidate_probe"}
            prefix = "2026-10-10T00:00:00Z " if self.mutation == "probe_timestamp" else ""
            return prefix + "GATE_NETWORK_PROBE=" + json.dumps(evidence) + "\n"
        blocked = "GATE_CANDIDATE_BLOCKED=firewall_release_pending\n"
        if not self.released:
            return blocked
        text = blocked + "\n".join(("GATE_FIREWALL_RELEASE=verified",
            "GATE_CANDIDATE_ARCHIVE_READONLY=true", "GATE_CANDIDATE_COPY=complete",
            "GATE_OFFLINE_ENVIRONMENT=prepared",
            "GATE_PREFLIGHT_SOURCE_PATH=/scratch/workspace/src/skybuild/__init__.py",
            "GATE_COMMAND_LAUNCH=trusted_exec", "GATE_HOST_GATEWAY_PROBE=blocked",
            "GATE_EXTERNAL_DIRECT_IP_PROBE=blocked", "GATE_EXTERNAL_DNS_PROBE=blocked"))
        text = text.replace("GATE_COMMAND_LAUNCH=trusted_exec", "") if self.mutation == "missing_preflight" else text
        return text + "\n" + getattr(self, "pytest_summary", "1 passed in 0.01s") + "\n"


def _execute_modeled_gate(monkeypatch, tmp_path, mutation=None, *, focused_mode=False,
                          pytest_summary="1 passed in 0.01s"):
    import trusted_gate_attestation as attestation

    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    predicate = {"bundle_id": "bundle-" + "a" * 24, "pr_number": 1,
        "target_ref": "refs/heads/dev-001", "target_base": "b" * 40,
        "candidate_commit": "c" * 40, "candidate_tree": "d" * 40,
        "candidate_archive_sha256": "e" * 64, "candidate_history_sha256": "f" * 64,
        "gate_argv": gate.DEFAULT_GATE_COMMAND, "gate_command_sha256": gate.DEFAULT_GATE_COMMAND_SHA256,
        "gate_policy_sha256": "1" * 64, "runner_identity": "trusted-runner",
        "runner_version": gate.RUNNER_VERSION, "runner_image_id": "sha256:" + "2" * 64,
        "postgres_image_id": "sha256:" + "3" * 64, "firewall_image_id": "sha256:" + "4" * 64,
        "firewall_policy_sha256": gate.FIREWALL_POLICY_SHA256,
        "network_probe_sha256": hashlib.sha256(Path(gate.__file__).with_name("gate_network_probe.py").read_bytes()).hexdigest(),
        "trusted_entrypoint_sha256": hashlib.sha256(Path(gate.__file__).with_name("gate_container_entrypoint.py").read_bytes()).hexdigest(),
        "attestation_signer_sha256": hashlib.sha256(Path(attestation.__file__).read_bytes()).hexdigest(),
        "candidate_blocked_until_probe": True, "execution_host": gate.platform.node().split(".", 1)[0].lower()}
    runner = {"commit": "5" * 40, "tree": "6" * 40, "script_sha256": "7" * 64,
        "entrypoint_sha256": predicate["trusted_entrypoint_sha256"],
        "network_probe_sha256": predicate["network_probe_sha256"],
        "attestation_signer_sha256": predicate["attestation_signer_sha256"]}
    candidate = {"checkout": str(tmp_path), "commit": predicate["candidate_commit"], "tree": predicate["candidate_tree"]}

    def archive(checkout, commit, destination):
        with tarfile.open(destination, "w") as stream:
            for name, data in {"src/skybuild/__init__.py": b"", "tests/test_one.py": b"",
                               "uv.lock": b"lock", "pyproject.toml": b"project"}.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                stream.addfile(member, io.BytesIO(data))
        return predicate["candidate_archive_sha256"], destination.stat().st_size

    def history(checkout, destination):
        destination.mkdir()
        return predicate["candidate_history_sha256"], 10

    monkeypatch.setattr(gate, "_candidate_identity", lambda *args: candidate)
    monkeypatch.setattr(gate, "_runner_provenance", lambda: runner)
    monkeypatch.setattr(gate, "_archive", archive)
    monkeypatch.setattr(gate, "_history_fixture", history)
    monkeypatch.setattr(gate, "_host_memory_check", lambda: 14)
    monkeypatch.setattr(gate, "_OwnedHostListener", lambda address: SimpleNamespace(port=32768, close=lambda: None))
    model = _DockerModel(predicate, mutation)
    model.pytest_summary = pytest_summary
    monkeypatch.setattr(gate, "_docker", model)

    class FakeLogProcess:
        def __init__(self, command, *, stdout, **kwargs):
            assert command[:2] == ["docker", "logs"]
            stdout.write(model.log(command[2]).encode())

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(gate.subprocess, "Popen", FakeLogProcess)
    predicate_path = tmp_path / "predicate.json"
    if focused_mode:
        predicate.pop("bundle_id")
        predicate.pop("pr_number")
    predicate_path.write_text(json.dumps(predicate))
    predicate_path.chmod(0o600)
    if focused_mode:
        from datetime import datetime, timedelta, timezone
        import gate_policy as policy
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        record = tmp_path / "focus.consumed.json"
        record.write_text('{"consumed":true}')
        record.chmod(0o600)
        approved = {"permit_id": "focused-permit", "project_id": "skybuild", "base_sha": predicate["target_base"],
                    "runner_source": runner, "execution_host": predicate["execution_host"], "expires_at": expires,
                    "images": {name: predicate[name] for name in ("runner_image_id", "postgres_image_id", "firewall_image_id")}}
        authorization = policy.Authorization(approved, record, "a" * 64)
        authorization.predicate_sha256 = gate._digest_bytes(gate._canonical(predicate))
        authorization.policy_sha256 = "b" * 64
        authorization.trust = {"validation": {"key_id": "key-1", "principal": "trusted-validator", "key_path": str(key)}}
        authorization.focused = {"task": {"task_id": "client", "assignment_id": "assigned", "worker_id": "worker",
                                          "brief_sha256": "c" * 64, "approved_patch_sha256": "d" * 64},
            "workflow": {"attempt_id": "attempt", "claim_fence": 1, "input_generation": 1, "definition_revision": 1,
                         "policy_version": "v1", "source_sha": predicate["candidate_commit"], "base_sha": predicate["target_base"]},
            "stage": "unit", "profile": "petri-client-unit-v1", "source_head": predicate["candidate_commit"],
            "conductor_intent_sha256": "e" * 64}
        monkeypatch.setattr(authorization, "check", lambda **kwargs: None)
        tmp_path.chmod(0o700)
        result = gate.execute(tmp_path, predicate_path, None, key, "key-1", tmp_path,
                              _policy_authorization=authorization)
        return result, predicate, model, key
    plan = gate.prepare(tmp_path, predicate_path)
    go = {"decision": "GO", "task_id": gate.TASK_ID, "plan_sha256": gate.plan_digest(plan),
        "predicate_sha256": plan["policy"]["predicate_sha256"], "attestation_key_id": "key-1",
        "reviewer": "independent", "root_authorizer": "root", "runner_commit": runner["commit"],
        "runner_tree": runner["tree"], "runner_script_sha256": runner["script_sha256"],
        "entrypoint_sha256": runner["entrypoint_sha256"],
        **{name: predicate[name] for name in ("runner_image_id", "postgres_image_id", "firewall_image_id",
            "firewall_policy_sha256", "network_probe_sha256", "attestation_signer_sha256", "execution_host")}}
    go_path = tmp_path / "go.json"
    go_path.write_text(json.dumps(go))
    go_path.chmod(0o600)
    tmp_path.chmod(0o700)
    result = gate.execute(tmp_path, predicate_path, go_path, key, "key-1", tmp_path)
    return result, predicate, model, key


def test_focused_real_builder_inspect_cleanup_sign_and_verify(monkeypatch, tmp_path):
    import gate_policy as policy
    result, predicate, model, key = _execute_modeled_gate(monkeypatch, tmp_path, focused_mode=True,
                                                         pytest_summary="5 passed, 19 deselected in 0.01s")
    receipt = json.loads(Path(result["attestation"]).read_bytes())
    payload = policy.verify_focused(receipt, {"key_id": "key-1", "principal": "trusted-validator", "key_path": str(key)},
                                    {"source_head": predicate["candidate_commit"], "stage": "unit"})
    assert payload["reported_counts"]["passed"] == 5 and payload["reported_counts"]["collected"] == 24
    assert payload["command"] == policy.FOCUSED_COMMANDS["petri-client-unit-v1"]
    assert "bundle_id" not in payload["isolation"] and "pr_number" not in payload["isolation"]
    assert result["validation_passed"] is True and model.released


@pytest.mark.parametrize("summary", ["2 skipped in 0.01s", "no tests ran in 0.01s", "1 passed, 1 xfailed in 0.01s"])
def test_focused_empty_or_skipped_collection_cannot_pass(monkeypatch, tmp_path, summary):
    import gate_policy as policy
    result, _, _, key = _execute_modeled_gate(monkeypatch, tmp_path, focused_mode=True, pytest_summary=summary)
    assert result["validation_passed"] is False
    with pytest.raises(policy.PolicyError, match="nonempty"):
        policy.verify_focused(json.loads(Path(result["attestation"]).read_bytes()),
                              {"key_id": "key-1", "principal": "trusted-validator", "key_path": str(key)}, {})


def test_real_builder_inspect_cleanup_sign_and_verify_roundtrip(monkeypatch, tmp_path):
    import trusted_gate_attestation as attestation

    result, predicate, model, key = _execute_modeled_gate(monkeypatch, tmp_path)
    assert result["failure"] is None and result["exit_code"] == 0 and result["cleanup_confirmed"]
    assert not model.rows and model.network is None
    candidate_run = next(call for call in model.calls if call[0] == "run"
                        and gate.KIND_LABEL + "=candidate" in call)
    assert "--init" in candidate_run
    receipt = json.loads(Path(result["attestation"]).read_bytes())
    expected = {**{name: predicate[name] for name in attestation.PINNED_PREDICATE_FIELDS
                   if name in predicate}, "environment_allowlist": gate.ENV_ALLOWLIST,
                "resource_limits": gate.ATTESTED_CANDIDATE_LIMITS}
    verified = attestation.verify_attestation(receipt, trusted_keys={"key-1": key},
        expected_predicate=expected, expected_key_id="key-1")
    assert verified["candidate_history_sha256"] == predicate["candidate_history_sha256"]
    receipt["predicate"]["candidate_history_sha256"] = "0" * 64
    with pytest.raises(attestation.AttestationError):
        attestation.verify_attestation(receipt, trusted_keys={"key-1": key},
            expected_predicate=expected, expected_key_id="key-1")


@pytest.mark.parametrize("mutation", ["wrong_pg_ip", "helper_label", "probe_timestamp",
                                     "missing_preflight", "test_exit", "cleanup"])
def test_full_modeled_flow_rejects_mismatches_and_never_signs(monkeypatch, tmp_path, mutation):
    result, predicate, model, key = _execute_modeled_gate(monkeypatch, tmp_path, mutation)
    assert result["attestation"] is None
    assert result["exit_code"] != 0
    assert not list(tmp_path.glob("run-*/attestation.json"))


def test_copied_environment_runs_unchanged_project_python_offline_with_empty_cache(tmp_path):
    import gate_container_entrypoint as entrypoint

    checkout = Path(gate.__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(checkout / name, workspace / name)
    for name in ("src", "scripts"):
        shutil.copytree(checkout / name, workspace / name, symlinks=True)
    template = tmp_path / "installed-template"
    shutil.copytree(checkout / ".venv", template, symlinks=True)
    (template / ".skybuild-environment.json").write_text(json.dumps({
        "schema": "skybuild.full-test.environment.v1", "uv_version": entrypoint.UV_VERSION,
        "pyproject_sha256": hashlib.sha256((workspace / "pyproject.toml").read_bytes()).hexdigest(),
        "uv_lock_sha256": hashlib.sha256((workspace / "uv.lock").read_bytes()).hexdigest()}))
    entrypoint._prepare_environment(workspace, template)
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "selected.py").write_text("import skybuild; print(skybuild.__file__)\n")
    home = tmp_path / "home"
    home.mkdir()
    cache = tmp_path / "empty-cache"
    environment = {"PATH": os.environ["PATH"], "HOME": str(home), "UV_OFFLINE": "1",
                   "UV_CACHE_DIR": str(cache), "UV_WORKING_DIR": str(tmp_path / "foreign")}
    result = subprocess.run([str(workspace / "scripts/project_python"), "selected.py"],
        cwd=caller, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(workspace / "src/skybuild/__init__.py")
    assert "Building" not in result.stderr and "Installed" not in result.stderr
    assert not list(cache.glob("**/hatchling*"))


def test_environment_manifest_mismatch_stops_before_copy_or_backend(tmp_path):
    import gate_container_entrypoint as entrypoint

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text('[project]\nname="skybuild"\nversion="0.1.0"\n')
    (workspace / "uv.lock").write_text("lock")
    template = tmp_path / "template"
    template.mkdir()
    (template / ".skybuild-environment.json").write_text("{}")
    with pytest.raises(RuntimeError, match="pinned project"):
        entrypoint._prepare_environment(workspace, template)
    assert not (workspace / ".venv").exists()
