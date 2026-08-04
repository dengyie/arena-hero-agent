from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from heapq import heappop, heappush

from .model import Position

PATH_NODE_CAP = 30_000
FRONTIER_PATH_NODE_CAP = 2_000
MAX_FRONTIER_PATH_EVALUATIONS = 8
PATH_MARGIN = 40
DIRECTIONS: dict[str, Position] = {
    "UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)
}


@dataclass(frozen=True)
class PathResult:
    direction: str | None
    status: str
    path_length: int
    explored_nodes: int
    target: Position | None


def step_position(position: Position, direction: str) -> Position:
    dx, dy = DIRECTIONS[direction]
    return position[0] + dx, position[1] + dy


def plan_path(start: Position, goals: set[Position], obstacles: frozenset[Position],
              occupied: set[Position], node_cap: int = PATH_NODE_CAP) -> PathResult:
    """Deterministic bounded cardinal BFS from current position to a goal."""
    if not goals:
        return PathResult(None, "NO_PATH", 0, 0, None)
    if start in goals:
        return PathResult(None, "START_AT_GOAL", 0, 0, start)
    points = set(goals) | set(obstacles) | set(occupied) | {start}
    min_x = min(x for x, _ in points) - PATH_MARGIN
    max_x = max(x for x, _ in points) + PATH_MARGIN
    min_y = min(y for _, y in points) - PATH_MARGIN
    max_y = max(y for _, y in points) + PATH_MARGIN
    queue = deque([(start, None, 0)])
    seen = {start}
    while queue:
        if len(seen) > node_cap:
            return PathResult(None, "NODE_CAP", 0, len(seen), None)
        current, first, distance = queue.popleft()
        for direction, delta in DIRECTIONS.items():
            nxt = current[0] + delta[0], current[1] + delta[1]
            if not (min_x <= nxt[0] <= max_x and min_y <= nxt[1] <= max_y):
                continue
            if nxt in seen or nxt in obstacles or (nxt in occupied and nxt not in goals):
                continue
            step = first or direction
            if nxt in goals:
                return PathResult(step, "FOUND", distance + 1, len(seen), nxt)
            seen.add(nxt)
            queue.append((nxt, step, distance + 1))
    return PathResult(None, "NO_PATH", 0, len(seen), None)



def plan_frontier_path(start: Position, goal: Position, obstacles: frozenset[Position],
                       occupied: set[Position], node_cap: int = FRONTIER_PATH_NODE_CAP) -> PathResult:
    """Deterministic bounded A* for one distant frontier waypoint."""
    if start == goal:
        return PathResult(None, "START_AT_GOAL", 0, 0, start)
    points = {goal} | set(obstacles) | set(occupied) | {start}
    min_x = min(x for x, _ in points) - PATH_MARGIN
    max_x = max(x for x, _ in points) + PATH_MARGIN
    min_y = min(y for _, y in points) - PATH_MARGIN
    max_y = max(y for _, y in points) + PATH_MARGIN

    def heuristic(pos: Position) -> int:
        return abs(pos[0] - goal[0]) + abs(pos[1] - goal[1])

    rank = {direction: index for index, direction in enumerate(DIRECTIONS)}
    queue: list[tuple[int, int, int, int, int, Position, str | None]] = []
    heappush(queue, (heuristic(start), 0, -1, start[0], start[1], start, None))
    distances = {start: 0}
    expanded = 0
    while queue:
        _, distance, _, _, _, current, first = heappop(queue)
        if distance != distances.get(current):
            continue
        expanded += 1
        if expanded > node_cap:
            return PathResult(None, "NODE_CAP", 0, expanded, None)
        for direction, delta in DIRECTIONS.items():
            nxt = current[0] + delta[0], current[1] + delta[1]
            if not (min_x <= nxt[0] <= max_x and min_y <= nxt[1] <= max_y):
                continue
            if nxt in obstacles or (nxt in occupied and nxt != goal):
                continue
            next_distance = distance + 1
            if next_distance >= distances.get(nxt, 10**12):
                continue
            step = first or direction
            if nxt == goal:
                return PathResult(step, "FOUND", next_distance, expanded, goal)
            distances[nxt] = next_distance
            heappush(queue, (next_distance + heuristic(nxt), next_distance, rank[step],
                             nxt[0], nxt[1], nxt, step))
    return PathResult(None, "NO_PATH", 0, expanded, None)


def first_step(start: Position, goals: set[Position], obstacles: frozenset[Position],
               occupied: set[Position]) -> str | None:
    return plan_path(start, goals, obstacles, occupied).direction
