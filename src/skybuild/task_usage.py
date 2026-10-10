"""Append-only task and attempt usage history; no collection or budget policy."""

from decimal import Decimal
import re
from uuid import UUID, uuid4

from .contracts import DomainError


_QUANTITY = re.compile(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{0,11}[1-9])?\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset({
    "event_kind", "attempt_id", "task_revision", "definition_revision",
    "input_generation", "claim_fence", "provider", "model", "pool_id",
    "policy_window_id", "operation_id", "unit", "quantity", "evidence_ref",
    "evidence_sha256", "reason",
})


def unresolved_usage_exists(connection, project_id, task_id):
    """Whether this task or any replaced source has unresolved uncertainty."""
    return bool(connection.execute(
        "WITH RECURSIVE ancestors(task_id) AS ("
        "SELECT %s::text UNION SELECT l.source_task_id FROM task_lineage l "
        "JOIN ancestors a ON l.target_task_id = a.task_id WHERE l.project_id = %s) "
        "SELECT 1 FROM task_usage_events u WHERE u.project_id = %s "
        "AND u.task_id IN (SELECT task_id FROM ancestors) AND u.event_kind = 'uncertain' "
        "AND NOT EXISTS (SELECT 1 FROM task_usage_events r WHERE r.resolves_event_id = u.event_id) LIMIT 1",
        (task_id, project_id, project_id),
    ).fetchone())


def _text(value, name, *, maximum=200):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or "\x00" in value):
        raise DomainError("validation", f"{name} must be nonempty text of at most {maximum} characters", 422)
    return value


def _quantity(value):
    if not isinstance(value, str) or not _QUANTITY.fullmatch(value):
        raise DomainError("validation", "quantity must be a bounded canonical nonnegative decimal string", 422)
    return value


def _digest(value):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise DomainError("validation", "evidence_sha256 must be 64 lowercase hexadecimal characters", 422)
    return value


def _canonical(value):
    # Decimal.normalize() applies the ambient precision and can round a valid
    # 30-digit aggregate.  Fixed-point formatting preserves the exact NUMERIC.
    result = format(Decimal(value), "f")
    return result if "." not in result else result.rstrip("0").rstrip(".")


