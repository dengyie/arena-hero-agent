from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .model import Position, Snapshot, Unit

RESOURCE_TTL_TICKS = 8
FRONTIER_STEP = 6  # approximately two Worker vision diameters
INITIAL_BAND_RADIUS = 9
BAND_INCREMENT = 6
MAX_BAND_RADIUS = 75
MAX_FRONTIER_CANDIDATES = 512
MAX_COMPLETED_TARGETS = 1024
MAX_FAILED_TARGETS = 1024
PATH_NODE_CAP = 30_000
PATH_MARGIN = 40

DIRECTIONS: dict[str, Position] = {
    "UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)
}


@dataclass
class ResourceObservation:
    last_seen_tick: int
    status: str = "visible"
    failure_count: int = 0


@dataclass(frozen=True)
class PathResult:
    direction: str | None
    status: str  # FOUND, START_AT_GOAL, NO_PATH, NODE_CAP
    explored_nodes: int
    target: Position | None


def plan_path(start: Position, goals: set[Position], obstacles: frozenset[Position],
              occupied: set[Position]) -> PathResult:
    """Bounded cardinal BFS from the actual current position to one target."""
    if not goals:
        return PathResult(None, "NO_PATH", 0, None)
    if start in goals:
        return PathResult(None, "START_AT_GOAL", 0, start)
    points = set(goals) | set(obstacles) | set(occupied) | {start}
    min_x = min(x for x, _ in points) - PATH_MARGIN
    max_x = max(x for x, _ in points) + PATH_MARGIN
    min_y = min(y for _, y in points) - PATH_MARGIN
    max_y = max(y for _, y in points) + PATH_MARGIN
    queue = deque([(start, None)])
    seen = {start}
    while queue:
        if len(seen) > PATH_NODE_CAP:
            return PathResult(None, "NODE_CAP", len(seen), None)
        current, first = queue.popleft()
        for direction, delta in DIRECTIONS.items():
            nxt = current[0] + delta[0], current[1] + delta[1]
            if not (min_x <= nxt[0] <= max_x and min_y <= nxt[1] <= max_y):
                continue
            if nxt in seen or nxt in obstacles or (nxt in occupied and nxt not in goals):
                continue
            step = first or direction
            if nxt in goals:
                return PathResult(step, "FOUND", len(seen), nxt)
            seen.add(nxt)
            queue.append((nxt, step))
    return PathResult(None, "NO_PATH", len(seen), None)


def first_step(start: Position, goals: set[Position], obstacles: frozenset[Position],
               occupied: set[Position]) -> str | None:
    """Compatibility wrapper used by focused path tests."""
    return plan_path(start, goals, obstacles, occupied).direction


