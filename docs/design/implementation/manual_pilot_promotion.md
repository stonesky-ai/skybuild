# Controlled manual pilot promotion from schema 011 to 012

Task: `SKYBUILD-SELF-BUILD-MVP`, with `SKYBUILD-EXECUTION-CONTROLS` inspection support. Owner: main SkyBuild session. This is the retained runbook for the one-time schema-011-to-012 promotion and subsequent task cutover. The 2026-10-09 pilot verification found the promoted API ready on schema 12, and the API-authority receipt records the completed import. The API serves 60 tasks: 38 imported from the pinned source and 22 created later. The steps below describe the historical promotion sequence; do not repeat them against the current API-owned project. For that sequence, the deployed source/image identity had to come from retained deployment evidence and the read-only preflight, not the stale schema-010 source hash. The prerequisite was an exact schema-011 runtime and reviewed candidate containing only migration 012.

The schema promotion step preserved the dedicated database, credentials, application CA/leaf, non-root API UID, private binds and manual Cord assignments. That step did not itself change ledger authority or worker/model admission. Live promotion required explicit runtime-promotion authority. Do not use root, sudo, self-SSH, Docker host mounts, changed privileges, or another database to bypass a failed prerequisite.

## Read-only preparation

Use the stable deployment checkout named by the running containers' Compose working-directory labels. A candidate worktree has a different ownership path. Require a clean checkout at the exact published candidate, the retained current image/source association, full API/database container IDs and PostgreSQL system identifier. Reconcile missing or mismatched retained identities independently; do not replace an expected identity with whichever process owns a name or port.

Keep the existing TLS environment and private state. Check a fresh host-watch sample, preserve the owner's 8 GiB reserve, and require 10 GiB available before preparation/build. Run at nice level 10. Niceness of a Docker CLI does not itself constrain daemon build workers; separately qualify builder memory resources before building. Do not change daemon privileges/configuration automatically.

```sh
export SKYBUILD_PILOT_STATE='/absolute/existing/private/pilot-state'
export SKYBUILD_PILOT_TAILNET_IP='REPLACE_WITH_APPROVED_CURRENT_TAILSCALE_IPV4'
export SKYBUILD_PILOT_TLS_UID="$(id -u)"
export APPROVED_SHA='REPLACE_WITH_REVIEWED_PUBLISHED_40_HEX_SHA'
export PYTHONPATH="$PWD/src"
CURRENT_SHA='REPLACE_WITH_RETAINED_FULL_40_HEX_SOURCE_SHA'
CURRENT_API_CONTAINER='REPLACE_WITH_RETAINED_FULL_64_HEX_CONTAINER_ID'
CURRENT_DB_CONTAINER='REPLACE_WITH_RETAINED_FULL_64_HEX_CONTAINER_ID'
CURRENT_API_IMAGE='REPLACE_WITH_RETAINED_sha256_IMAGE_ID'
CURRENT_DB_SYSTEM_ID='REPLACE_WITH_RETAINED_POSTGRESQL_SYSTEM_IDENTIFIER'
CURRENT_CA_PEM_SHA256='REPLACE_WITH_RETAINED_64_HEX_CA_PEM_FILE_DIGEST'
CONTROLLER_HOST='REPLACE_WITH_APPROVED_CURRENT_SELF_DNS_NAME'
nice -n 10 ./.venv/bin/python scripts/manual_pilot_controller.py --promotion --checkout "$PWD" --expected-sha "$APPROVED_SHA" --published-ref refs/heads/dev-002 --current-sha "$CURRENT_SHA" --state-dir "$SKYBUILD_PILOT_STATE" --hostname "$CONTROLLER_HOST" --tailnet-ip "$SKYBUILD_PILOT_TAILNET_IP" --api-container-id "$CURRENT_API_CONTAINER" --db-container-id "$CURRENT_DB_CONTAINER" --api-image-id "$CURRENT_API_IMAGE" --database-system-id "$CURRENT_DB_SYSTEM_ID" --ca-pem-sha256 "$CURRENT_CA_PEM_SHA256"
```

The guard checks published clean source/ancestry, installed package hashes against retained source, exact retained container/image/cluster identities, unchanged migration digests 001–011 and only authority migration 012, private runtime environment, TLS/CA/readiness, binds/mounts/UID, resources, empty Serve/Funnel, niceness and headroom. It checks the running API against the retained immutable API image ID; the historical `:local` tag can name an older image. PostgreSQL reads use a bounded repeatable-read/read-only administrator transaction. The current restricted-role audit must pass with no findings; the candidate role audit must pass after migration 012. A failure changes no services or database state. Remote worker reachability remains a separate qualification.

