# Restricted PostgreSQL runtime role qualification

Task: `SKYBUILD-BOOTSTRAP`, restricted runtime-role qualification slice. Source base: `7aac7292cd2afa7374168e7a9fd2cdbb3005d420`, architecture A35 working contract and sections 4/5/7. This implements the existing service-role separation requirement. It does not approve deployment, live provisioning, credential rotation, task authority cutover, or access to any SkyKeep database.

## Administrator and runtime boundaries

Use a dedicated SkyBuild database, separate migration credentials, and an existing clean PostgreSQL LOGIN role. The runtime role must have no superuser, CREATEDB, CREATEROLE, REPLICATION, BYPASSRLS, role memberships, database ownership, or schema-object ownership. Role creation and authentication configuration remain explicit administrator work; these commands never create roles or passwords, change role attributes, transfer ownership, or connect at service startup.

`skybuild migrate` and `skybuild provision` continue to use trusted administration credentials. `skybuild serve` uses the restricted runtime DSN and never migrates. Do not place the administrator DSN in the service environment.

After applying migrations with the migration administrator, set `SKYBUILD_ROLE_ADMIN_DSN` through the local secret environment and `SKYBUILD_EXPECTED_DATABASE` to the exact dedicated database name. Then run:

```sh
skybuild audit-runtime-role skybuild_runtime
skybuild provision-runtime-role skybuild_runtime
skybuild audit-runtime-role skybuild_runtime
```

Provisioning is an explicit mutation, not a dry run. Run it only under separately granted target-database authority. It revokes PUBLIC database CREATE/TEMP, PUBLIC access to the public/skybuild schemas, and PUBLIC table/sequence/function privileges in skybuild. These database-local changes deliberately affect other users of that dedicated database; do not use a shared application database. Provisioning sets explicit grants on existing known tables only. It grants no default privileges on future tables. An unknown schema table or failed final audit rolls back the entire operation when called through the CLI. Unrelated direct or column grants are reported rather than silently removed.

## Grant policy

The policy in `skybuild.runtime_role` allows SELECT on all known SkyBuild tables. Tasks, messages and mutable projections receive INSERT/UPDATE. Dependencies receive INSERT/DELETE. Journals, lineage and idempotency receipts receive INSERT only. Migration records, principals, principal grants and frozen import receipts receive SELECT only. There is no DELETE on tasks/messages/journals, TRUNCATE, REFERENCES, TRIGGER, sequence privilege, schema CREATE, temporary-table privilege, or grant option. Credential provisioning and importer writes fail with the runtime role.

PostgreSQL requires UPDATE privilege for SELECT FOR SHARE. Migration 010 preserves existing locks without granting UPDATE on principals or import receipts. Two narrow SECURITY DEFINER SQL helpers lock only a supplied principal or a supplied project's Markdown import marker. Their fixed search path is pg_catalog, table references are fully qualified, and PUBLIC EXECUTE is revoked. They return only principal ID/admin status or import-marker presence. Runtime EXECUTE is granted only on these two exact signatures. The audit checks their body, language, return contract, volatility and fixed search path against the checked-in migration. Existing owner-level CLI flows use the same helpers. This is a trusted administrator-owned boundary, not application authorization: API project/actor checks remain necessary.

The audit examines effective role/PUBLIC privileges, column grants and grant options, known and unexpected tables/sequences, non-system schemas and executable routines. Unsafe role flags, memberships and ownership fail qualification. A successful audit describes the current target database, not a durable guarantee against later administrator grants or schema changes. Re-run migration/readiness, provisioning and audit after schema changes. New schema versions require explicit policy review rather than automatic grants.

## Isolated evidence and limitations

The disposable PostgreSQL gate exercises real password-authenticated restricted connections. HTTP integration tests run with both migration-owner and restricted roles, including task creation, revision/idempotency checks, history, workflow, split/merge, Cord receipt/handling/reply and process restart. Additional restricted-role tests exercise concurrent claims, CPU reservations, observations and effect intents without launches. Negative SQL tests cover DDL, ownership powers, credential/import/migration writes, journal mutation, trigger disabling, replication bypass and privilege delegation. Drift tests cover PUBLIC privileges, column grants, grant options, unsafe role flags and memberships. Helper tests verify locks still block concurrent administrator row updates.

```sh
UV_CACHE_DIR=/tmp/skybuild-runtime-role-uv-cache python scripts/disposable_pg_gate.py --checkout /tmp/skybuild-runtime-role
```

These tests create only task-owned disposable containers/databases and remove containers after completion. The tool never probes or connects to another database. It cannot establish absence of grants or network access in another database on the same cluster; deployment qualification must separately inspect SkyKeep/vault access under explicit authority, or use a separate PostgreSQL cluster. Database-name checks prevent accidental mismatches but do not prove host identity. No live connection, authority cutover, deployment, model call or worker launch is part of this implementation. Independent exact-head review remains required before integration.
