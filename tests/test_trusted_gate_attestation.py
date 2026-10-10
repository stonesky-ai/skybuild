from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import trusted_gate_attestation as gate


@pytest.fixture
def trusted(tmp_path):
    key = tmp_path / "publisher.hmac"
    key.write_bytes(b"trusted gate key material for test only 0123456789")
    key.chmod(0o600)
    keys = {"gate-v1": key}
    predicate = {
        "bundle_id": "bundle-" + hashlib.sha256(b"manifest").hexdigest()[:24],
        "pr_number": 41,
        "target_ref": "refs/heads/dev-006",
        "target_base": "a" * 40,
        "candidate_commit": "b" * 40,
        "candidate_tree": "c" * 40,
        "candidate_archive_sha256": "d" * 64,
        "gate_argv": ["uv", "run", "--extra", "test", "python", "-m", "pytest", "-q"],
        "gate_command_sha256": "e" * 64,
        "gate_policy_sha256": "f" * 64,
        "runner_identity": "trusted-runner-01",
        "runner_version": "1.2.3",
        "runner_image_id": "sha256:" + "1" * 64,
        "postgres_image_id": "sha256:" + "7" * 64,
        "firewall_image_id": "sha256:" + "9" * 64,
        "firewall_policy_sha256": "a" * 64,
        "trusted_entrypoint_sha256": "8" * 64,
        "network_probe_sha256": "b" * 64,
        "attestation_signer_sha256": "c" * 64,
        "execution_host": "gate-host-a",
        "source_mount_readonly": True,
        "scratch_mount_writable": True,
        "docker_socket_mounted": False,
        "host_home_mounted": False,
        "host_credentials_mounted": False,
        "credential_access": "synthetic_database_only",
        "network_mode": "internal",
        "egress_allowed": False,
        "firewall_defaults_drop": True,
        "firewall_ipv4_default_drop": True,
        "firewall_ipv6_default_drop": True,
        "firewall_policy_applied": True,
        "candidate_blocked_until_probe": True,
        "postgres_namespace_egress_blocked": True,
        "readonly_fixture_allowlist": ["/runner/entrypoint.py", "/runner/network_probe.py"],
        "mounts": [
            {"target": "/workspace/source", "mode": "ro", "kind": "bind",
             "source_class": "candidate_archive", "source": "sha256:" + "d" * 64},
            {"target": "/workspace/scratch", "mode": "rw", "kind": "tmpfs",
             "source_class": "scratch", "source": "tmpfs"},
            {"target": "/runner/entrypoint.py", "mode": "ro", "kind": "bind",
             "source_class": "trusted_runner_fixture", "source": "sha256:" + "8" * 64},
            {"target": "/runner/network_probe.py", "mode": "ro", "kind": "bind",
             "source_class": "trusted_runner_fixture", "source": "sha256:" + "b" * 64},
        ],
        "environment_allowlist": ["HOME", "PATH", "SKYBUILD_TEST_DSN"],
        "postgres_data_mount": {"type": "tmpfs", "source": "tmpfs", "size_bytes": 1024**3},
        "resource_limits": {"cpu_millis": 4000, "memory_bytes": 8 * 1024**3,
                            "pids": 2048, "timeout_seconds": 7200},
        "resources": {"runner_container_id": "2" * 64, "pg_container_id": "3" * 64,
                      "network_id": "4" * 64, "test_image_id": "sha256:" + "1" * 64,
                      "postgres_image_id": "sha256:" + "7" * 64,
                      "firewall_container_id": "5" * 64, "probe_container_id": "6" * 64,
                      "firewall_image_id": "sha256:" + "9" * 64,
                      "postgres_firewall_container_id": "a" * 64,
                      "postgres_probe_container_id": "b" * 64},
        "network_probe": {"postgres_tcp_allowed": True, "dns_blocked": True,
                          "external_ipv4_blocked": True, "external_ipv6_blocked": True,
                          "host_gateway_listener_blocked": True, "host_listener_port": 54001,
                          "log_sha256": "c" * 64},
        "postgres_network_probe": {"dns_blocked": True, "external_ipv4_blocked": True,
                                   "external_ipv6_blocked": True,
                                   "host_gateway_listener_blocked": True,
                                   "host_listener_port": 54001,
                                   "postgres_unix_socket_ready": True, "log_sha256": "d" * 64},
        "result": {"exit_code": 0, "log_sha256": "6" * 64,
                   "started_at": "2026-10-10T10:00:00Z", "finished_at": "2026-10-10T10:01:00Z",
                   "preflight": {"candidate_archive_readonly": True,
                                 "candidate_copied_to_scratch": True,
                                 "imported_package_path": "/scratch/workspace/src/skybuild/__init__.py",
                                 "host_gateway_probe": "blocked",
                                 "external_direct_ip_probe": "blocked",
                                 "external_dns_probe": "blocked"}},
        "cleanup": {"status": "confirmed", "owned_resources": [
            {"kind": "runner_container", "id": "2" * 64, "state": "absent"},
            {"kind": "postgres_container", "id": "3" * 64, "state": "absent"},
            {"kind": "network", "id": "4" * 64, "state": "absent"},
            {"kind": "firewall_container", "id": "5" * 64, "state": "absent"},
            {"kind": "network_probe_container", "id": "6" * 64, "state": "absent"},
            {"kind": "postgres_firewall_container", "id": "a" * 64, "state": "absent"},
            {"kind": "postgres_network_probe_container", "id": "b" * 64, "state": "absent"},
        ]},
    }
    expected = {key: predicate[key] for key in gate.PINNED_PREDICATE_FIELDS}
    receipt = gate.sign_attestation(predicate, key_path=key, key_id="gate-v1")
    return {"key": key, "keys": keys, "predicate": predicate,
            "expected": expected, "receipt": receipt}