## Isolated schema rehearsal

This is a partial implementation tranche for `SKYBUILD-MVP-CONTROLLER-UPDATE`, adapted from source commit `c605f5dd46769e2622ad007402b1f75564e83bcd` onto the schema-012 controller. The task remains incomplete; its resolver must still qualify candidate construction from an accepted published bundle, pinned controller identities, external responsiveness, one writable authority and an actionable deployment journal record in an isolated runtime. No live task state changes are part of this source tranche.

Run the focused rehearsal through `scripts/disposable_pg_gate.py` with a disposable PostgreSQL image. The test creates a unique database and runtime role, applies only migrations 001–010, and drops both resources in `finally`. It seeds one task journal and one Cord message, keeps a schema-010 API test server responsive while it checks the candidate migration digest, then injects a failed atomic migration qualification and verifies the old API and records remain available. It next applies migrations 011–012 from the pinned candidate, confirms the old server refuses the incompatible schema, injects candidate readiness failure, and starts a compatible candidate against the same database to verify retained task and Cord records.

This is an in-process rehearsal, not a container image build or published-bundle promotion. Candidate preparation is represented by hashing the migration source; readiness failure is injected. It demonstrates transaction rollback before the schema commit and forward candidate recovery after the schema commit. It does not qualify production recovery, candidate binary provenance, external service responsiveness, a journaled deployment event, remote worker connectivity, or operator rollback. Do not treat it as deployment acceptance.

```sh
./.venv/bin/python scripts/disposable_pg_gate.py --checkout "$PWD" --min-available-gib 10 -- ./.venv/bin/python -m pytest -q tests/test_manual_pilot_promotion.py::test_schema_010_role_audit_and_atomic_candidate_requalification
```

## Later build and recovery evidence

Do not execute this section during source preparation. After explicit authority, stop new manual dispatch and reconcile pending assignments/results without renewing authority. Retain allowlisted source/image/container/TLS/role/message evidence privately. Never dump full Docker environments or credentials. Build at nice level 10 with a separately qualified bounded builder, using a separate source tag. Do not overwrite the accepted `:local` image tag; the preflight verifies that it still names the old image.

```sh
nice -n 10 docker build --pull=false -f ops/manual-pilot/Dockerfile -t "skybuild-pilot-api:$APPROVED_SHA" .
export SKYBUILD_PROMOTION_IMAGE_ID="$(docker image inspect "skybuild-pilot-api:$APPROVED_SHA" --format '{{.Id}}')"
nice -n 10 ./.venv/bin/python - <<'PY'
import json, os, pathlib, re, sys
sys.path.insert(0, str(pathlib.Path.cwd() / 'scripts'))
import manual_pilot_controller as controller
import manual_pilot_tls as tls
image = os.environ['SKYBUILD_PROMOTION_IMAGE_ID']
assert re.fullmatch(r'sha256:[0-9a-f]{64}', image)
probe = ("import hashlib,json,pathlib,skybuild; p=pathlib.Path(skybuild.__file__).parent; "
         "files=sorted(f for f in p.rglob('*') if f.is_file() and not (f.parent.name=='__pycache__' and f.suffix=='.pyc')); "
         "assert len(files)<=200 and all(f.stat().st_size<=1048576 for f in files); "
         "print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}))")
actual = json.loads(tls.command('docker', 'run', '--rm', '--network', 'none', '--read-only',
    '--memory', '256m', '--memory-swap', '256m', '--cpus', '1', '--pids-limit', '32',
    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'python', image, '-c', probe))
assert actual == controller._source_manifest(pathlib.Path.cwd(), os.environ['APPROVED_SHA'])
print(json.dumps({'candidate_image': image, 'package_matches_reviewed_source': True}))
PY
```

The verification container has no network, credentials, mounts, writable root, capabilities, service listener or model invocation. Use an existing private mode-0700 evidence directory outside Git and `umask 077` for new files. Create a new custom-format dump and private source-evidence manifest through the retained full PostgreSQL container ID; the helper checks the container labels, database name and system identifier before running `pg_dump`, then binds the evidence file to the dump's path, SHA-256 and byte count:

```sh
nice -n 10 ./.venv/bin/python scripts/create_pilot_backup.py --checkout "$PWD" --container-id "$CURRENT_DB_CONTAINER" --expected-system-identifier "$CURRENT_DB_SYSTEM_ID" --backup-file "$PRIVATE_DUMP" --evidence-file "$PRIVATE_BACKUP_EVIDENCE"
```

