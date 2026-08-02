from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .model import Position, Snapshot, Unit

RESOURCE_TTL_TICKS = 8
WAYPOINT_RADII = (3, 5, 7, 9, 11, 13, 15)
DIRECTIONS: dict[str, Position] = {
    "UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)
}


@dataclass
class ResourceObservation:
    last_seen_tick: int
    status: str = "visible"
    failure_count: int = 0


@dataclass
class ExplorationMemory:
    permanent_obstacles: set[Position] = field(default_factory=set)
    resources: dict[Position, ResourceObservation] = field(default_factory=dict)
    waypoint_index: int = 0
    waypoints: list[Position] = field(default_factory=list)
    active_target: Position | None = None
    policy_state: str = "BOOT"
    last_event_types: tuple[str, ...] = ()

    def observe(self, state: Snapshot) -> None:
        self.permanent_obstacles.update(state.obstacle_cells)
        visible = set(state.resource_cells)
        for pos in visible:
            self.resources[pos] = ResourceObservation(state.tick, "visible")
        for pos, obs in self.resources.items():
            if state.tick - obs.last_seen_tick >= RESOURCE_TTL_TICKS and pos not in visible:
                obs.status = "stale"

    def apply_events(self, events: tuple[dict[str, Any], ...]) -> None:
        self.last_event_types = tuple(str(e.get("event_type")) for e in events)
        for event in events:
            kind = event.get("event_type")
            raw_pos = event.get("position", ())
            pos = tuple(raw_pos)
            if len(pos) != 2:
                continue
            pos = (int(pos[0]), int(pos[1]))
            if kind == "HARVEST_SUCCEEDED":
                self.resources.setdefault(pos, ResourceObservation(0)).status = "depleted"
            elif kind == "HARVEST_FAILED":
                obs = self.resources.setdefault(pos, ResourceObservation(0))
                obs.status = "failed"
                obs.failure_count += 1
            elif kind == "UNIT_MOVE_FAILED":
                self.waypoint_index += 1

    def ensure_waypoints(self, core: Position | None) -> None:
        if core is None or self.waypoints:
            return
        for radius in WAYPOINT_RADII:
            self.waypoints.extend([
                (core[0], core[1] - radius),
                (core[0] + radius, core[1]),
                (core[0], core[1] + radius),
                (core[0] - radius, core[1]),
            ])

    def next_waypoint(self, state: Snapshot, worker: Unit) -> Position | None:
        self.ensure_waypoints(state.core_position)
        occupied = {u.position for u in state.units if u.id != worker.id}
        while self.waypoint_index < len(self.waypoints):
            candidate = self.waypoints[self.waypoint_index]
            if candidate in self.permanent_obstacles or candidate in occupied:
                self.waypoint_index += 1
                continue
            self.active_target = candidate
            return candidate
        self.active_target = None
        return None

    def visible_targets(self, state: Snapshot) -> list[Position]:
        return sorted(
            (p for p in state.resource_cells
             if self.resources.get(p, ResourceObservation(state.tick)).status in {"visible", "targeted"}),
            key=lambda p: (p[0], p[1]),
        )


@dataclass(frozen=True)
class Plan:
    unit_actions: dict[str, dict[str, Any]]
    core_action: dict[str, Any] | None = None
    policy_state: str = "BOOT"
    active_target: Position | None = None
    waypoint: Position | None = None
    last_event_types: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"unit_actions": self.unit_actions}
        if self.core_action is not None:
            out["core_action"] = self.core_action
        return out


def first_step(start: Position, goals: set[Position], obstacles: frozenset[Position], occupied: set[Position]) -> str | None:
    if start in goals:
        return None
    points = set(goals) | set(obstacles) | set(occupied) | {start}
    min_x = min(x for x, _ in points) - 32
    max_x = max(x for x, _ in points) + 32
    min_y = min(y for _, y in points) - 32
    max_y = max(y for _, y in points) + 32
    q = deque([(start, None)])
    seen = {start}
    while q and len(seen) <= 20_000:
        cur, first = q.popleft()
        for direction, delta in DIRECTIONS.items():
            nxt = cur[0] + delta[0], cur[1] + delta[1]
            if not (min_x <= nxt[0] <= max_x and min_y <= nxt[1] <= max_y):
                continue
            if nxt in seen or nxt in obstacles or (nxt in occupied and nxt not in goals):
                continue
            step = first or direction
            if nxt in goals:
                return step
            seen.add(nxt)
            q.append((nxt, step))
    return None


def economy_plan(state: Snapshot, memory: ExplorationMemory | None = None) -> Plan:
    memory = memory or ExplorationMemory()
    memory.observe(state)
    memory.apply_events(state.events)
    actions: dict[str, dict[str, Any]] = {}
    if state.status != "ACTIVE":
        memory.policy_state = "PAUSE"
        return Plan(actions, policy_state=memory.policy_state, last_event_types=memory.last_event_types)
    workers = sorted(state.workers, key=lambda u: u.id)
    if not workers:
        memory.policy_state = "WAIT_NO_WORKER"
        return Plan(actions, policy_state=memory.policy_state, last_event_types=memory.last_event_types)
    worker = workers[0]
    occupied = {u.position for u in state.units if u.id != worker.id}

    if worker.cargo > 0:
        if state.core_position and worker.position == state.core_position:
            memory.policy_state = "DEPOSIT"
            actions[worker.id] = {"type": "DEPOSIT"}
            return Plan(actions, policy_state=memory.policy_state, active_target=state.core_position,
                        last_event_types=memory.last_event_types)
        memory.policy_state = "RETURN_CORE"
        direction = first_step(worker.position, {state.core_position} if state.core_position else set(),
                               state.obstacle_cells, occupied)
        actions[worker.id] = {"type": "MOVE", "direction": direction} if direction else {"type": "WAIT"}
        return Plan(actions, policy_state=memory.policy_state, active_target=state.core_position,
                    last_event_types=memory.last_event_types)

    visible_targets = memory.visible_targets(state)
    if visible_targets:
        target = min(visible_targets, key=lambda p: (abs(p[0] - worker.position[0]) + abs(p[1] - worker.position[1]), p))
        memory.active_target = target
        if worker.position == target:
            memory.policy_state = "HARVEST"
            actions[worker.id] = {"type": "HARVEST"}
        else:
            memory.policy_state = "TO_RESOURCE"
            direction = first_step(worker.position, {target}, state.obstacle_cells, occupied)
            actions[worker.id] = {"type": "MOVE", "direction": direction} if direction else {"type": "WAIT"}
        return Plan(actions, policy_state=memory.policy_state, active_target=target,
                    last_event_types=memory.last_event_types)

    waypoint = memory.next_waypoint(state, worker)
    if waypoint is not None:
        if worker.position == waypoint:
            memory.waypoint_index += 1
            waypoint = memory.next_waypoint(state, worker)
        memory.policy_state = "EXPLORE"
        direction = first_step(worker.position, {waypoint} if waypoint else set(), memory.permanent_obstacles, occupied)
        actions[worker.id] = {"type": "MOVE", "direction": direction} if direction else {"type": "WAIT"}
        return Plan(actions, policy_state=memory.policy_state, waypoint=waypoint,
                    last_event_types=memory.last_event_types)

    memory.policy_state = "EXPLORATION_EXHAUSTED"
    actions[worker.id] = {"type": "WAIT"}
    return Plan(actions, policy_state=memory.policy_state, last_event_types=memory.last_event_types)