class TaskUsageHistory:
    """Trusted append and read paths for immutable usage facts."""

    def record_task_usage(self, principal, project_id, task_id, body, idempotency_key):
        if not isinstance(body, dict) or set(body) != _FIELDS:
            raise DomainError("validation", "Usage record has missing or unknown fields", 422)
        if not isinstance(body["event_kind"], str) or body["event_kind"] not in {"consumed", "uncertain"}:
            raise DomainError("validation", "event_kind must be consumed or uncertain", 422)
        event = {
            "event_id": str(uuid4()),
            "project_id": project_id,
            "task_id": task_id,
            "event_kind": body["event_kind"],
            "attempt_id": _text(body["attempt_id"], "attempt_id"),
            "task_revision": body["task_revision"],
            "definition_revision": body["definition_revision"],
            "input_generation": body["input_generation"],
            "claim_fence": body["claim_fence"],
            "provider": _text(body["provider"], "provider"),
            "model": _text(body["model"], "model"),
            "pool_id": _text(body["pool_id"], "pool_id"),
            "policy_window_id": _text(body["policy_window_id"], "policy_window_id"),
            "operation_id": _text(body["operation_id"], "operation_id"),
            "unit": _text(body["unit"], "unit", maximum=64),
            "quantity": _quantity(body["quantity"]),
            "evidence_ref": _text(body["evidence_ref"], "evidence_ref", maximum=1024),
            "evidence_sha256": _digest(body["evidence_sha256"]),
            "reason": _text(body["reason"], "reason", maximum=4096),
        }
        numeric = ("task_revision", "definition_revision", "input_generation", "claim_fence")
        if (any(type(event[key]) is not int for key in numeric)
                or event["task_revision"] < 1 or event["definition_revision"] < 1
                or event["input_generation"] < 0 or event["claim_fence"] < 1):
            raise DomainError("validation", "Usage record has invalid task or attempt binding", 422)
        if event["event_kind"] == "uncertain" and event["quantity"] == "0":
            raise DomainError("validation", "Uncertain exposure must have a positive bounded amount", 422)
        payload = {key: value for key, value in event.items() if key != "event_id"}
        with self._connection() as connection:
            actor = self._authorize(connection, principal, project_id, "tasks:usage-record")

            def mutation():
                # Structural edits, claims and usage writes share graph-lock then
                # task-row-lock order so late source events cannot race lineage.
                self._graph_lock(connection, project_id)
                self._task(connection, project_id, task_id, lock=True)
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"skybuild:usage:{project_id}:{event['provider']}:{event['operation_id']}",),
                )
                prior = connection.execute(
                    "SELECT * FROM task_usage_events WHERE project_id = %s AND provider = %s AND operation_id = %s",
                    (project_id, event["provider"], event["operation_id"]),
                ).fetchone()
                if prior:
                    comparable = {key: prior[key] for key in payload if key not in {"project_id"}}
                    expected = {key: payload[key] for key in comparable}
                    if comparable != expected:
                        raise DomainError("usage_conflict", "Provider operation identity already has different usage facts", 409)
                    return prior
                history = connection.execute(
                    "SELECT event_id, after_state FROM task_journal WHERE project_id = %s AND task_id = %s AND revision = %s",
                    (project_id, task_id, event["task_revision"]),
                ).fetchone()
                if not history:
                    raise DomainError("stale_revision", "Usage task revision is not present in task history", 409)
                snapshot = history["after_state"]
                petri = snapshot.get("metadata", {}).get("_skybuild_workflow", {}).get("petri", {})
                attempt_binding = petri.get("attempt_binding")
                token = petri.get("token", {})
                expected_binding = {
                    "attempt_id": event["attempt_id"],
                    "task_revision": event["task_revision"],
                    "input_generation": event["input_generation"],
                    "claim_fence": event["claim_fence"],
                }
                if (not isinstance(attempt_binding, dict) or attempt_binding != expected_binding
                        or token.get("definition_revision") != event["definition_revision"]):
                    raise DomainError("usage_conflict", "Usage identity does not match an immutable task attempt", 409)
                event["task_journal_event_id"] = history["event_id"]
                columns = tuple(event) + ("actor",)
                values = tuple(event.values()) + (actor.principal_id,)
                row = connection.execute(
                    "INSERT INTO task_usage_events (" + ", ".join(columns) + ") VALUES (" +
                    ", ".join(["%s"] * len(columns)) + ") RETURNING *", values,
                ).fetchone()
                return row

            return self._idempotent(connection, actor, project_id, "task.usage.record",
                                    idempotency_key, payload, mutation)

    def resolve_task_usage(self, principal, project_id, task_id, event_id, body, idempotency_key):
        if not isinstance(body, dict) or set(body) != {
                "operation_id", "quantity", "evidence_ref", "evidence_sha256", "reason"}:
            raise DomainError("validation", "Usage resolution has missing or unknown fields", 422)
        try:
            origin_id = str(UUID(str(event_id)))
        except (ValueError, TypeError, AttributeError):
            raise DomainError("validation", "event_id must be a UUID", 422) from None
        operation_id = _text(body["operation_id"], "operation_id")
        quantity = _quantity(body["quantity"])
        evidence_ref = _text(body["evidence_ref"], "evidence_ref", maximum=1024)
        evidence_sha256 = _digest(body["evidence_sha256"])
        reason = _text(body["reason"], "reason", maximum=4096)
        payload = {"event_id": origin_id, "operation_id": operation_id, "quantity": quantity,
                   "evidence_ref": evidence_ref, "evidence_sha256": evidence_sha256, "reason": reason}
        with self._connection() as connection:
            actor = self._authorize(connection, principal, project_id, "tasks:usage-resolve")

            def mutation():
                origin = connection.execute(
                    "SELECT * FROM task_usage_events WHERE project_id = %s AND task_id = %s "
                    "AND event_id = %s FOR UPDATE", (project_id, task_id, origin_id),
                ).fetchone()
                if not origin or origin["event_kind"] != "uncertain":
                    raise DomainError("not_found", "Unresolved usage exposure not found", 404)
                if origin["actor"] == actor.principal_id:
                    raise DomainError("authorization", "A different trusted principal must resolve usage exposure", 403)
                prior_resolution = connection.execute(
                    "SELECT * FROM task_usage_events WHERE resolves_event_id = %s", (origin_id,),
                ).fetchone()
                if prior_resolution:
                    if (prior_resolution["operation_id"] == operation_id
                            and prior_resolution["quantity"] == quantity
                            and prior_resolution["evidence_ref"] == evidence_ref
                            and prior_resolution["evidence_sha256"] == evidence_sha256
                            and prior_resolution["reason"] == reason):
                        return prior_resolution
                    raise DomainError("usage_conflict", "Uncertain usage already has an immutable resolution", 409)
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"skybuild:usage:{project_id}:{origin['provider']}:{operation_id}",),
                )
                prior_operation = connection.execute(
                    "SELECT * FROM task_usage_events WHERE project_id = %s AND provider = %s AND operation_id = %s",
                    (project_id, origin["provider"], operation_id),
                ).fetchone()
                if prior_operation:
                    if (prior_operation["event_kind"] == "resolved"
                            and prior_operation["resolves_event_id"] == origin_id
                            and prior_operation["quantity"] == quantity
                            and prior_operation["evidence_ref"] == evidence_ref
                            and prior_operation["evidence_sha256"] == evidence_sha256
                            and prior_operation["reason"] == reason):
                        return prior_operation
                    raise DomainError("usage_conflict", "Provider operation identity already has different usage facts", 409)
                row = connection.execute(
                    "INSERT INTO task_usage_events (event_id, project_id, task_id, event_kind, attempt_id, "
                    "task_journal_event_id, task_revision, definition_revision, input_generation, claim_fence, provider, model, pool_id, "
                    "policy_window_id, operation_id, unit, quantity, evidence_ref, evidence_sha256, reason, "
                    "actor, resolves_event_id) VALUES (%s, %s, %s, 'resolved', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (str(uuid4()), origin["project_id"], origin["task_id"], origin["attempt_id"],
                     origin["task_journal_event_id"], origin["task_revision"], origin["definition_revision"], origin["input_generation"],
                     origin["claim_fence"], origin["provider"], origin["model"], origin["pool_id"],
                     origin["policy_window_id"], operation_id, origin["unit"], quantity, evidence_ref,
                     evidence_sha256, reason, actor.principal_id, origin_id),
                ).fetchone()
                return row

            return self._idempotent(connection, actor, project_id, "task.usage.resolve",
                                    idempotency_key, payload, mutation)

    def task_usage_history(self, principal, project_id, task_id, *, limit=100, offset=0):
        self._page(limit, offset)
        with self._connection(consistent_snapshot=True) as connection:
            self._authorize(connection, principal, project_id, "tasks:read")
            self._task(connection, project_id, task_id)
            lineage = connection.execute(
                "WITH RECURSIVE ancestors(task_id) AS ("
                "SELECT %s::text UNION SELECT l.source_task_id FROM task_lineage l "
                "JOIN ancestors a ON l.target_task_id = a.task_id WHERE l.project_id = %s) "
                "SELECT task_id FROM ancestors ORDER BY task_id", (task_id, project_id),
            ).fetchall()
            task_ids = [row["task_id"] for row in lineage]
            rows = connection.execute(
                "SELECT e.* FROM task_usage_events e WHERE e.project_id = %s AND e.task_id = ANY(%s) "
                "ORDER BY e.created_at, e.event_id LIMIT %s OFFSET %s",
                (project_id, task_ids, limit, offset),
            ).fetchall()
            events = []
            for row in rows:
                event = dict(row)
                event["event_id"] = str(event["event_id"])
                event["task_journal_event_id"] = str(event["task_journal_event_id"])
                if event["resolves_event_id"] is not None:
                    event["resolves_event_id"] = str(event["resolves_event_id"])
                event["created_at"] = event["created_at"].isoformat()
                events.append(event)
            total = connection.execute(
                "SELECT count(*) AS count FROM task_usage_events WHERE project_id = %s AND task_id = ANY(%s)",
                (project_id, task_ids),
            ).fetchone()["count"]
            totals = connection.execute(
                "SELECT provider, model, pool_id, policy_window_id, unit, "
                "sum(CASE WHEN event_kind IN ('consumed', 'resolved') THEN quantity::numeric ELSE 0 END)::text AS consumed, "
                "sum(CASE WHEN event_kind = 'uncertain' AND NOT EXISTS ("
                "SELECT 1 FROM task_usage_events r WHERE r.resolves_event_id = task_usage_events.event_id) "
                "THEN quantity::numeric ELSE 0 END)::text AS uncertain "
                "FROM task_usage_events WHERE project_id = %s AND task_id = ANY(%s) "
                "GROUP BY provider, model, pool_id, policy_window_id, unit "
                "ORDER BY provider, model, pool_id, policy_window_id, unit",
                (project_id, task_ids),
            ).fetchall()
            return {
                "task_id": task_id,
                "lineage_task_ids": task_ids,
                "events": events,
                "totals": [{**row, "consumed": _canonical(row["consumed"]),
                            "uncertain": _canonical(row["uncertain"])} for row in totals],
                "limit": limit,
                "offset": offset,
                "total_events": total,
            }
