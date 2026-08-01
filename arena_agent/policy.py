from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from .model import Position, Snapshot, Unit

DIRECTIONS: dict[str, Position] = {"UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)}

@dataclass(frozen=True)
class Plan:
    unit_actions: dict[str, dict[str, Any]]
    core_action: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"unit_actions": self.unit_actions}
        if self.core_action is not None:
            out["core_action"] = self.core_action
        return out


def first_step(start: Position, goals: set[Position], obstacles: frozenset[Position], occupied: set[Position]) -> str | None:
    if start in goals:
        return None
    q = deque([(start, None)])
    seen = {start}
    while q:
        cur, first = q.popleft()
        for direction, delta in DIRECTIONS.items():
            nxt = cur[0] + delta[0], cur[1] + delta[1]
            if nxt in seen or nxt in obstacles or (nxt in occupied and nxt not in goals):
                continue
            step = first or direction
            if nxt in goals:
                return step
            seen.add(nxt)
            q.append((nxt, step))
    return None


def economy_plan(state: Snapshot) -> Plan:
    actions: dict[str, dict[str, Any]] = {}
    if state.status != "ACTIVE":
        return Plan(actions)
    occupied = {u.position for u in state.units}
    workers = sorted(state.workers, key=lambda u: u.id)
    for worker in workers:
        if worker.cargo > 0:
            if state.core_position and worker.position == state.core_position:
                actions[worker.id] = {"type": "DEPOSIT"}
            elif state.core_position:
                direction = first_step(worker.position, {state.core_position}, state.obstacle_cells, occupied)
                actions[worker.id] = {"type": "MOVE", "direction": direction} if direction else {"type": "WAIT"}
            continue
        if worker.position in state.resource_cells:
            actions[worker.id] = {"type": "HARVEST"}
        else:
            direction = first_step(worker.position, set(state.resource_cells), state.obstacle_cells, occupied)
            actions[worker.id] = {"type": "MOVE", "direction": direction} if direction else {"type": "WAIT"}
    return Plan(actions)
