import pytest
from types import SimpleNamespace

from skybuild.cpu_worker_bridge import CPUWorkerBridgeError, _controller_pin, _file_bytes


def test_empty_stdin_is_allowed_only_when_explicit(tmp_path):
    empty = tmp_path / "stdin.empty"
    empty.write_bytes(b"")
    empty.chmod(0o600)

    with pytest.raises(CPUWorkerBridgeError, match="bounded private regular file"):
        _file_bytes(empty, limit=1, private=True)

    assert _file_bytes(empty, limit=1, private=True, allow_empty=True) == b""


def test_controller_pin_rejects_a_different_worker_import_checkout(tmp_path):
    checkout = tmp_path / "different-checkout"
    checkout.mkdir()
    plan = SimpleNamespace(checkout=checkout)

    with pytest.raises(CPUWorkerBridgeError, match="exact controller source root"):
        _controller_pin(plan, "a" * 64)
