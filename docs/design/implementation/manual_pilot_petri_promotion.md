# Controlled schema-012-to-013 Petri promotion

This is a preparation and operator runbook for a partial tranche of `SKYBUILD-MVP-CONTROLLER-UPDATE`. Repository integration does not authorize its live commands. The accepted schema-012 API remains the task authority until an explicitly authorized operator performs and verifies promotion. Existing tasks are not automatically enrolled by migration 013.

The older [011-to-012 procedure](manual_pilot_promotion.md) remains a historical record. This procedure adds one explicit migration contract to the same guarded controller path. It does not provide another executor, credential provisioner, authority importer, or automatic deployment service.

## Source preparation and isolated qualification

Freeze an independently reviewed candidate from a passed integration bundle at an exact published `refs/heads/dev-NNN` or `refs/heads/main` SHA. The selected ref must itself resolve to that SHA; publication on another ref is insufficient. `scripts/manual_pilot_controller.py --promotion --schema-transition 012-to-013` requires the exact unchanged 001–012 prefix and only `013_petri_workflow.sql`. Its default transition remains historical `011-to-012`.

The guard passes the explicit publication ref through the TLS controller check. Existing callers without that parameter retain their historical dev-002/main default. All retained source/package, database/container/image/cluster/CA, private environment, TLS/bind/mount/UID, role, resource, niceness and Serve/Funnel checks remain required. A passing preflight changes no service, schema, credential or grant.

Run the focused rehearsal from the exact owned checkout with the existing disposable PostgreSQL helper:

```sh
rtk proxy scripts/project_python scripts/disposable_pg_gate.py \
  --checkout "$PWD" --min-available-gib 6 -- \
  "$PWD/scripts/project_python" -m pytest -q \
  tests/test_manual_pilot_controller.py tests/test_manual_pilot_tls.py \
  tests/test_manual_pilot_promotion.py tests/test_petri_promotion.py \
  tests/test_runtime_role.py tests/test_petri_api_store.py \
  tests/test_petri_acceptance_store.py
```

The exact 012-to-013 rehearsal uses a unique disposable database/role, canonical migrations, representative task/journal/Cord snapshots, and a schema-012 fixture that models deployed source `e0cc07c2a6fa72e1aff1bcc7b3bf93bcd2a443e2`. It keeps the accepted fixture responsive during preparation, injects a failed role audit after DDL/version/grants, proves rollback, commits the candidate atomically, exercises strict old-schema refusal and failed candidate readiness, and recovers with the compatible candidate. It checks default new-task enrollment, conservative legacy enrollment and unchanged historical journal records.

The fixture is not the actual accepted container binary. This rehearsal does not qualify a captured live snapshot, external service responsiveness, an image build, operator recovery from storage loss, or managed workers. Rehearse the current captured task snapshot separately in isolated PostgreSQL before a live enrollment operation. Ambiguous records and legacy completion without matching current Petri evidence enter diagnostic Hold; preserve stable identities, old completion evidence and all journal events. Never replay effects or renew authority from a snapshot.

## Reviewable operator inputs

Use the stable deployment checkout named by the retained Compose ownership labels, not an author worktree. Pin the reviewed candidate source and published ref; current installed source; full API/database container IDs; immutable current and candidate image IDs; PostgreSQL system identifier; approved private hostname/IP; CA PEM digest; and private state directory. Independently reconcile any mismatch with retained deployment receipts. Do not adopt whatever currently owns a name, tag or port.

Require a fresh clear host-watch sample, the owner's 8 GiB reserve, at least 10 GiB available before preparation/build, sufficient disk, and nice level 10. The 6 GiB disposable-test exception does not lower runtime preparation requirements. Qualify a memory-bounded builder separately: niceness on a Docker CLI does not constrain daemon build workers. Build only after that qualification and runtime preparation authority. Preserve the accepted image/tag and verify every candidate package file against the exact approved source manifest using the existing bounded isolated image-probe procedure in the historical runbook.

Stop new dispatch and reconcile pending assignments/results before maintenance. Keep credentials, runtime environment, TLS, PostgreSQL storage, project authority, stops and reservations unchanged. Preserve one writable authority. Never rerun initial provisioning or the completed task-authority import.

