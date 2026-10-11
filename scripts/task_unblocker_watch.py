#!/usr/bin/env python3
"""Run the pinned TaskUnblocker once every two hours without a model."""
import argparse
import json
import os
import signal
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / 'src')]
from skybuild.task_unblocker import read_json, UnblockerError


def command(config):
    required = {'checkout', 'source_head', 'expected_host', 'api_url', 'ca_file', 'token_file',
                'principal', 'project', 'state', 'apply'}
    if set(config) != required or type(config['apply']) is not bool:
        raise UnblockerError('Invalid watch configuration')
    if Path(config['checkout']).resolve() != ROOT or socket.gethostname() != config['expected_host']:
        raise UnblockerError('Watch host or checkout pin differs')
    head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(ROOT), 'status', '--porcelain'], check=True,
                           capture_output=True, text=True, timeout=10).stdout
    if head != config['source_head'] or dirty:
        raise UnblockerError('Watch source is not the exact clean commit')
    args = [str(ROOT / 'scripts/project_python'), str(ROOT / 'scripts/task_unblocker.py')]
    for name in ('checkout', 'expected_host', 'api_url', 'ca_file', 'token_file', 'principal', 'project', 'state'):
        args += ['--' + name.replace('_', '-'), config[name]]
    if config['apply']:
        args.append('--apply')
    return args


def bounded_run(args, seconds=150):
    child = subprocess.Popen(args, start_new_session=True)
    try:
        return child.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        # Only this sweep owns this new process group. Never leave a late child running.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass
        # Keep the leader unreaped until both signals are sent. Its process-group
        # identity cannot be reused while that child PID still belongs to us.
        child.wait(timeout=5)
        return 1


def sweep(config):
    try:
        return bounded_run(command(config))
    except (OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({'schema': 'skybuild.task-unblocker-watch.v1', 'status': 'sweep_failed'}), flush=True)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    # Reload the private configuration only on process start. A config change requires restart.
    config = read_json(args.config)
    while True:
        started = time.monotonic()
        result = sweep(config)
        if args.once:
            return result
        # Failed sweeps receive the same interval. Never retry a POST here.
        time.sleep(max(0, 7200 - (time.monotonic() - started)))


if __name__ == '__main__':
    raise SystemExit(main())