@dataclass
class ExplorationMemory:
    permanent_obstacles: set[Position] = field(default_factory=set)
    resources: dict[Position, ResourceObservation] = field(default_factory=dict)
    route_core: Position | None = None
    band_radius: int = INITIAL_BAND_RADIUS
    frontier_candidates: deque[Position] = field(default_factory=deque)
    completed_targets: dict[Position, int] = field(default_factory=dict)
    failed_targets: dict[Position, tuple[int, int]] = field(default_factory=dict)
    active_target: Position | None = None
    policy_state: str = "BOOT"
    last_event_types: tuple[str, ...] = ()
    core_full: bool = False
    last_path: PathResult | None = None

    def observe(self, state: Snapshot) -> None:
        self.permanent_obstacles.update(state.obstacle_cells)
        # A state snapshot is authoritative. A newly respawned/replaced Core
        # may have spare capacity without producing a DEPOSIT_SUCCEEDED event.
        if state.resources < state.resource_capacity:
            self.core_full = False
        for pos in state.resource_cells:
            self.resources[pos] = ResourceObservation(state.tick, "visible")
        for pos, observation in self.resources.items():
            if state.tick - observation.last_seen_tick >= RESOURCE_TTL_TICKS and pos not in state.resource_cells:
                observation.status = "stale"

    def apply_events(self, events: tuple[dict[str, Any], ...]) -> None:
        self.last_event_types = tuple(str(event.get("event_type")) for event in events)
        for event in events:
            kind = str(event.get("event_type"))
            raw_pos = event.get("position")
            if not isinstance(raw_pos, list | tuple) or len(raw_pos) != 2:
                continue
            pos = int(raw_pos[0]), int(raw_pos[1])
            if kind == "HARVEST_SUCCEEDED":
                self.resources.setdefault(pos, ResourceObservation(0)).status = "depleted"
            elif kind == "HARVEST_FAILED":
                obs = self.resources.setdefault(pos, ResourceObservation(0))
                obs.status = "failed"
                obs.failure_count += 1
            elif kind == "DEPOSIT_FAILED":
                self.core_full = event.get("reason_code") == "CORE_RESOURCE_FULL"
            elif kind == "DEPOSIT_SUCCEEDED":
                self.core_full = False
            elif kind == "UNIT_MOVE_FAILED" and self.active_target is not None:
                failures, _ = self.failed_targets.get(self.active_target, (0, 0))
                # Dynamic movement failure is not a permanent obstacle. Backoff
                # this target briefly and force a fresh start→goal plan next tick.
                self.failed_targets[self.active_target] = (failures + 1, 0)
                self.active_target = None

    def _trim(self) -> None:
        while len(self.completed_targets) > MAX_COMPLETED_TARGETS:
            self.completed_targets.pop(next(iter(self.completed_targets)))
        while len(self.failed_targets) > MAX_FAILED_TARGETS:
            self.failed_targets.pop(next(iter(self.failed_targets)))

    def _build_band(self, core: Position, radius: int) -> list[Position]:
        """Stable serpentine ring with coverage centers every FRONTIER_STEP."""
        points: list[Position] = []
        for x in range(core[0] - radius, core[0] + radius + 1, FRONTIER_STEP):
            points.append((x, core[1] - radius))
        for y in range(core[1] - radius + FRONTIER_STEP, core[1] + radius + 1, FRONTIER_STEP):
            points.append((core[0] + radius, y))
        for x in range(core[0] + radius - FRONTIER_STEP, core[0] - radius - 1, -FRONTIER_STEP):
            points.append((x, core[1] + radius))
        for y in range(core[1] + radius - FRONTIER_STEP, core[1] - radius, -FRONTIER_STEP):
            points.append((core[0] - radius, y))
        return list(dict.fromkeys(points))

    def _reset_for_core(self, core: Position) -> None:
        self.route_core = core
        self.band_radius = INITIAL_BAND_RADIUS
        self.frontier_candidates.clear()
        self.completed_targets.clear()
        self.failed_targets.clear()
        self.active_target = None

    def _refill_frontier(self, state: Snapshot) -> None:
        core = state.core_position
        if core is None:
            return
        if self.route_core != core:
            self._reset_for_core(core)
        if self.frontier_candidates:
            return
        if self.band_radius <= MAX_BAND_RADIUS:
            self.frontier_candidates.extend(self._build_band(core, self.band_radius))
            self.band_radius += BAND_INCREMENT
        else:
            # Keep patrolling at the legal maximum radius. Oldest completed
            # sectors become candidates again instead of terminal WAIT.
            self.frontier_candidates.extend(self._build_band(core, MAX_BAND_RADIUS))
        while len(self.frontier_candidates) > MAX_FRONTIER_CANDIDATES:
            self.frontier_candidates.pop()

    def next_frontier(self, state: Snapshot, worker: Unit, obstacles: frozenset[Position],
                      occupied: set[Position]) -> tuple[Position | None, PathResult]:
        self._refill_frontier(state)
        if self.active_target is not None and self.active_target not in self.failed_targets:
            result = plan_path(worker.position, {self.active_target}, obstacles, occupied)
            if result.status == "FOUND":
                return self.active_target, result
            self.failed_targets[self.active_target] = (1, state.tick + 3)
            self.active_target = None
        candidates = list(self.frontier_candidates)
        self.frontier_candidates.clear()
        best: tuple[tuple[int, int, int, int], Position, PathResult] | None = None
        for target in candidates:
            failure_count, retry_after = self.failed_targets.get(target, (0, 0))
            if retry_after > state.tick or target in obstacles or target in occupied:
                self.frontier_candidates.append(target)
                continue
            result = plan_path(worker.position, {target}, obstacles, occupied)
            if result.status != "FOUND":
                self.failed_targets[target] = (failure_count + 1, state.tick + min(12, 2 + failure_count * 2))
                self.frontier_candidates.append(target)
                continue
            # Prefer never-completed candidates and short continuous routes.
            completed_tick = self.completed_targets.get(target, -1)
            score = (0 if completed_tick < 0 else 1, result.explored_nodes, target[0], target[1])
            if best is None or score < best[0]:
                best = (score, target, result)
            else:
                self.frontier_candidates.append(target)
        if best is None:
            self._trim()
            return None, PathResult(None, "NO_PATH", 0, None)
        _, target, result = best
        self.active_target = target
        self._trim()
        return target, result

    def complete_frontier_if_reached(self, worker: Unit, tick: int) -> None:
        if self.active_target == worker.position:
            self.completed_targets[worker.position] = tick
            self.active_target = None

    def visible_targets(self, state: Snapshot) -> list[Position]:
        return sorted(
            (pos for pos in state.resource_cells
             if self.resources.get(pos, ResourceObservation(state.tick)).status == "visible"),
            key=lambda pos: (pos[0], pos[1]),
        )


