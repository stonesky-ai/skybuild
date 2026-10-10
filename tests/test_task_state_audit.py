from copy import deepcopy
import json

import pytest

from skybuild.task_state_audit import analyze, assemble, choose_sample, digest, inventory, history
from skybuild.store import Store
from skybuild.workflow import Place, TaskToken


def task(place=Place.READY):
    token = TaskToken("skybuild", "TEST", place, title="Test", revision=1,
                      definition_revision=1, input_generation=1, policy_version="checks-v1",
                      responsible="owner", next_action="Inspect evidence")
    row = {"project_id": "skybuild", "task_id": "TEST", "title": "Test", "description": "A task",
           "status": "ready", "phase": place.value, "revision": 1, "priority": 0,
           "dependencies": [], "acceptance_criteria": ["Pass"], "architecture_refs": [],
           "responsible": "owner", "next_action": "Inspect evidence", "blocker": None,
           "metadata": {"_skybuild_workflow": {"petri": {"schema_version": 1, "token": token.to_dict()}}}}
    return row | Store.workflow_projection(row)


def packet(row=None):
    row = row or task()
    return {"task": row, "final_task": deepcopy(row), "read_errors": []}


def test_projection_corruption_is_a_contradiction_but_races_are_unknown():
    p = packet()
    p["task"]["place"] = p["final_task"]["place"] = "done"
    assert analyze(p)["verdict"] == "contradiction"
    p["final_task"]["revision"] = 2
    assert analyze(p)["verdict"] == "unstable"
    p = packet()
    p["final_task"]["title"] = "Changed without revision"
    assert analyze(p)["verdict"] == "unstable"


def test_legacy_done_is_not_a_missing_petri_alarm():
    p = packet()
    p["task"]["metadata"] = p["final_task"]["metadata"] = {}
    p["task"]["status"] = p["final_task"]["status"] = "done"
    assert analyze(p)["verdict"] == "not_applicable"


def test_stored_token_overlay_is_not_an_error_and_ready_dependencies_can_wait():
    p = packet()
    for row in [p["task"], p["final_task"]]:
        row["metadata"]["_skybuild_workflow"]["petri"]["token"]["revision"] = 0
        row["dependencies"] = ["WAITING"]
    assert analyze(p)["verdict"] == "consistent"


def test_done_missing_acceptance_is_unknown_not_proof_of_incorrect_state():
    result = analyze(packet(task(Place.DONE)))
    assert result["verdict"] == "insufficient_evidence"
    assert any(c["code"] == "current_completion_contract" for c in result["checks"])


def test_history_comparison_ignores_additive_fields_and_respects_truncation():
    p = packet()
    p["history"] = {"complete": True, "items": [{"revision": 1, "after_state": deepcopy(p["task"])}]}
    for field in ["place", "validation", "evidence_freshness"]:
        p["history"]["items"][0]["after_state"].pop(field)
    assert analyze(p)["verdict"] == "consistent"
    p["history"]["items"][0]["after_state"]["phase"] = "done"
    assert analyze(p)["verdict"] == "contradiction"
    p["history"]["complete"] = False
    assert analyze(p)["verdict"] == "insufficient_evidence"


def test_execution_absence_does_not_prove_safety_and_held_effects_need_reconciliation():
    p = packet()
    p["execution"] = {"revision": 1, "effects": {"items": [], "truncated": False},
                      "reservations": {"items": [], "truncated": False},
                      "observations": {"items": [], "truncated": False}}
    assert "does not prove" in analyze(p)["checks"][-1]["observed"]
    p["execution"]["effects"]["items"] = [{"operation_id": "x", "exposure_held": True}]
    assert analyze(p)["verdict"] == "insufficient_evidence"


def test_inventory_finishes_full_page_with_another_request_and_rejects_duplicates():
    class API:
        calls = []

        def list_tasks(self, project, **kwargs):
            self.calls.append(kwargs)
            return [{"task_id": "A"}, {"task_id": "B"}] if kwargs["after_task_id"] is None else []

    api = API()
    assert len(inventory(api, "skybuild", 2)) == 2
    assert api.calls[-1]["after_task_id"] == "B"
    api.list_tasks = lambda *args, **kwargs: [{"task_id": "A"}, {"task_id": "A"}]
    with pytest.raises(ValueError, match="Duplicate"):
        inventory(api, "skybuild", 2)