Set these non-secret operator inputs from retained evidence: `SKYBUILD_PILOT_STATE`, `CONTROLLER_HOST`, `SKYBUILD_PILOT_TAILNET_IP`, `APPROVED_SHA`, `APPROVED_REF`, `CURRENT_SHA`, `CURRENT_API_CONTAINER`, `CURRENT_DB_CONTAINER`, `CURRENT_API_IMAGE`, `CURRENT_DB_SYSTEM_ID`, and `CURRENT_CA_PEM_SHA256`. `SKYBUILD_PROMOTION_IMAGE_ID` must name the separately verified immutable candidate image. Keep new evidence in an owned mode-0700 directory outside Git; use exclusive new filenames and `umask 077`.

Create a fresh private identity-bound backup using the existing helper and retain its returned dump/evidence digests. Older backups predate current task mutations and are not current recovery evidence. A dump alone is not a tested restore guarantee.

```sh
rtk proxy nice -n 10 scripts/project_python scripts/create_pilot_backup.py \
  --checkout "$PWD" --container-id "$CURRENT_DB_CONTAINER" \
  --expected-system-identifier "$CURRENT_DB_SYSTEM_ID" \
  --backup-file "$PRIVATE_DUMP" --evidence-file "$PRIVATE_BACKUP_EVIDENCE"

rtk proxy nice -n 10 scripts/project_python scripts/manual_pilot_controller.py \
  --promotion --schema-transition 012-to-013 --checkout "$PWD" \
  --expected-sha "$APPROVED_SHA" --published-ref "$APPROVED_REF" \
  --current-sha "$CURRENT_SHA" --state-dir "$SKYBUILD_PILOT_STATE" \
  --hostname "$CONTROLLER_HOST" --tailnet-ip "$SKYBUILD_PILOT_TAILNET_IP" \
  --api-container-id "$CURRENT_API_CONTAINER" --db-container-id "$CURRENT_DB_CONTAINER" \
  --api-image-id "$CURRENT_API_IMAGE" --database-system-id "$CURRENT_DB_SYSTEM_ID" \
  --ca-pem-sha256 "$CURRENT_CA_PEM_SHA256"
```

Immediately before stopping the API, repeat the full preflight and save its successful, unfiltered JSON in a new mode-0600 `SKYBUILD_PROMOTION_REPORT` file. Confirm `schema_transition=012-to-013`, `current_schema=12`, `candidate_schema=13`, the exact approved ref/source, and every retained identity. Capture command exit status; a printed failure object is not readiness. The report must be less than two minutes old when the transaction begins. Confirm the private backup and its evidence file still match the retained SHA-256/byte-count/database/system-identifier manifest.

## Authorized atomic migration

After explicit runtime-promotion authority and the verified preparation, stop only the retained API container. Leave PostgreSQL and storage running.

```sh
rtk proxy nice -n 10 docker stop --time 30 "$CURRENT_API_CONTAINER"
```

The operator transaction below checks stopped runtime identity, fresh source/TLS evidence, exact migration prefix and absence of runtime-role sessions. DDL, version digest and runtime-role qualification commit together. Execute through the stable checkout's project runner. Do not substitute independently committing migrate/grant CLI commands.