Record the returned dump and evidence digests privately. The live cutover requires both digests, the evidence file, and the same retained system identifier; it independently checks the archive database name and live cluster identity. No password belongs in arguments. The dump has not undergone a restore drill and is not an automatic rollback guarantee; never overwrite it.

Keep the same base and TLS configuration and recheck the pinned image ID before use. Rerun the full preflight immediately before the stop and save its successful JSON in a new mode-0600 `$SKYBUILD_PROMOTION_REPORT` file. It contains no secrets. Parent-directory ownership and private modes remain operator prerequisites. The later replacement uses an exclusive-name Docker creation primitive, not Compose reconciliation, so a foreign replacement is never adopted or removed.

## Stop only the API and migrate atomically

After authority, stop only `api`; leave PostgreSQL and storage running. Retain the stopped API identity/config and old image. Never rerun initial provisioning, replace credentials, import task authority, run `down`, or remove volumes/state.

```sh
nice -n 10 docker stop --time 30 "$CURRENT_API_CONTAINER"
```

This explicit operator transaction checks the stopped container, cluster/database, exact 011 prefix, clean reviewed source and absence of runtime-role sessions. DDL, version digest, runtime grants and full candidate audit commit together. Do not substitute separate `migrate` and `provision-runtime-role` CLI calls; those commit independently.

```sh
nice -n 10 ./.venv/bin/python - <<'PY'
import datetime as dt, hashlib, json, os, pathlib, sys
import psycopg
from psycopg.conninfo import make_conninfo
sys.path.insert(0, str(pathlib.Path.cwd() / 'scripts'))
import manual_pilot_controller as controller
import manual_pilot_provision as provisioner
import manual_pilot_tls as tls
from skybuild.runtime_role import audit_runtime_role, provision_runtime_role
try:
    root = pathlib.Path.cwd()
    state = pathlib.Path(os.environ['SKYBUILD_PILOT_STATE'])
    report_path = pathlib.Path(os.environ['SKYBUILD_PROMOTION_REPORT'])
    tls.private_file(report_path)
    report = json.loads(report_path.read_text())
    assert report['ready_for_operator_promotion'] and report['no_changes_made']
    assert report['candidate_source'] == os.environ['APPROVED_SHA']
    checked = dt.datetime.fromisoformat(report['tls']['checked_at'])
    assert dt.timedelta(0) <= dt.datetime.now(dt.timezone.utc) - checked < dt.timedelta(minutes=2)
    assert tls.command('git', '-C', str(root), 'rev-parse', 'HEAD') == report['candidate_source']
    assert not tls.command('git', '-C', str(root), 'status', '--porcelain', '--untracked-files=all')
    api = json.loads(tls.command('docker', 'inspect', 'skybuild-pilot-api'))[0]
    assert api['Id'] == report['api_container'] and not api['State']['Running']
    assert api['Image'] == report['api_image']
    db = json.loads(tls.command('docker', 'inspect', 'skybuild-pilot-pg'))[0]
    assert db['Id'] == report['database_container']
    assert provisioner._dedicated_container(state) == report['database_system_id']
    old = controller._source_manifest(root, report['current_source'])
    expected = sorted((int(pathlib.Path(name).name.split('_', 1)[0]), digest)
                      for name, digest in old.items() if name.startswith('migrations/'))
    migration = root / 'src/skybuild/migrations/012_api_task_authority.sql'
    digest = hashlib.sha256(migration.read_bytes()).hexdigest()
    candidate = controller._source_manifest(root, report['candidate_source'])
    assert digest == candidate['migrations/012_api_task_authority.sql']
    password = provisioner._read_secret(state / 'secrets/admin-password', mode=0o644)
    dsn = make_conninfo(provisioner._dsn(password, 'postgres', host='127.0.0.1', port=55432), dbname=provisioner.DATABASE)
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        connection.execute("SET LOCAL statement_timeout = '30s'")
        connection.execute("SET LOCAL lock_timeout = '5s'")
        connection.execute("SET LOCAL idle_in_transaction_session_timeout = '60s'")
        connection.execute('SET LOCAL search_path TO skybuild, pg_catalog')
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('skybuild:migrate', 0))")
        provisioner._require_same_cluster(connection, report['database_system_id'])
        assert connection.execute('SELECT current_database()').fetchone()[0] == provisioner.DATABASE
        assert connection.execute('SELECT version, digest FROM schema_migrations ORDER BY version').fetchall() == expected
        assert connection.execute('SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND usename = %s',
                                  (provisioner.DATABASE, provisioner.ROLE)).fetchone()[0] == 0
        connection.execute(migration.read_text())
        connection.execute('INSERT INTO schema_migrations VALUES (12, %s)', (digest,))
        assert provision_runtime_role(connection, provisioner.DATABASE, provisioner.ROLE)['ok']
        assert audit_runtime_role(connection, provisioner.DATABASE, provisioner.ROLE)['ok']
    print(json.dumps({'ok': True, 'schema_version': 12, 'runtime_role_audit': 'passed', 'migration_committed': True}))
except Exception:
    print(json.dumps({'ok': False, 'migration_outcome': 'unconfirmed; inspect exact schema before any restart'}))
    raise SystemExit(2)
PY
```