def test_history_bound_is_not_treated_as_complete():
    class API:
        def task_history(self, *args, **kwargs):
            return [{"revision": i} for i in range(kwargs["limit"])]

    assert history(API(), "skybuild", "TEST", 10)["complete"] is False


def test_sampling_is_reproducible_and_stratified_without_replacement():
    rows = [{"task_id": str(i), "place": ["ready", "done", None][i % 3]} for i in range(30)]
    chosen = choose_sample(rows, 6, 123, "stratified")
    assert chosen == choose_sample(list(reversed(rows)), 6, 123, "stratified")
    assert len({r["task_id"] for r in chosen}) == 6
    assert len({r["place"] for r in chosen}) == 3


def test_assemble_pins_reviews_and_keeps_actions_non_executing(tmp_path):
    from skybuild.task_state_audit import SCHEMA
    report = {"schema": SCHEMA, "tasks": [{"task_id": "TEST", "revision": 7, "place": "ready",
               "verdict": "consistent", "evidence": "evidence/0.json", "checks": []}]}
    review = {"task_id": "TEST", "audited_revision": 7, "report_sha256": digest(report),
              "verdict": "false_negative", "reason": "Definition is ambiguous", "evidence_refs": ["evidence/0.json"]}
    assert assemble(report, [review], tmp_path)["proposals"] == 1
    result = json.loads((tmp_path / "revisions.json").read_text())
    assert result["apply_supported"] is False
    assert result["proposals"][0]["decision"] == "pending"
    assert result["proposals"][0]["proposed_changes"] == {}
    review["audited_revision"] = 6
    with pytest.raises(ValueError, match="bind"):
        assemble(report, [review], tmp_path, "other.json")


def test_false_positive_is_dismissed_not_an_actionable_revision(tmp_path):
    from skybuild.task_state_audit import SCHEMA
    report = {"schema": SCHEMA, "tasks": [{"task_id": "TEST", "revision": 1, "place": "ready",
               "verdict": "contradiction", "checks": [], "evidence": "evidence/0.json"}]}
    review = {"task_id": "TEST", "audited_revision": 1, "report_sha256": digest(report),
              "verdict": "false_positive", "reason": "Different supported contract", "evidence_refs": ["evidence/0.json"]}
    assert assemble(report, [review], tmp_path)["proposals"] == 0
    assert len(json.loads((tmp_path / "revisions.json").read_text())["dismissed"]) == 1


def test_full_scan_reads_only_get_routes_and_writes_bound_evidence(tmp_path):
    import httpx
    from skybuild.client import Client
    from skybuild.task_state_audit import scan
    row = task()
    methods = []

    def respond(request):
        methods.append(request.method)
        assert request.method == "GET"
        path = request.url.path
        if path.endswith("/tasks"):
            value = [row]
        elif path.endswith("/workflow"):
            value = {"task": row, "token": Store.workflow_token(row).to_dict(), "available_actions": []}
        elif path.endswith("/history"):
            value = [{"revision": 1, "after_state": row}]
        elif path.endswith("/execution-status"):
            value = {"revision": 1, **{key: {"items": [], "truncated": False}
                                      for key in ("effects", "reservations", "observations")}}
        else:
            value = row
        return httpx.Response(200, json=value)

    output = tmp_path / "audit"
    with Client("https://example.test", "private-test-token", transport=httpx.MockTransport(respond)) as client:
        result = scan(client, "skybuild", output, sample_size=1)
    assert result["counts"] == {"consistent": 1}
    report = json.loads((output / "report.json").read_text())
    packet_bytes = (output / report["tasks"][0]["evidence"]).read_text()
    assert "private-test-token" not in packet_bytes
    assert digest(json.loads(packet_bytes)) == report["tasks"][0]["evidence_sha256"]
    assert (output.stat().st_mode & 0o777) == 0o700
    assert ((output / "report.json").stat().st_mode & 0o777) == 0o600
    template = json.loads((output / "review-template.json").read_text())
    assert template[0]["report_sha256"] == digest(report)
    assert template[0]["audited_revision"] == 1