def test_sign_and_verify_exact_trusted_gate_attestation(trusted):
    result = gate.verify_attestation(trusted["receipt"], trusted_keys=trusted["keys"],
                                     expected_predicate=trusted["expected"],
                                     expected_key_id="gate-v1")
    assert result == trusted["predicate"]
    assert len(gate.canonical_bytes(trusted["receipt"])) < gate.MAX_ATTESTATION_BYTES


def test_tampered_receipt_fails_signature_even_when_pins_match(trusted):
    receipt = deepcopy(trusted["receipt"])
    receipt["predicate"]["result"]["log_sha256"] = "7" * 64
    expected = dict(trusted["expected"])
    with pytest.raises(gate.AttestationError, match="signature verification failed"):
        gate.verify_attestation(receipt, trusted_keys=trusted["keys"],
                                expected_predicate=expected, expected_key_id="gate-v1")


def test_wrong_expected_candidate_or_archive_is_rejected(trusted):
    expected = dict(trusted["expected"], candidate_commit="9" * 40)
    with pytest.raises(gate.AttestationError, match="publication inputs"):
        gate.verify_attestation(trusted["receipt"], trusted_keys=trusted["keys"],
                                expected_predicate=expected, expected_key_id="gate-v1")


def test_key_substitution_and_unpinned_receipt_key_are_rejected(trusted, tmp_path):
    other = tmp_path / "other.hmac"
    other.write_bytes(b"different publisher secret that must not verify")
    other.chmod(0o600)
    with pytest.raises(gate.AttestationError, match="signature verification failed"):
        gate.verify_attestation(trusted["receipt"], trusted_keys={"gate-v1": other},
                                expected_predicate=trusted["expected"], expected_key_id="gate-v1")
    receipt = deepcopy(trusted["receipt"])
    receipt["signature"]["key_id"] = "attacker-choice"
    with pytest.raises(gate.AttestationError, match="signature metadata"):
        gate.verify_attestation(receipt, trusted_keys=trusted["keys"],
                                expected_predicate=trusted["expected"], expected_key_id="gate-v1")


@pytest.mark.parametrize("mutate, message", [
    (lambda p: p.update(unreviewed_field=True), "missing or unknown fields"),
    (lambda p: p.update(egress_allowed=True), "violates isolation policy"),
    (lambda p: p.update(docker_socket_mounted=True), "violates isolation policy"),
    (lambda p: p.update(host_credentials_mounted=True), "violates isolation policy"),
    (lambda p: p["mounts"].append({"target": "/host", "mode": "rw", "kind": "bind",
                                   "source_class": "other", "source": "/host"}), "unapproved host resource"),
    (lambda p: p["result"].update(exit_code=1), "did not pass"),
    (lambda p: p["cleanup"].update(owned_resources=[]), "did not account"),
])
def test_unsafe_or_unknown_runner_claims_cannot_be_signed(trusted, mutate, message):
    predicate = deepcopy(trusted["predicate"])
    mutate(predicate)
    with pytest.raises(gate.AttestationError, match=message):
        gate.sign_attestation(predicate, key_path=trusted["key"], key_id="gate-v1")


def test_key_must_be_private_mode_0600(trusted):
    trusted["key"].chmod(0o640)
    with pytest.raises(gate.AttestationError, match="mode-0600"):
        gate.verify_attestation(trusted["receipt"], trusted_keys=trusted["keys"],
                                expected_predicate=trusted["expected"], expected_key_id="gate-v1")


def test_unknown_envelope_fields_and_non_json_are_rejected(trusted):
    receipt = dict(trusted["receipt"], extra="surprise")
    with pytest.raises(gate.AttestationError, match="missing or unknown fields"):
        gate.verify_attestation(receipt, trusted_keys=trusted["keys"],
                                expected_predicate=trusted["expected"], expected_key_id="gate-v1")
    with pytest.raises(gate.AttestationError, match="canonical JSON"):
        gate.canonical_bytes({"bad": object()})
