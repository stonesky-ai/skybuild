from __future__ import annotations

import hashlib

import pytest

from skybuild.bundling import BundleCandidate, BundlePlanningError, plan_bundles


BASE = "a" * 40


def candidate(
    name: str,
    paths: set[str],
    *,
    dependencies: set[str] | None = None,
    base: str = BASE,
    status: str = "in-progress",
    phase: str = "ready-for-integration",
    reviewed_head: str | None = None,
) -> BundleCandidate:
    head = hashlib.sha1(name.encode()).hexdigest()
    return BundleCandidate(
        task_id=f"SKYBUILD-{name}",
        status=status,
        phase=phase,
        ref=f"refs/heads/task/{name.lower()}",
        head_sha=head,
        base_sha=base,
        changed_paths=frozenset(paths),
        dependencies=frozenset(dependencies or ()),
        review_head_sha=reviewed_head or head,
        review_verdict="pass",
        reviewer="independent-reviewer",
        review_evidence=f"reviews/{name.lower()}.md",
    )


def test_five_manual_worker_cases_group_by_overlapping_paths():
    tasks = [
        candidate("MANUAL-A", {"src/skybuild/manual_dispatch.py", "tests/test_manual_dispatch.py"}),
        candidate("MANUAL-B", {"src/skybuild/manual_dispatch.py", "src/skybuild/manual_assignment.py"}),
        candidate("MANUAL-C", {"src/skybuild/manual_assignment.py", "tests/test_manual_assignment.py"}),
        candidate("MANUAL-D", {"tests/test_manual_assignment.py", "docs/design/implementation/manual_worker.md"}),
        candidate("MANUAL-E", {"src/skybuild/execution_status.py", "tests/test_execution_status.py"}),
    ]

    plan = plan_bundles(tasks)

    assert [[item.task_id for item in group.members] for group in plan.bundles] == [[
        "SKYBUILD-MANUAL-A", "SKYBUILD-MANUAL-B", "SKYBUILD-MANUAL-C", "SKYBUILD-MANUAL-D"
    ]]
    assert plan.bundles[0].shared_paths == (
        "src/skybuild/manual_assignment.py",
        "src/skybuild/manual_dispatch.py",
        "tests/test_manual_assignment.py",
    )
    assert plan.unbundled == ("SKYBUILD-MANUAL-E",)


def test_common_ignored_path_does_not_create_false_overlap():
    plan = plan_bundles(
        [candidate("A", {"docs/design/implementation_plan.md"}),
         candidate("B", {"docs/design/implementation_plan.md"})],
        ignored_paths=("docs/design/*",),
    )

    assert plan.bundles == ()
    assert plan.unbundled == ("SKYBUILD-A", "SKYBUILD-B")


def test_dense_group_respects_maximum_and_leaves_singleton_unbundled():
    tasks = [candidate(name, {"src/skybuild/shared.py"}) for name in "ABCDE"]

    plan = plan_bundles(tasks, max_members=2)

    assert [len(group.members) for group in plan.bundles] == [2, 2]
    grouped = {item.task_id for group in plan.bundles for item in group.members}
    assert len(grouped) == 4
    assert len(plan.unbundled) == 1


def test_dependency_order_precedes_dependent_within_bundle():
    prerequisite = candidate("PREREQUISITE", {"src/skybuild/shared.py"})
    dependent = candidate("DEPENDENT", {"src/skybuild/shared.py"},
                          dependencies={prerequisite.task_id})

    plan = plan_bundles([dependent, prerequisite])

    assert [item.task_id for item in plan.bundles[0].members] == [
        prerequisite.task_id, dependent.task_id
    ]
    assert plan.bundles[0].dependency_edges == ((prerequisite.task_id, dependent.task_id),)


@pytest.mark.parametrize("changes, message", [
    ({"reviewed_head": "f" * 40}, "exact-head review"),
    ({"status": "blocked"}, "not ready for integration"),
    ({"phase": "working"}, "not ready for integration"),
])
def test_unready_or_stale_review_is_refused(changes, message):
    with pytest.raises(BundlePlanningError, match=message):
        candidate("A", {"src/skybuild/shared.py"}, **changes)


def test_previous_bundle_and_in_flight_members_are_not_selected_twice():
    tasks = [candidate(name, {"src/skybuild/shared.py"}) for name in "ABC"]

    plan = plan_bundles(
        tasks,
        already_bundled={"SKYBUILD-A"},
        in_flight={"SKYBUILD-B"},
    )

    assert plan.bundles == ()
    assert plan.unbundled == ("SKYBUILD-C",)
