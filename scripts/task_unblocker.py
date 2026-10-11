#!/usr/bin/env python3
"""Run one bounded TaskUnblocker sweep. Dry-run is the default."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
from _repo_guard import verify_skybuild
from skybuild.client import Client, ClientError
from skybuild.fleet_preflight import _token_from_file
from skybuild.manual_dispatch import _private_endpoint
from skybuild.task_unblocker import UnblockerError, run


class ReadOnlyClient(Client):
    def request(self, method, *args, **kwargs):
        if method.upper() != 'GET':
            raise UnblockerError('Dry-run permits GET only')
        return super().request(method, *args, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--expected-host', required=True)
    parser.add_argument('--api-url', required=True)
    parser.add_argument('--ca-file', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--principal', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--seconds', type=int, default=120, choices=range(10, 301))
    args = parser.parse_args(argv)
    try:
        if socket.gethostname().lower() != args.expected_host.lower():
            raise UnblockerError('Execution host differs from the explicit pin')
        if verify_skybuild(args.checkout).resolve() != ROOT:
            raise UnblockerError('Checkout differs from the executing source')
        def deadline(_signum, _frame):
            raise UnblockerError('Process deadline reached')
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(args.seconds + 5)
        _private_endpoint(args.api_url, lambda host: {row[4][0] for row in socket.getaddrinfo(host, None)})
        token = _token_from_file(args.token_file)
        with (Client if args.apply else ReadOnlyClient)(args.api_url, token, retries=0, timeout=5, ca_file=args.ca_file, trust_env=False) as client:
            report = run(client, args.project, args.state, apply=args.apply, expected_principal=args.principal,
                         api_url=args.api_url, seconds=args.seconds)
        # Never print task snapshots or credential-bearing transport errors.
        print(json.dumps({key: report[key] for key in ('schema', 'status', 'verified_at_utc', 'inventory_complete',
                         'scanned', 'checked', 'apply', 'models_invoked') if key in report} |
                         {'changed_findings': len(report.get('findings', [])), 'actions': report.get('actions', [])}, sort_keys=True))
        return 0
    except (UnblockerError, ClientError, OSError, ValueError) as error:
        print(json.dumps({'schema': 'skybuild.task-unblocker.v1', 'status': 'failed',
                          'error_type': type(error).__name__}), file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    raise SystemExit(main())
