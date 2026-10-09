"""Explicitly arm one bounded CPU-only receive; never launch assignment work."""

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import time

from skybuild.client import Client, ClientError
from skybuild.contracts import valid_identifier
from skybuild.fleet_preflight import probe_private_api, _token_from_file
from skybuild.manual_assignment import _git
from skybuild.manual_cord import receive_assignment
from skybuild.manual_dispatch import _state_directory

class ListenerError(ValueError):
    pass


def listen(client, *, project, worker, dispatcher, assignment_id, checkout, base_sha,
           destination, state_dir, brief_path, brief_sha256, deadline, duration=600, clock=time.time,
           monotonic=time.monotonic, sleep=time.sleep):
    """Save/receipt one expected committed assignment, then exit without handling."""
    if (any(not valid_identifier(value) for value in (project, worker, dispatcher, assignment_id))
            or not isinstance(deadline, (int, float)) or not clock() < deadline < float("inf")
            or type(duration) not in (int, float) or not 0 < duration <= 600):
        raise ListenerError("Invalid arming bounds")
    end = monotonic() + duration
    checkout = checkout.resolve()
    if (not re.fullmatch(r"[0-9a-f]{40}", base_sha)
            or not re.fullmatch(r"[0-9a-f]{64}", brief_sha256)
            or not destination.is_absolute() or checkout in destination.parents):
        raise ListenerError("Exact pins and an external destination are required")
    def check_checkout():
        if (_git(checkout, "rev-parse", "--show-toplevel").decode().strip() != str(checkout)
                or _git(checkout, "rev-parse", "HEAD").decode().strip() != base_sha
                or _git(checkout, "status", "--porcelain", "--untracked-files=all").strip()):
            raise ListenerError("Listener needs the clean pinned checkout")
    check_checkout()
    state_dir = _state_directory(state_dir, checkout)
    slot = hashlib.sha256(f"{project}\0{worker}".encode()).hexdigest()
    descriptor = os.open(state_dir / f"listener-{slot}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)

    def remaining():
        return min(deadline - clock(), end - monotonic())

    class Snapshot:
        def inbox(self, *_args, **_kwargs):
            return [selected]

        def message_action(self, *args, **kwargs):
            check_checkout()
            if remaining() <= 0:
                raise ListenerError("Deadline reached before receipt; preserve snapshot")
            return client.message_action(*args, **kwargs)

    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ListenerError("This worker listener is already armed") from None
        while remaining() > 0:
            # Leave five seconds for the client's wait transport margin.
            wait = min(25, max(0, int(remaining() - 5)))
            messages = client.inbox(project, limit=100, offset=0, wait_seconds=wait)
            if remaining() <= 0:
                break
            if not isinstance(messages, list) or len(messages) > 100:
                raise ListenerError("Invalid bounded inbox response")
            matches = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                body = message.get("body")
                if not isinstance(body, str) or len(body.encode()) > 32768:
                    continue
                try:
                    envelope = json.loads(body)
                except ValueError:
                    continue
                if isinstance(envelope, dict) and envelope.get("assignment_id") == assignment_id:
                    matches.append((message, envelope))
            if matches:
                if len(matches) != 1:
                    raise ListenerError("Expected exactly one assignment delivery")
                selected, envelope = matches[0]
                if not valid_identifier(selected.get("message_id")):
                    raise ListenerError("Invalid message identifier")
                if (envelope.get("base_sha") != base_sha or envelope.get("brief_path") != brief_path
                        or envelope.get("brief_sha256") != brief_sha256):
                    raise ListenerError("Assignment differs from pinned checkout")
                result = receive_assignment(Snapshot(), project, checkout, worker=worker,
                                            dispatcher=dispatcher, message_id=selected.get("message_id"),
                                            destination=destination)
                return {"status": "received", "assignment_id": assignment_id,
                        "message_id": result["message_id"]}
            # Receipt does not remove unrelated messages. Never busy-spin on them.
            sleep(min(1, max(0, remaining())))
        return {"status": "timeout", "assignment_id": assignment_id}
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("url", "project", "worker", "dispatcher", "assignment-id", "base-sha", "brief-path", "brief-sha256", "approved-until"):
        parser.add_argument("--" + name, required=True)
    for name in ("token-file", "ca-file", "checkout", "destination", "state-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--duration", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        parsed = datetime.fromisoformat(args.approved_until.replace("Z", "+00:00"))
        if (parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0
                or not time.time() < parsed.timestamp()):
            raise ListenerError("Invalid owner deadline")
        if not 1 <= args.duration <= 600:
            raise ListenerError("Invalid duration")
        stop_at = time.monotonic() + args.duration
        probe_private_api(args.url, args.project, args.token_file, args.worker, ca_file=args.ca_file)
        with Client(args.url, _token_from_file(args.token_file), retries=0, timeout=5,
                    trust_env=False, ca_file=args.ca_file) as client:
            result = listen(client, project=args.project, worker=args.worker, dispatcher=args.dispatcher,
                            assignment_id=args.assignment_id, checkout=args.checkout, base_sha=args.base_sha,
                            destination=args.destination, state_dir=args.state_dir,
                            brief_path=args.brief_path, brief_sha256=args.brief_sha256,
                            deadline=parsed.timestamp(), duration=max(0, stop_at - time.monotonic()))
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "received" else 2
    except (ValueError, OSError, ClientError):
        print(json.dumps({"status": "blocked", "reason": "Listener failed; preserve local evidence"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
