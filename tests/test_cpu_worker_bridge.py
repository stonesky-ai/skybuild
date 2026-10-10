import pytest

from skybuild.cpu_worker_bridge import CPUWorkerBridgeError, _file_bytes


def test_empty_stdin_is_allowed_only_when_explicit(tmp_path):
    empty = tmp_path / "stdin.empty"
    empty.write_bytes(b"")
    empty.chmod(0o600)

    with pytest.raises(CPUWorkerBridgeError, match="bounded private regular file"):
        _file_bytes(empty, limit=1, private=True)

    assert _file_bytes(empty, limit=1, private=True, allow_empty=True) == b""