@dataclass(frozen=True)
class Plan:
    unit_actions: dict[str, dict[str, Any]]
    core_action: dict[str, Any] | None = None
    policy_state: str = "BOOT"
    active_target: Position | None = None
    waypoint: Position | None = None
    last_event_types: tuple[str, ...] = ()
    path_status: str = "NONE"
    path_nodes: int = 0
    band_radius: int = 0

    def as_dict(self) -> dict[str, Any]:
        plan: dict[str, Any] = {"unit_actions": self.unit_actions}
        if self.core_action is not None:
            plan["core_action"] = self.core_action
        return plan


def _plan(actions: dict[str, dict[str, Any]], memory: ExplorationMemory, *,
          policy_state: str, target: Position | None = None,
          waypoint: Position | None = None, path: PathResult | None = None) -> Plan:
    memory.policy_state = policy_state
    memory.last_path = path
    return Plan(
        actions, policy_state=policy_state, active_target=target, waypoint=waypoint,
        last_event_types=memory.last_event_types,
        path_status=path.status if path else "NONE",
        path_nodes=path.explored_nodes if path else 0,
        band_radius=memory.band_radius,
    )


def economy_plan(state: Snapshot, memory: ExplorationMemory | None = None) -> Plan:
    memory = memory or ExplorationMemory()
    memory.observe(state)
    memory.apply_events(state.events)
    actions: dict[str, dict[str, Any]] = {}
    if state.status != "ACTIVE":
        return _plan(actions, memory, policy_state="PAUSE")
    workers = sorted(state.workers, key=lambda unit: unit.id)
    if not workers:
        return _plan(actions, memory, policy_state="WAIT_NO_WORKER")

    # Phase 1/2 retains the existing single Worker economy behavior. Phase 3
    # will allocate all Workers. Other units are deliberately omitted here so
    # the server applies its documented WAIT default.
    worker = workers[0]
    occupied = {unit.position for unit in state.units if unit.id != worker.id}
    known_obstacles = frozenset(memory.permanent_obstacles | set(state.obstacle_cells))

    if worker.cargo > 0:
        if state.core_position and worker.position == state.core_position:
            if memory.core_full:
                return _plan({worker.id: {"type": "WAIT"}}, memory,
                             policy_state="CORE_FULL", target=state.core_position)
            return _plan({worker.id: {"type": "DEPOSIT"}}, memory,
                         policy_state="DEPOSIT", target=state.core_position)
        path = plan_path(worker.position, {state.core_position} if state.core_position else set(),
                         known_obstacles, occupied)
        action = {"type": "MOVE", "direction": path.direction} if path.direction else {"type": "WAIT"}
        state_name = "RETURN_CORE" if path.status == "FOUND" else "RETURN_BLOCKED"
        return _plan({worker.id: action}, memory, policy_state=state_name,
                     target=state.core_position, path=path)

    targets = memory.visible_targets(state)
    if targets:
        target_paths = [(target, plan_path(worker.position, {target}, known_obstacles, occupied)) for target in targets]
        reachable = [(target, path) for target, path in target_paths if path.status in {"FOUND", "START_AT_GOAL"}]
        if reachable:
            target, path = min(reachable, key=lambda pair: (
                abs(pair[0][0] - worker.position[0]) + abs(pair[0][1] - worker.position[1]), pair[0]))
            memory.active_target = target
            if path.status == "START_AT_GOAL":
                return _plan({worker.id: {"type": "HARVEST"}}, memory,
                             policy_state="HARVEST", target=target, path=path)
            return _plan({worker.id: {"type": "MOVE", "direction": path.direction}}, memory,
                         policy_state="TO_RESOURCE", target=target, path=path)
        for target, path in target_paths:
            failures, _ = memory.failed_targets.get(target, (0, 0))
            memory.failed_targets[target] = (failures + 1, state.tick + 6)

    memory.complete_frontier_if_reached(worker, state.tick)
    target, path = memory.next_frontier(state, worker, known_obstacles, occupied)
    if target is not None and path.direction:
        return _plan({worker.id: {"type": "MOVE", "direction": path.direction}}, memory,
                     policy_state="EXPLORE", waypoint=target, path=path)
    # A bounded and explicit idle result is only possible when no legal
    # frontier is currently reachable; next Tick refills/re-evaluates it.
    return _plan({worker.id: {"type": "WAIT"}}, memory,
                 policy_state="NO_FRONTIER", path=path)
