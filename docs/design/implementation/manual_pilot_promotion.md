# Controlled manual pilot promotion from schema 010 to 011

Task: `SKYBUILD-SELF-BUILD-MVP`, with `SKYBUILD-EXECUTION-CONTROLS` inspection support. Owner: main SkyBuild session. Phase: candidate preparation; no runtime promotion performed. Source base: published `42c3256`. Accepted runtime: `7d40df9fa7b26035736ffa613b5c5dad548269f5`, schema 010. Obtain the full candidate SHA from the independently reviewed publication record after this procedure is integrated.

This procedure preserves the dedicated database, credentials, application CA/leaf, non-root API UID, private binds and manual Cord assignments. It changes neither ledger authority nor worker/model admission. Execute live steps only under explicit runtime-promotion authority. Do not use root, sudo, self-SSH, Docker host mounts, changed privileges, or another database to bypass a failed prerequisite.

## Read-only preparation

Use the stable deployment checkout named by the running containers' Compose working-directory labels. A candidate worktree has a different ownership path. Require a clean checkout at the exact published candidate, the retained current image/source association, full API/database container IDs and PostgreSQL system identifier. Reconcile missing or mismatched retained identities independently; do not replace an expected identity with whichever process owns a name or port.

Keep the existing TLS environment and private state. Check a fresh host-watch sample, preserve the owner's 8 GiB reserve, and require 10 GiB available before preparation/build. Run at nice level 10. Niceness of a Docker CLI does not itself constrain daemon build workers; separately qualify builder memory resources before building. Do not change daemon privileges/configuration automatically.

```sh
export SKYBUILD_PILOT_STATE='/absolute/existing/private/pilot-state'
export SKYBUILD_PILOT_TAILNET_IP='REPLACE_WITH_APPROVED_CURRENT_TAILSCALE_IPV4'
export SKYBUILD_PILOT_TLS_UID="$(id -u)"
export APPROVED_SHA='REPLACE_WITH_REVIEWED_PUBLISHED_40_HEX_SHA'
export PYTHONPATH="$PWD/src"
CURRENT_SHA='7d40df9fa7b26035736ffa613b5c5dad548269f5'
CURRENT_API_CONTAINER='REPLACE_WITH_RETAINED_FULL_64_HEX_CONTAINER_ID'
CURRENT_DB_CONTAINER='REPLACE_WITH_RETAINED_FULL_64_HEX_CONTAINER_ID'
CURRENT_API_IMAGE='REPLACE_WITH_RETAINED_sha256_IMAGE_ID'
CURRENT_DB_SYSTEM_ID='REPLACE_WITH_RETAINED_POSTGRESQL_SYSTEM_IDENTIFIER'
CURRENT_CA_PEM_SHA256='REPLACE_WITH_RETAINED_64_HEX_CA_PEM_FILE_DIGEST'
CONTROLLER_HOST='REPLACE_WITH_APPROVED_CURRENT_SELF_DNS_NAME'
nice -n 10 ./.venv/bin/python scripts/manual_pilot_controller.py --promotion --checkout "$PWD" --expected-sha "$APPROVED_SHA" --published-ref refs/heads/dev-002 --current-sha "$CURRENT_SHA" --state-dir "$SKYBUILD_PILOT_STATE" --hostname "$CONTROLLER_HOST" --tailnet-ip "$SKYBUILD_PILOT_TAILNET_IP" --api-container-id "$CURRENT_API_CONTAINER" --db-container-id "$CURRENT_DB_CONTAINER" --api-image-id "$CURRENT_API_IMAGE" --database-system-id "$CURRENT_DB_SYSTEM_ID" --ca-pem-sha256 "$CURRENT_CA_PEM_SHA256"
```

The guard checks published clean source/ancestry, installed package hashes against retained source, exact container/image/cluster identities, unchanged migration digests 001–010 and only expansion 011, private runtime environment, TLS/CA/readiness, binds/mounts/UID, resources, empty Serve/Funnel, niceness and headroom. PostgreSQL reads use a bounded repeatable-read/read-only administrator transaction. The candidate role auditor must find exactly the two absent simulator tables and no excess/mismatched privileges; this is not a passed schema-011 audit. A failure changes no services or database state. Remote worker reachability remains a separate qualification.

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
         "files=sorted(f for f in p.rglob('*') if f.is_file() and f.suffix in ('.py','.sql')); "
         "assert len(files)<=200 and all(f.stat().st_size<=1048576 for f in files); "
         "print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}))")
