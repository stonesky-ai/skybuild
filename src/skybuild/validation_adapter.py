"""Submit check, review and rebase evidence without starting external work.

The API verifies producer authority and current inputs in its transaction.
A failed result, including a required rebase, returns the task to Ready there.
This adapter never refreshes a revision or changes the result after a conflict.
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys

from .client import Client, ClientError
from .contracts import DomainError, valid_identifier
from .fleet_preflight import _resolved_addresses, _token_from_file
from .manual_dispatch import _private_endpoint
from .workflow import ResultState, ValidationResult


class ValidationAdapterError(ValueError):
    """A result cannot be submitted without its original input identity."""


def record_validation_result(client, result: ValidationResult, *, expected_revision: int,
                             idempotency_key: str) -> dict:
    """Send one immutable result through the guarded task mutation path.

    Use the original revision and operation ID for an uncertain retry. The
    authenticated API decides authority and freshness; this function grants none.
    All five validation stages use this same envelope. A needs_rebase result
    passes when the base needs no change and fails when corrective work is needed.
    """
    if not isinstance(result, ValidationResult):
        raise ValidationAdapterError("A typed validation result is required")
    if type(expected_revision) is not int or not 0 < expected_revision < 2**63:
        raise ValidationAdapterError("An exact positive task revision is required")
    if not valid_identifier(idempotency_key):
        raise ValidationAdapterError("A stable operation ID is required")
    for value in (result.source_head, result.target_base):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
            raise ValidationAdapterError("Full source head and target base IDs are required")
    if (not valid_identifier(result.attempt_id) or type(result.claim_fence) is not int
            or not 0 < result.claim_fence < 2**63):
        raise ValidationAdapterError("The original attempt and claim fence are required")
    if any(not value.strip() for value in
           (result.policy_version, result.producer, result.check_id, result.tool_version)):
        raise ValidationAdapterError("Policy, producer, check and tool versions are required")
    if result.state == ResultState.STALE:
        raise ValidationAdapterError("Stale results cannot update the current task")
    if result.state == ResultState.NOT_APPLICABLE and not (result.policy_reason and result.policy_reason.strip()):
        raise ValidationAdapterError("Not-applicable results require a policy reason")
    return client.workflow_transition(result.project_id, result.task_id, "validation_result",
                                     {"result": result.to_dict()},
                                     expected_revision=expected_revision,
                                     idempotency_key=idempotency_key)


def _read_result(path: Path) -> ValidationResult:
    """Read a bounded ordinary packet without following a replacement symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 16384:
            raise ValidationAdapterError("Result must be a bounded ordinary JSON file")
        raw = os.read(descriptor, 16385)
        if len(raw) > 16384:
            raise ValidationAdapterError("Result packet exceeds 16 KiB")
        return ValidationResult.from_dict(json.loads(raw))
    finally:
        os.close(descriptor)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = _read_result(args.result)
        endpoint = _private_endpoint(args.url, _resolved_addresses)
        with Client(endpoint, _token_from_file(args.token_file), retries=0, timeout=5,
                    trust_env=False, ca_file=args.ca_file) as client:
            record_validation_result(client, result, expected_revision=args.revision,
                                     idempotency_key=args.operation_id)
    except (ValidationAdapterError, DomainError, ClientError, OSError, ValueError, TypeError):
        # No remote body, credentials or result text enters local diagnostics.
        print("Validation submission failed. Preserve the result and operation ID.", file=sys.stderr)
        return 2
    print("Validation result recorded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
