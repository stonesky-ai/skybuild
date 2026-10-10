"""Choose reviewed task heads likely to benefit from one integration gate."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class BundlePlanningError(ValueError):
    """The supplied candidates cannot produce a safe, deterministic plan."""


@dataclass(frozen=True)
class BundleCandidate:
    task_id: str
    status: str
    ref: str
    head_sha: str
    base_sha: str
    changed_paths: frozenset[str]
    dependencies: frozenset[str]
    review_head_sha: str
    review_verdict: str
    reviewer: str
    review_evidence: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise BundlePlanningError("Task IDs must be nonempty text")
        if self.status not in {"ready", "in-progress"}:
            raise BundlePlanningError(f"{self.task_id}: task is not ready for integration")
        if not isinstance(self.ref, str) or not self.ref.startswith("refs/heads/task/"):
            raise BundlePlanningError(f"{self.task_id}: member ref must name a task branch")
        if (not isinstance(self.head_sha, str) or not isinstance(self.base_sha, str)
                or not SHA_RE.fullmatch(self.head_sha) or not SHA_RE.fullmatch(self.base_sha)):
            raise BundlePlanningError(f"{self.task_id}: use full lowercase base and head SHAs")
        if self.review_verdict != "pass" or self.review_head_sha != self.head_sha:
            raise BundlePlanningError(f"{self.task_id}: exact-head review has not passed")
        if not isinstance(self.reviewer, str) or not self.reviewer.strip():
            raise BundlePlanningError(f"{self.task_id}: reviewer is required")
        if not isinstance(self.review_evidence, str) or not self.review_evidence.strip():
            raise BundlePlanningError(f"{self.task_id}: review evidence is required")
        if not self.changed_paths:
            raise BundlePlanningError(f"{self.task_id}: changed paths are required")
        if any(not isinstance(path, str) for path in self.changed_paths):
            raise BundlePlanningError(f"{self.task_id}: changed paths must be text")
        for path in self.changed_paths:
            parsed = PurePosixPath(path)
            if not path or parsed.is_absolute() or ".." in parsed.parts:
                raise BundlePlanningError(f"{self.task_id}: changed paths must be repository-relative")
        if any(not isinstance(task_id, str) or not task_id.strip() for task_id in self.dependencies):
            raise BundlePlanningError(f"{self.task_id}: dependency IDs must be nonempty text")


@dataclass(frozen=True)
class BundleGroup:
    members: tuple[BundleCandidate, ...]
    shared_paths: tuple[str, ...]
    dependency_edges: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class BundlePlan:
    bundles: tuple[BundleGroup, ...]
    excluded: tuple[str, ...]


def _shared_paths(left: BundleCandidate, right: BundleCandidate, ignored: tuple[str, ...]) -> set[str]:
    return {
        path for path in left.changed_paths & right.changed_paths
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in ignored)
    }


def _edge_weight(left: BundleCandidate, right: BundleCandidate, ignored: tuple[str, ...]) -> int:
    dependency = int(left.task_id in right.dependencies or right.task_id in left.dependencies)
    return len(_shared_paths(left, right, ignored)) + dependency


def _ordered(members: Iterable[BundleCandidate]) -> tuple[BundleCandidate, ...]:
    by_id = {member.task_id: member for member in members}
    incoming = {
        task_id: set(member.dependencies) & by_id.keys()
        for task_id, member in by_id.items()
    }
    ordered: list[BundleCandidate] = []
    while incoming:
        ready = sorted(task_id for task_id, dependencies in incoming.items() if not dependencies)
        if not ready:
            raise BundlePlanningError("Bundle members contain a dependency cycle")
        for task_id in ready:
            ordered.append(by_id[task_id])
            incoming.pop(task_id)
        for dependencies in incoming.values():
            dependencies.difference_update(ready)
    return tuple(ordered)


def plan_bundles(
    candidates: Iterable[BundleCandidate],
    *,
    max_members: int = 20,
    ignored_paths: Iterable[str] = (),
    already_bundled: Iterable[str] = (),
    in_flight: Iterable[str] = (),
) -> BundlePlan:
    """Prefer related work while coalescing every eligible head up to the cap.

    Exact-head reviews establish integration readiness; task status comes from
    authority. Dependencies order members across bundles as well as within them.
    Task ID prefixes do not imply related code. The publisher still owns freeze,
    the full combined gate, and final remote-ref/inclusion verification.
    """
    if type(max_members) is not int or not 2 <= max_members <= 20:
        raise BundlePlanningError("max_members must be an integer from two to twenty")
    ignored = tuple(sorted(set(ignored_paths)))
    excluded = set(already_bundled) | set(in_flight)
    skipped: set[str] = set()
    remaining: dict[str, BundleCandidate] = {}
    refs: set[str] = set()
    bases: set[str] = set()
    for candidate in candidates:
        if candidate.task_id in excluded:
            skipped.add(candidate.task_id)
            continue
        if candidate.task_id in remaining:
            raise BundlePlanningError(f"Duplicate task ID: {candidate.task_id}")
        if candidate.ref in refs:
            raise BundlePlanningError(f"Duplicate task ref: {candidate.ref}")
        refs.add(candidate.ref)
        bases.add(candidate.base_sha)
        remaining[candidate.task_id] = candidate
    if len(bases) > 1:
        raise BundlePlanningError("Every candidate must use the same frozen base")
    _ordered(remaining.values())  # Detect cycles before bounded grouping can hide one.

    bundles: list[BundleGroup] = []
    while remaining:
        ids = sorted(remaining)
        group_ids: list[str] = []
        while len(group_ids) < max_members:
            choices = [task_id for task_id in ids if task_id not in group_ids
                       and (remaining[task_id].dependencies & remaining.keys()) <= set(group_ids)]
            if not choices:
                break
            def score(task_id):
                peers = group_ids or [other for other in ids if other != task_id]
                return (sum(_edge_weight(remaining[task_id], remaining[other], ignored)
                            for other in peers), task_id)
            group_ids.append(max(choices, key=score))
        group = _ordered(remaining[task_id] for task_id in group_ids)
        member_ids = {member.task_id for member in group}
        shared = sorted({path for index, left in enumerate(group) for right in group[index + 1:]
                         for path in _shared_paths(left, right, ignored)})
        dependencies = sorted({(dependency, member.task_id) for member in group
                               for dependency in member.dependencies & member_ids})
        bundles.append(BundleGroup(group, tuple(shared), tuple(dependencies)))
        for task_id in group_ids:
            remaining.pop(task_id)
    return BundlePlan(tuple(bundles), tuple(sorted(skipped)))