actual = json.loads(tls.command('docker', 'run', '--rm', '--network', 'none', '--read-only',
    '--memory', '256m', '--memory-swap', '256m', '--cpus', '1', '--pids-limit', '32',
    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--entrypoint', 'python', image, '-c', probe))
assert actual == controller._source_manifest(pathlib.Path.cwd(), os.environ['APPROVED_SHA'])
print(json.dumps({'candidate_image': image, 'package_matches_reviewed_source': True}))
PY
```

The verification container has no network, credentials, mounts, writable root, capabilities, service listener or model invocation. Use an existing private mode-0700 evidence directory outside Git and `umask 077` for new files. Save a custom-format dump of only `skybuild_pilot` to a new file through the dedicated container's ordinary PostgreSQL channel: `docker exec --user postgres skybuild-pilot-pg nice -n 10 pg_dump --format=custom --dbname=skybuild_pilot > "$PRIVATE_DUMP"`. Check exit status and a bounded dump manifest. No password belongs in arguments. The dump has not undergone a restore drill and is not an automatic rollback guarantee; never overwrite it.

Create a private Compose image overlay containing only the API's verified `sha256:` image ID and `pull_policy: never`. Keep the same base and TLS files. Inspect rendered allowlisted configuration without printing resolved environment secrets; only the image may change. Recheck the image ID before use. Rerun the full preflight immediately before the stop and save its successful JSON in a new mode-0600 `$SKYBUILD_PROMOTION_REPORT` file. Set `$SKYBUILD_PROMOTION_OVERLAY` to the reviewed private overlay path. Neither file contains secrets. Parent-directory ownership and private modes remain operator prerequisites.

## Stop only the API and migrate atomically

After authority, stop only `api`; leave PostgreSQL and storage running. Retain the stopped API identity/config and old image. Never rerun initial provisioning, replace credentials, import task authority, run `down`, or remove volumes/state.

```sh
nice -n 10 docker compose -f ops/manual-pilot/compose.yaml -f ops/manual-pilot/compose.tls.yaml stop --timeout 30 api
```

This explicit operator transaction checks the stopped container, cluster/database, exact 010 prefix, clean reviewed source and absence of runtime-role sessions. DDL, version digest, runtime grants and full candidate audit commit together. Do not substitute separate `migrate` and `provision-runtime-role` CLI calls; those commit independently.

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
    migration = root / 'src/skybuild/migrations/011_cpu_fake_dispatch.sql'
    digest = hashlib.sha256(migration.read_bytes()).hexdigest()
    candidate = controller._source_manifest(root, report['candidate_source'])
    assert digest == candidate['migrations/011_cpu_fake_dispatch.sql']
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
        connection.execute('INSERT INTO schema_migrations VALUES (11, %s)', (digest,))
        assert provision_runtime_role(connection, provisioner.DATABASE, provisioner.ROLE)['ok']
        assert audit_runtime_role(connection, provisioner.DATABASE, provisioner.ROLE)['ok']
    print(json.dumps({'ok': True, 'schema_version': 11, 'runtime_role_audit': 'passed', 'migration_committed': True}))
except Exception:
    print(json.dumps({'ok': False, 'migration_outcome': 'unconfirmed; inspect exact schema before any restart'}))
    raise SystemExit(2)
PY
```

A lost commit acknowledgment remains unconfirmed. Inspect exact digests/grants before deciding what committed; never rerun blindly. If the transaction definitely rolled back and 010 plus its role policy remain verified, the operator may restart the retained old API container with unchanged TLS. That is pre-migration recovery, not post-011 binary downgrade.

## Start the pinned candidate and requalify

Only after confirmed 011 commit and full candidate role audit, recreate only the API using the reviewed immutable image overlay. Do not rebuild, pull, restart PostgreSQL or replace credentials here.

```sh
nice -n 10 docker compose -f ops/manual-pilot/compose.yaml -f ops/manual-pilot/compose.tls.yaml -f "$SKYBUILD_PROMOTION_OVERLAY" up -d --no-deps --no-build --pull never api
curl --fail --silent --max-time 10 --cacert "$SKYBUILD_PILOT_STATE/tls/ca.crt" --resolve "$CONTROLLER_HOST:8000:127.0.0.1" "https://$CONTROLLER_HOST:8000/health/ready"
```

Require HTTP readiness `{"status":"ready"}` and retain the separate transactional migration/role evidence for schema 11; the HTTP response does not expose a schema version. Require exact candidate image/container identity, unchanged runtime-only environment, TLS bindings/mounts/UID and CA fingerprint. Verify authenticated `/api/v1/me`, read-only task execution-status and owner CPU-control inspection with protected token files. Do not enable controls, reserve work, invoke a simulator, or switch ledger authority to populate a panel.

On each qualified worker, rerun read-only `fleet_preflight` with existing scoped credentials and pinned `--ca-file` at the same private DNS endpoint. Then use the explicitly approved harmless Cord qualification procedure for send/receive/receipt/handle, retaining message IDs. No worker/inference starts follow. Record source/image/container/cluster IDs, schema digests, role audit, readiness, TLS expiry/fingerprint and remote outcomes privately. Declare success only when all required checks pass.

## Rollback boundary and remaining gaps

Migration 011 adds a column/tables, widens states and replaces guards. The accepted 7d40 binary's readiness requires the exact migration list and rejects schema 011; it also predates released/settled simulator records. **After confirmed 011 commit, the old image is not a qualified healthy rollback.** Failed candidate readiness requires evidence preservation and reviewed forward repair. Any restore must be isolated, explicitly authorized and tested, with acknowledged data-loss/effect reconciliation; never silently replace the live database or renew authority. Never delete 011's version record or objects to force old readiness.

Old image/dump evidence is not tested disaster recovery. Builder memory qualification, later immutable image build, actual live preflight, migration and remote qualification remain unperformed by this source task. No credential/CA rotation, task cutover, model qualification or worker launch is included.