A lost commit acknowledgment remains unconfirmed. Inspect exact digests/grants before deciding what committed; never rerun blindly. If the transaction definitely rolled back and 011 plus its role policy remain verified, the operator may restart the retained old API container with unchanged TLS. That is pre-migration recovery, not post-012 binary downgrade.

## Start the pinned candidate and requalify

Only after confirmed 012 commit and full candidate role audit, replace only the retained stopped API. Recheck the retained API and database IDs and the existing network; remove only the immutable stopped ID. Creation must fail on any name collision, and startup targets only its returned full ID. Do not rebuild, pull, reconcile a Compose service name, restart PostgreSQL or replace credentials here. Coordinate exclusive maintenance with other operators; identity checks cannot prevent another authorized operator from changing the runtime independently.

```sh
nice -n 10 ./.venv/bin/python - <<'PY'
import json, os, pathlib, re, sys
sys.path.insert(0, str(pathlib.Path.cwd() / 'scripts'))
import manual_pilot_tls as tls
try:
    state = pathlib.Path(os.environ['SKYBUILD_PILOT_STATE'])
    report_path = pathlib.Path(os.environ['SKYBUILD_PROMOTION_REPORT'])
    tls.private_file(report_path)
    report = json.loads(report_path.read_text())
    image = os.environ['SKYBUILD_PROMOTION_IMAGE_ID']
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', image)
    old = json.loads(tls.command('docker', 'inspect', report['api_container']))[0]
    named = json.loads(tls.command('docker', 'inspect', 'skybuild-pilot-api'))[0]
    db = json.loads(tls.command('docker', 'inspect', report['database_container']))[0]
    assert old['Id'] == named['Id'] == report['api_container']
    assert old['Image'] == report['api_image'] and not old['State']['Running']
    assert os.getuid() != 0 and old['Config']['User'] == str(os.getuid())
    assert db['Id'] == report['database_container'] and db['State']['Running']
    networks = old['NetworkSettings']['Networks']
    assert len(networks) == 1
    network_name, network = next(iter(networks.items()))
    assert db['NetworkSettings']['Networks'][network_name]['NetworkID'] == network['NetworkID']
    assert re.fullmatch(r'[0-9a-f]{64}', network['NetworkID'])
    labels = old['Config']['Labels']
    assert labels['com.docker.compose.project'] == 'skybuild-pilot'
    assert labels['com.docker.compose.service'] == 'api'
    assert labels['com.docker.compose.project.working_dir'] == str(pathlib.Path.cwd() / 'ops/manual-pilot')
    args = ['docker', 'create', '--pull', 'never', '--name', 'skybuild-pilot-api',
            '--network', network['NetworkID'], '--network-alias', 'api',
            '--user', str(os.getuid()), '--memory', '512m', '--pids-limit', '128',
            '--restart', 'unless-stopped', '--env-file', str(state / 'runtime.env'),
            '--publish', '127.0.0.1:8000:8000',
            '--publish', os.environ['SKYBUILD_PILOT_TAILNET_IP'] + ':8443:8000']
    for name in ('server.crt', 'server.key'):
        args += ['--mount', f'type=bind,src={state / "tls" / name},dst=/run/skybuild-tls/{name},readonly']
    for key in ('com.docker.compose.project', 'com.docker.compose.service',
                'com.docker.compose.project.working_dir', 'com.docker.compose.project.config_files'):
        args += ['--label', key + '=' + labels[key]]
    args += [image, *old['Config']['Cmd']]
    tls.command('docker', 'rm', report['api_container'])
    created = tls.command(*args)
    assert re.fullmatch(r'[0-9a-f]{64}', created)
    candidate = json.loads(tls.command('docker', 'inspect', created))[0]
    assert candidate['Id'] == created and candidate['Image'] == image
    tls.command('docker', 'start', created)
    print(json.dumps({'candidate_container': created, 'candidate_image': image}))
except Exception:
    print(json.dumps({'ok': False, 'replacement_outcome': 'unconfirmed; inspect retained identities; do not reconcile names'}))
    raise SystemExit(2)
PY
curl --fail --silent --max-time 10 --cacert "$SKYBUILD_PILOT_STATE/tls/ca.crt" --resolve "$CONTROLLER_HOST:8000:127.0.0.1" "https://$CONTROLLER_HOST:8000/health/ready"
```

