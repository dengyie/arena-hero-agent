from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .model import Position

PATH_NODE_CAP = 30_000
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
              occupied: set[Position]) -> PathResult:
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
        if len(seen) > PATH_NODE_CAP:
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


def first_step(start: Position, goals: set[Position], obstacles: frozenset[Position],
               occupied: set[Position]) -> str | None:
    return plan_path(start, goals, obstacles, occupied).direction
