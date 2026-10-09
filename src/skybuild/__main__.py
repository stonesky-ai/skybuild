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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skybuild")
    commands = parser.add_subparsers(dest="command", required=True)
    manifest = commands.add_parser("ledger-manifest", help="Emit a read-only ledger validation manifest without database access")
    manifest.add_argument("paths", nargs="+", type=Path)
    ledger_import = commands.add_parser("ledger-import", help="Plan or rehearse frozen ledger import; never switch live authority")
    ledger_import.add_argument("--ledger-dir", type=Path, default=Path("docs/design"))
    ledger_import.add_argument("--contract", type=Path, default=Path("docs/design/implementation/frozen_ledger_import.json"))
    ledger_import.add_argument("--project-id", default="skybuild")
    ledger_import.add_argument("--apply-disposable", action="store_true")
    ledger_import.add_argument("--expected-import-sha256")
    commands.add_parser("migrate", help="Apply migrations to the explicitly configured dedicated database")
    provision = commands.add_parser("provision", help="Provision a principal using SKYBUILD_TOKEN or --token-stdin")
    provision.add_argument("principal_id")
    provision.add_argument("--admin", action="store_true")
    provision.add_argument("--grant", action="append", default=[], metavar="PROJECT:OPERATION")
    provision.add_argument("--token-stdin", action="store_true")
    serve = commands.add_parser("serve", help="Serve an already migrated database on loopback")
    serve.add_argument("--port", type=int, default=8000)
    for command in ("tasks", "get", "history"):
        view = commands.add_parser(command, help="Read tasks through the authenticated API")
        view.add_argument("project_id")
        if command != "tasks":
            view.add_argument("task_id")
        if command != "get":
            view.add_argument("--limit", type=int, default=100)
            view.add_argument("--offset", type=int, default=0)
    due = commands.add_parser("reconcile-due", help="Run one bounded CPU-only pass over due deferrals through the API")
    due.add_argument("project_id")
    due.add_argument("--page-size", type=int, default=100)
    due.add_argument("--max-pages", type=int, default=20)
    due.add_argument("--after-task-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "ledger-manifest":
            from .ledger import build_manifest

            print(json.dumps(build_manifest(args.paths), ensure_ascii=False, indent=2))
            return 0
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
            with Client(_environment("SKYBUILD_API_URL"), _environment("SKYBUILD_TOKEN")) as client:
                if args.command == "get":
                    result = client.get_task(args.project_id, args.task_id)
                elif args.command == "history":
                    result = client.task_history(args.project_id, args.task_id, limit=args.limit, offset=args.offset)
                else:
                    result = client.list_tasks(args.project_id, limit=args.limit, offset=args.offset)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "reconcile-due":
            if not 1 <= args.page_size <= 100 or not 1 <= args.max_pages <= 100:
                raise ValueError("Reconciliation bounds are invalid")
            cursor, scanned, reassessed = args.after_task_id, 0, []
            with Client(_environment("SKYBUILD_API_URL"), _environment("SKYBUILD_TOKEN")) as client:
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

            uvicorn.run(create_app(store), host="127.0.0.1", port=args.port)
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