Require HTTP readiness `{"status":"ready"}` and retain the separate transactional migration/role evidence for schema 12; the HTTP response does not expose a schema version. Require exact candidate image/container identity, unchanged runtime-only environment, TLS bindings/mounts/UID and CA fingerprint. Verify authenticated `/api/v1/me`, read-only task execution-status and owner CPU-control inspection with protected token files. Do not enable controls, reserve work, invoke a simulator, or switch ledger authority to populate a panel.

On each qualified worker, rerun read-only `fleet_preflight` with existing scoped credentials and pinned `--ca-file` at the same private DNS endpoint. Then use the explicitly approved harmless Cord qualification procedure for send/receive/receipt/handle, retaining message IDs. No worker/inference starts follow. Record source/image/container/cluster IDs, schema digests, role audit, readiness, TLS expiry/fingerprint and remote outcomes privately. Declare success only when all required checks pass.

## Follow-on task-authority cutover

This one-time cutover followed deployment of the healthy migration-012 candidate, disposable rehearsal of the exact 38-task manifest, and the stop of Markdown task edits and other ledger writers. The frozen source was commit `d79d2e1947d2c8e9edb577ab5f5093edfa3c94e3`, with content digest `307019c967c0531912be0441809da8d89ca506905ff63594976bf5463ba142c7` and the reviewed import digest in the command below. The command is retained as the historical operator procedure, not a command to rerun. The live transaction checked the PostgreSQL system identifier, retained database container, backup archive database name and pinned backup evidence before inserting all 38 tasks and the API-authority receipt atomically.

```sh
export SKYBUILD_EXPECTED_DATABASE='skybuild_pilot'
# Load SKYBUILD_ROLE_ADMIN_DSN from the existing protected operator environment.
nice -n 10 ./.venv/bin/python -m skybuild ledger-cutover \
  --ledger-dir docs/design \
  --contract docs/design/implementation/current_task_import.json \
  --apply-live \
  --expected-import-sha256 b25770457bc3c55d5697e67cb582a302ed39712b2693c3218e0079517376663a \
  --backup-file "$PRIVATE_DUMP" --backup-sha256 "$BACKUP_SHA256" \
  --backup-evidence "$PRIVATE_BACKUP_EVIDENCE" --backup-evidence-sha256 "$BACKUP_EVIDENCE_SHA256" \
  --expected-system-identifier "$CURRENT_DB_SYSTEM_ID" \
  --database-container-id "$CURRENT_DB_CONTAINER"
```

Cutover verification found exactly the 38 frozen task IDs and an `imported` event in `SKYBUILD-TASK-CUTOVER` history. An idempotent owner update then set the next action to retire the Markdown ledgers, preserved the workflow phase, and produced a second history event. All three former ledger paths now contain retirement notices; their frozen source remains in Git history. The API later gained 22 created tasks, so its current total of 60 is not the frozen import count. Do not re-import the notices or add bidirectional synchronization.

## Rollback boundary and remaining gaps

Migration 012 widens the ledger authority check and changes the row-lock helper so missing receipts fail closed while only an explicit API receipt permits writes and claims. The retained schema-011 image requires the exact migration list and rejects schema 012. **After confirmed 012 commit, the old image is not a qualified healthy rollback.** Failed candidate readiness requires evidence preservation and reviewed forward repair. Any restore must be isolated, explicitly authorized and tested, with acknowledged data-loss/effect reconciliation; never silently replace the live database or renew authority. Never delete 012's version record or objects to force old readiness.

Old image/dump evidence is not tested disaster recovery. The source rehearsal alone did not qualify a live build, preflight, migration or remote worker connection; the later live promotion and separately rehearsed task cutover are complete. This record does not establish remote worker qualification, credential/CA rotation or an automatic worker launch. Further recovery or worker admission needs its own evidence and controls.