```python
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
import manual_pilot_controller as controller
import manual_pilot_provision as provisioner
import manual_pilot_tls as tls
from skybuild.runtime_role import audit_runtime_role, provision_runtime_role

root = Path.cwd()
state = Path(os.environ["SKYBUILD_PILOT_STATE"])
report_path = Path(os.environ["SKYBUILD_PROMOTION_REPORT"])
tls.private_file(report_path)
report = json.loads(report_path.read_text())
assert report["ready_for_operator_promotion"] and report["no_changes_made"]
assert (report["current_schema"], report["candidate_schema"], report["schema_transition"]) == (12, 13, "012-to-013")
assert report["candidate_source"] == os.environ["APPROVED_SHA"]
assert report["published_ref"] == os.environ["APPROVED_REF"]
checked = dt.datetime.fromisoformat(report["tls"]["checked_at"])
assert dt.timedelta(0) <= dt.datetime.now(dt.timezone.utc) - checked < dt.timedelta(minutes=2)
assert tls.command("git", "-C", str(root), "rev-parse", "HEAD") == report["candidate_source"]
assert not tls.command("git", "-C", str(root), "status", "--porcelain", "--untracked-files=all")
assert tls.command("git", "-C", str(root), "ls-remote", "--exit-code", "origin", report["published_ref"]) == (
    report["candidate_source"] + "\t" + report["published_ref"])
api = json.loads(tls.command("docker", "inspect", report["api_container"]))[0]
assert api["Id"] == report["api_container"] and not api["State"]["Running"]
assert api["Image"] == report["api_image"]
db = json.loads(tls.command("docker", "inspect", report["database_container"]))[0]
assert db["Id"] == report["database_container"] and db["State"]["Running"]
assert provisioner._dedicated_container(state) == report["database_system_id"]
assert hashlib.sha256((state / "tls/ca.crt").read_bytes()).hexdigest() == report["ca_pem_sha256"]
old = controller._source_manifest(root, report["current_source"])
candidate = controller._source_manifest(root, report["candidate_source"])
assert controller._verify_schema_transition(old, candidate, "012-to-013") == (12, 13)
expected = sorted((int(Path(name).name.split("_", 1)[0]), digest)
                  for name, digest in old.items() if name.startswith("migrations/"))
migration = root / "src/skybuild/migrations/013_petri_workflow.sql"
digest = hashlib.sha256(migration.read_bytes()).hexdigest()
assert digest == candidate["migrations/013_petri_workflow.sql"]
password = provisioner._read_secret(state / "secrets/admin-password", mode=0o644)
dsn = make_conninfo(provisioner._dsn(password, "postgres", host="127.0.0.1", port=55432),
                    dbname=provisioner.DATABASE)
with psycopg.connect(dsn, connect_timeout=5) as connection:
    connection.execute("SET LOCAL statement_timeout = '30s'")
    connection.execute("SET LOCAL lock_timeout = '5s'")
    connection.execute("SET LOCAL idle_in_transaction_session_timeout = '60s'")
    connection.execute("SET LOCAL search_path TO skybuild, pg_catalog")
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('skybuild:migrate', 0))")
    provisioner._require_same_cluster(connection, report["database_system_id"])
    assert connection.execute("SELECT current_database()").fetchone()[0] == provisioner.DATABASE
    assert connection.execute("SELECT version, digest FROM schema_migrations ORDER BY version").fetchall() == expected
    assert connection.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND usename = %s",
                              (provisioner.DATABASE, provisioner.ROLE)).fetchone()[0] == 0
    connection.execute(migration.read_text())
    connection.execute("INSERT INTO schema_migrations VALUES (13, %s)", (digest,))
    assert provision_runtime_role(connection, provisioner.DATABASE, provisioner.ROLE)["ok"]
    assert audit_runtime_role(connection, provisioner.DATABASE, provisioner.ROLE)["ok"]
print(json.dumps({"migration_committed": True, "schema_version": 13, "runtime_role_audit": "passed"}))
```

Keep errors private and sanitize operator-facing failures; driver tracebacks can contain sensitive details. A lost commit acknowledgment is unconfirmed. Inspect exact schema digests and role state before deciding what committed. If the transaction definitely rolled back and schema 012 plus its role policy remain verified, the retained old API can recover with unchanged TLS. Never blindly rerun the transaction.

## Candidate startup and verification

After confirmed schema-013 commit and full role qualification, use the existing guarded Python replacement block under [Start the pinned candidate and requalify](manual_pilot_promotion.md#start-the-pinned-candidate-and-requalify). The block is schema-independent and consumes the same retained container/image/source report and verified `SKYBUILD_PROMOTION_IMAGE_ID`. Execute it through `scripts/project_python` at nice level 10. It rechecks exact stopped/API/database/network ownership, removes only the retained stopped API ID, creates with an exclusive name, and starts only the returned full candidate ID. A foreign replacement or collision stops the procedure. Do not run the historical migration or authority-cutover blocks.

Verify pinned-CA HTTP readiness, exact installed candidate package/image/container identity, unchanged runtime environment, private binds/mounts/UID/resources, PostgreSQL system identifier, full applied migration digests through 013, and restricted-role audit. HTTP readiness is only `{"status":"ready"}`; obtain schema evidence separately. Verify authenticated `/api/v1/me` and a declared Petri workflow read using protected token files; preserve existing task/journal/Cord records. Record source/image/container/cluster IDs, role/TLS evidence, checks and unresolved limits in a private actionable deployment record.

Keep workers parked while reconciling existing tasks. Legacy four-grant worker relay credentials do not gain claim/transition capability through this schema update. Any later six-grant worker qualification or credential expansion needs its own authority and evidence. After the runtime and worker path are qualified, run the owner-selected README task through actual author, validation, review, bundle publication and confirmed dev inclusion.

## Recovery boundary

The old controller requires exactly schema 012. After confirmed 013 commit, restarting the old image is not a qualified rollback. Candidate failure requires evidence preservation and reviewed forward repair, or separately authorized isolated recovery with loss/effect reconciliation. Do not delete migration rows/objects, disable constraints, restore over current writes, replace credentials or renew authority to force readiness. A passing source rehearsal and an old dump are not production disaster recovery proof.
