"""Explicit trusted administration and read-only task views."""

import argparse
import json
import os
import sys
from pathlib import Path

from .client import Client, ClientError
from .contracts import DomainError


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Set {name}")
    return value


def _cord_payload(args: argparse.Namespace) -> dict:
    if args.body_file is not None:
        with args.body_file.open("r", encoding="utf-8") as stream:
            raw = stream.read(262_145)
    else:
        raw = sys.stdin.read(262_145)
    if len(raw.encode("utf-8")) > 262_144:
        raise ValueError("Cord payload is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Cord payload must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skybuild")
    parser.add_argument("--ca-file", type=Path, help="Trust only this installation CA for HTTPS API requests")
    commands = parser.add_subparsers(dest="command", required=True)
    manifest = commands.add_parser("ledger-manifest", help="Emit a read-only ledger validation manifest without database access")
    manifest.add_argument("paths", nargs="+", type=Path)
    ledger_import = commands.add_parser("ledger-import", help="Plan or rehearse frozen ledger import; never switch live authority")
    ledger_import.add_argument("--ledger-dir", type=Path, default=Path("docs/design"))
    ledger_import.add_argument("--contract", type=Path, default=Path("docs/design/implementation/frozen_ledger_import.json"))
    ledger_import.add_argument("--project-id", default="skybuild")
    ledger_import.add_argument("--apply-disposable", action="store_true")
    ledger_import.add_argument("--expected-import-sha256")
    audit = commands.add_parser("ledger-audit", help="Audit frozen versus current Markdown without database access or authority changes")
    audit.add_argument("--ledger-dir", type=Path, default=Path("docs/design"))
    audit.add_argument("--contract", type=Path, default=Path("docs/design/implementation/frozen_ledger_import.json"))
    for command in ("audit-runtime-role", "provision-runtime-role"):
        role_command = commands.add_parser(command, help="Inspect or qualify a pre-created restricted LOGIN role using SKYBUILD_ROLE_ADMIN_DSN")
        role_command.add_argument("role")
    commands.add_parser("migrate", help="Apply migrations to the explicitly configured dedicated database")
    provision = commands.add_parser("provision", help="Provision a principal using SKYBUILD_TOKEN or --token-stdin")
    provision.add_argument("principal_id")
    provision.add_argument("--admin", action="store_true")
    provision.add_argument("--grant", action="append", default=[], metavar="PROJECT:OPERATION")
    provision.add_argument("--token-stdin", action="store_true")
    serve = commands.add_parser("serve", help="Serve an already migrated database on loopback")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--ssl-certfile", type=Path)
    serve.add_argument("--ssl-keyfile", type=Path)
    serve.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1",
                       help="Bind inside a private container; keep the published host port on loopback")
    for command in ("tasks", "get", "history"):
        view = commands.add_parser(command, help="Read tasks through the authenticated API")
        view.add_argument("--ca-file", type=Path, default=argparse.SUPPRESS)
        view.add_argument("project_id")
        if command != "tasks":
            view.add_argument("task_id")
        if command != "get":
            view.add_argument("--limit", type=int, default=100)
            view.add_argument("--offset", type=int, default=0)
    inbox = commands.add_parser("cord-inbox", help="Read pending Cord messages for this credential")
    inbox.add_argument("--ca-file", type=Path, default=argparse.SUPPRESS)
    inbox.add_argument("project_id")
    inbox.add_argument("--limit", type=int, default=100)
    inbox.add_argument("--offset", type=int, default=0)
    inbox.add_argument("--wait-seconds", type=int, choices=range(26), default=0,
                       help="Wait once for a pending inbox page, up to 25 seconds; never execute messages")
    for command in ("cord-send", "cord-reply"):
        message = commands.add_parser(command, help="Send a Cord JSON message from a UTF-8 file or standard input")
        message.add_argument("--ca-file", type=Path, default=argparse.SUPPRESS)
        message.add_argument("project_id")
        if command == "cord-reply":
            message.add_argument("message_id")
        source = message.add_mutually_exclusive_group(required=True)
        source.add_argument("--body-file", type=Path)
        source.add_argument("--body-stdin", action="store_true")
        message.add_argument("--idempotency-key")
    for command in ("cord-receipt", "cord-handle"):
        action = commands.add_parser(command, help="Acknowledge a Cord message transition")
        action.add_argument("--ca-file", type=Path, default=argparse.SUPPRESS)
        action.add_argument("project_id")
        action.add_argument("message_id")
        action.add_argument("--idempotency-key")
    due = commands.add_parser("reconcile-due", help="Run one bounded CPU-only pass over due deferrals through the API")
    due.add_argument("--ca-file", type=Path, default=argparse.SUPPRESS)
    due.add_argument("project_id")
    due.add_argument("--page-size", type=int, default=100)
    due.add_argument("--max-pages", type=int, default=20)
    due.add_argument("--after-task-id")
    schedule = commands.add_parser("schedule-due", help="Run a finite CPU-only due-deferral catch-up timer")
    schedule.add_argument("--ca-file", type=Path, default=argparse.SUPPRESS)
    schedule.add_argument("project_id")
    schedule.add_argument("--interval-seconds", type=int, default=60)
    schedule.add_argument("--max-ticks", type=int, default=60)
    schedule.add_argument("--page-size", type=int, default=100)
    schedule.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args(argv)
    if args.command == "serve" and bool(args.ssl_certfile) != bool(args.ssl_keyfile):
        parser.error("--ssl-certfile and --ssl-keyfile must be supplied together")
    try:
        if args.command == "ledger-manifest":
            from .ledger import build_manifest

            print(json.dumps(build_manifest(args.paths), ensure_ascii=False, indent=2))
            return 0
        if args.command == "ledger-audit":
            from .ledger_audit import audit_ledgers

            report = audit_ledgers(args.ledger_dir, args.contract)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2 if report["stale_freeze"] else 0
        if args.command == "ledger-import":
            from .importer import import_frozen, prepare_import
            from .store import Store

            plan = prepare_import(args.ledger_dir, args.contract)
            if args.apply_disposable:
                if not args.expected_import_sha256:
                    raise ValueError("Apply requires --expected-import-sha256 from reviewed dry run")
                expected_database = _environment("SKYBUILD_EXPECTED_DATABASE")
                if not expected_database.startswith("skybuild_import_test"):
                    raise ValueError("Frozen import rehearsal requires a skybuild_import_test database")
                result = import_frozen(Store(_environment("SKYBUILD_DSN"), expected_database), args.project_id,
                                       args.ledger_dir, args.contract, args.expected_import_sha256)
                print(json.dumps({**result, "content_sha256": plan["content_sha256"]}, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        if args.command in {"tasks", "get", "history"}:
            with Client(_environment("SKYBUILD_API_URL"), _environment("SKYBUILD_TOKEN"), ca_file=args.ca_file) as client:
                if args.command == "get":
                    result = client.get_task(args.project_id, args.task_id)
                elif args.command == "history":
                    result = client.task_history(args.project_id, args.task_id, limit=args.limit, offset=args.offset)
                else:
                    result = client.list_tasks(args.project_id, limit=args.limit, offset=args.offset)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command.startswith("cord-"):
            with Client(_environment("SKYBUILD_API_URL"), _environment("SKYBUILD_TOKEN"), ca_file=args.ca_file) as client:
                if args.command == "cord-inbox":
                    result = client.inbox(args.project_id, limit=args.limit, offset=args.offset,
                                          **({"wait_seconds": args.wait_seconds} if args.wait_seconds else {}))
                elif args.command == "cord-send":
                    result = client.send_message(args.project_id, _cord_payload(args),
                                                 idempotency_key=args.idempotency_key)
                else:
                    action = args.command.removeprefix("cord-")
                    body = _cord_payload(args) if action == "reply" else None
                    result = client.message_action(args.project_id, args.message_id, action, body,
                                                   idempotency_key=args.idempotency_key)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "schedule-due":
            from .scheduler import schedule_due

            with Client(_environment("SKYBUILD_API_URL"), _environment("SKYBUILD_TOKEN"), ca_file=args.ca_file) as client:
                result = schedule_due(client, args.project_id, interval_seconds=args.interval_seconds,
                                      max_ticks=args.max_ticks, page_size=args.page_size, max_pages=args.max_pages,
                                      report=lambda tick: print(json.dumps(tick, ensure_ascii=False), flush=True))
            return 0 if result["complete"] else 1
        if args.command == "reconcile-due":
            if not 1 <= args.page_size <= 100 or not 1 <= args.max_pages <= 100:
                raise ValueError("Reconciliation bounds are invalid")
            cursor, scanned, reassessed = args.after_task_id, 0, []
            with Client(_environment("SKYBUILD_API_URL"), _environment("SKYBUILD_TOKEN"), ca_file=args.ca_file) as client:
                for _ in range(args.max_pages):
                    try:
                        result = client.reconcile_due_deferrals(args.project_id, limit=args.page_size, after_task_id=cursor)
                        count, changed, next_cursor = result["scanned"], result["reassessed"], result["next_after_task_id"]
                        if (type(count) is not int or count < 0 or count > args.page_size or
                                not isinstance(changed, list) or len(changed) > count or
                                any(not isinstance(task_id, str) for task_id in changed)):
                            raise ValueError("Invalid reconciliation response")
                        if next_cursor is not None and (not isinstance(next_cursor, str) or
                                                        not next_cursor or count != args.page_size or
                                                        next_cursor == cursor):
                            raise ValueError("Invalid reconciliation cursor")
                    except (ClientError, ValueError, KeyError, TypeError):
                        print(json.dumps({"scanned": scanned, "reassessed": reassessed,
                                          "complete": False, "next_after_task_id": cursor, "uncertain_page": True},
                                         ensure_ascii=False, indent=2))
                        print("Reconciliation page unconfirmed; retry from next_after_task_id", file=sys.stderr)
                        return 1
                    scanned += count
                    reassessed.extend(changed)
                    if next_cursor is None:
                        break
                    cursor = next_cursor
            print(json.dumps({"scanned": scanned, "reassessed": reassessed,
                              "complete": next_cursor is None, "next_after_task_id": next_cursor}, ensure_ascii=False, indent=2))
            return 0
        if args.command in ("audit-runtime-role", "provision-runtime-role"):
            import psycopg
            from .runtime_role import audit_runtime_role, provision_runtime_role

            action = audit_runtime_role if args.command == "audit-runtime-role" else provision_runtime_role
            with psycopg.connect(_environment("SKYBUILD_ROLE_ADMIN_DSN"), connect_timeout=5) as connection:
                connection.execute("SET LOCAL statement_timeout = '10s'")
                connection.execute("SET LOCAL lock_timeout = '5s'")
                result = action(connection, _environment("SKYBUILD_EXPECTED_DATABASE"), args.role)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        from .store import Store

        store = Store(_environment("SKYBUILD_DSN"), _environment("SKYBUILD_EXPECTED_DATABASE"))
        if args.command == "migrate":
            store.migrate()
            print("Migrations applied")
        elif args.command == "provision":
            grants: dict[str, set[str]] = {}
            for grant in args.grant:
                project, area, verb = grant.rsplit(":", 2)
                operation = f"{area}:{verb}"
                if not project or operation not in {"tasks:read", "tasks:write", "cord:read", "cord:send", "cord:handle"}:
                    raise ValueError("Use --grant PROJECT:OPERATION with a supported operation")
                grants.setdefault(project, set()).add(operation)
            token = sys.stdin.readline(4098).rstrip("\r\n") if args.token_stdin else _environment("SKYBUILD_TOKEN")
            if len(token) > 4096:
                raise ValueError("Token is too long")
            store.provision_principal(args.principal_id, token, is_admin=args.admin, grants=grants)
            print("Principal provisioned")
        else:
            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be 1–65535")
            from .api import create_app
            import uvicorn

            uvicorn.run(create_app(store), host=args.host, port=args.port,
                        **({"ssl_certfile": str(args.ssl_certfile), "ssl_keyfile": str(args.ssl_keyfile)}
                           if args.ssl_certfile else {}))
        return 0
    except (ValueError, DomainError, ClientError) as error:
        # Driver failures may contain connection strings; never print arbitrary exceptions.
        if isinstance(error, ClientError):
            print(f"Request failed ({error.code})", file=sys.stderr)
        else:
            print("Operation failed; verify configuration and supplied inputs", file=sys.stderr)
        return 1
    except Exception:
        print("Operation unavailable; verify the dedicated database and service", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
