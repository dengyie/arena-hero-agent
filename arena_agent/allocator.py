from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .model import Position, Unit
from .path import PathResult

MAX_CANDIDATES_PER_WORKER = 16
MAX_ASSIGNMENT_EDGES = 128
LOAD_PENALTY = 15
DISTANCE_COST = 100


@dataclass(frozen=True)
class ResourceAssignment:
    worker_id: str
    resource: Position
    path: PathResult
    cost: int


def _hungarian(costs: list[list[int]]) -> list[tuple[int, int]]:
    """Return a minimum-cost assignment for a rectangular matrix (rows <= columns)."""
    rows, cols = len(costs), len(costs[0]) if costs else 0
    u = [0] * (rows + 1)
    v = [0] * (cols + 1)
    matched = [0] * (cols + 1)
    previous = [0] * (cols + 1)
    for row in range(1, rows + 1):
        matched[0] = row
        col0 = 0
        minimum = [10**12] * (cols + 1)
        used = [False] * (cols + 1)
        while True:
            used[col0] = True
            active = matched[col0]
            delta, col1 = 10**12, 0
            for col in range(1, cols + 1):
                if used[col]:
                    continue
                value = costs[active - 1][col - 1] - u[active] - v[col]
                if value < minimum[col]:
                    minimum[col], previous[col] = value, col0
                if minimum[col] < delta:
                    delta, col1 = minimum[col], col
            for col in range(cols + 1):
                if used[col]:
                    u[matched[col]] += delta
                    v[col] -= delta
                else:
                    minimum[col] -= delta
            col0 = col1
            if matched[col0] == 0:
                break
        while True:
            prior = previous[col0]
            matched[col0] = matched[prior]
            col0 = prior
            if col0 == 0:
                break
    return [(matched[col] - 1, col - 1) for col in range(1, cols + 1) if matched[col]]


def allocate_visible_resources(
    workers: Iterable[Unit], resources: Iterable[Position], path_for: Callable[[Unit, Position], PathResult],
    worker_loads: dict[str, int] | None = None,
) -> tuple[ResourceAssignment, ...]:
    """Globally minimize current-visible resource assignment cost with bounded edges."""
    worker_loads = worker_loads or {}
    ordered_workers = tuple(sorted(workers, key=lambda worker: worker.id))
    ordered_resources = tuple(sorted(set(resources)))
    if not ordered_workers or not ordered_resources:
        return ()
    edges: dict[tuple[int, int], tuple[PathResult, int]] = {}
    for wi, worker in enumerate(ordered_workers):
        choices: list[tuple[int, int, PathResult]] = []
        for ri, resource in enumerate(ordered_resources):
            path = path_for(worker, resource)
            if path.status not in {"FOUND", "START_AT_GOAL"}:
                continue
            cost = path.path_length * DISTANCE_COST + worker_loads.get(worker.id, 0) * LOAD_PENALTY
            choices.append((cost, ri, path))
        for cost, ri, path in sorted(choices, key=lambda item: (item[0], ordered_resources[item[1]]))[:MAX_CANDIDATES_PER_WORKER]:
            edges[wi, ri] = path, cost
    if not edges:
        return ()
    resources_used = sorted({ri for _, ri in edges})
    # Add worker-specific unmatched columns at a dominating finite cost.
    unmatched_cost = 10**9
    columns = [("resource", ri) for ri in resources_used] + [("unmatched", wi) for wi in range(len(ordered_workers))]
    matrix: list[list[int]] = []
    for wi in range(len(ordered_workers)):
        row: list[int] = []
        for kind, index in columns:
            row.append(edges.get((wi, index), (None, unmatched_cost))[1] if kind == "resource" else unmatched_cost)
        matrix.append(row)
    assignments: list[ResourceAssignment] = []
    for wi, ci in _hungarian(matrix):
        kind, ri = columns[ci]
        if kind != "resource" or (wi, ri) not in edges:
            continue
        path, cost = edges[wi, ri]
        assignments.append(ResourceAssignment(ordered_workers[wi].id, ordered_resources[ri], path, cost))
    return tuple(sorted(assignments, key=lambda item: item.worker_id))
