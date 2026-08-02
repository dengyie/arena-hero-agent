from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .model import Position, Snapshot, Unit

RESOURCE_TTL_TICKS = 8
FRONTIER_STEP = 6
INITIAL_BAND_RADIUS = 9
BAND_INCREMENT = 6
MAX_BAND_RADIUS = 75
MAX_FRONTIER_CANDIDATES = 512
MAX_COMPLETED_TARGETS = 1024
MAX_FAILED_TARGETS = 1024
MAX_EVENT_IDS = 4096
PATH_NODE_CAP = 30_000
PATH_MARGIN = 40
SPAWN_MIN_RESOURCES = 12
SPAWN_DEPOSITS_REQUIRED = 3
SPAWN_WINDOW_TICKS = 20
MAX_ECONOMY_WORKERS = 3

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
    status: str
    path_length: int
    explored_nodes: int
    target: Position | None


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


@dataclass
class EventLedger:
    seen_ids: set[str] = field(default_factory=set)
    order: deque[str] = field(default_factory=deque)
    deposits: deque[int] = field(default_factory=deque)
    core_damage_ticks: deque[int] = field(default_factory=deque)

    def accept(self, event: dict[str, Any]) -> bool:
        event_id = str(event.get("event_id", ""))
        if not event_id:
            return True
        if event_id in self.seen_ids:
            return False
        self.seen_ids.add(event_id)
        self.order.append(event_id)
        while len(self.order) > MAX_EVENT_IDS:
            self.seen_ids.discard(self.order.popleft())
        return True

    def record_deposit(self, tick: int) -> None:
        self.deposits.append(tick)

    def record_damage(self, tick: int) -> None:
        self.core_damage_ticks.append(tick)

    def trim(self, tick: int) -> None:
        while self.deposits and self.deposits[0] < tick - SPAWN_WINDOW_TICKS:
            self.deposits.popleft()
        while self.core_damage_ticks and self.core_damage_ticks[0] < tick - SPAWN_WINDOW_TICKS:
            self.core_damage_ticks.popleft()


@dataclass
class ExplorationMemory:
    permanent_obstacles: set[Position] = field(default_factory=set)
    resources: dict[Position, ResourceObservation] = field(default_factory=dict)
    route_core: Position | None = None
    route_core_id: str | None = None
    band_radius: int = INITIAL_BAND_RADIUS
    frontier_candidates: deque[Position] = field(default_factory=deque)
    completed_targets: dict[Position, int] = field(default_factory=dict)
    failed_targets: dict[Position, tuple[int, int]] = field(default_factory=dict)
    active_targets: dict[str, Position] = field(default_factory=dict)
    active_target: Position | None = None  # compatibility/journal primary target
    policy_state: str = "BOOT"
    last_event_types: tuple[str, ...] = ()
    core_full: bool = False
    recovery_cooldown_until: int = 0
    ledger: EventLedger = field(default_factory=EventLedger)
    last_path: PathResult | None = None

    def _reset_for_core(self, state: Snapshot) -> None:
        self.route_core = state.core_position
        self.route_core_id = state.core_id
        self.band_radius = INITIAL_BAND_RADIUS
        self.frontier_candidates.clear()
        self.completed_targets.clear()
        self.failed_targets.clear()
        self.active_targets.clear()
        self.active_target = None
        self.core_full = False
        self.recovery_cooldown_until = 0

    def observe(self, state: Snapshot) -> None:
        core_changed = (
            self.route_core is not None
            and (self.route_core != state.core_position
                 or (self.route_core_id is not None and self.route_core_id != state.core_id))
        )
        if state.core_position is not None and (self.route_core is None or core_changed):
            self._reset_for_core(state)
        self.permanent_obstacles.update(state.obstacle_cells)
        if state.resources < state.resource_capacity:
            self.core_full = False
        for pos in state.resource_cells:
            self.resources[pos] = ResourceObservation(state.tick, "visible")
        for pos, observation in self.resources.items():
            if state.tick - observation.last_seen_tick >= RESOURCE_TTL_TICKS and pos not in state.resource_cells:
                observation.status = "stale"
        self.ledger.trim(state.tick)

    def apply_events(self, state: Snapshot) -> None:
        self.last_event_types = tuple(str(event.get("event_type")) for event in state.events)
        for event in state.events:
            if not self.ledger.accept(event):
                continue
            kind = str(event.get("event_type"))
            raw_pos = event.get("position")
            pos: Position | None = None
            if isinstance(raw_pos, (list, tuple)) and len(raw_pos) == 2:
                pos = int(raw_pos[0]), int(raw_pos[1])
            actor_id = str(event.get("actor_id", ""))
            if kind == "HARVEST_SUCCEEDED" and pos is not None:
                self.resources.setdefault(pos, ResourceObservation(0)).status = "depleted"
            elif kind == "HARVEST_FAILED" and pos is not None:
                obs = self.resources.setdefault(pos, ResourceObservation(0))
                obs.status = "failed"
                obs.failure_count += 1
                self.active_targets.pop(actor_id, None)
            elif kind == "DEPOSIT_FAILED":
                self.core_full = event.get("reason_code") == "CORE_RESOURCE_FULL"
            elif kind == "CORE_SPAWN_FAILED":
                # A Core-full recovery must not retry blindly while another
                # carrier/movement dependency still occupies the Core cell.
                self.recovery_cooldown_until = max(self.recovery_cooldown_until, state.tick + 4)
            elif kind == "DEPOSIT_SUCCEEDED":
                self.core_full = False
                self.ledger.record_deposit(state.tick)
                self.active_targets.pop(actor_id, None)
            elif kind == "UNIT_MOVE_FAILED":
                target = self.active_targets.pop(actor_id, None)
                if target is not None:
                    failures, _ = self.failed_targets.get(target, (0, 0))
                    self.failed_targets[target] = (failures + 1, state.tick + min(12, 2 + failures * 2))
            elif kind == "CORE_DAMAGED":
                self.ledger.record_damage(state.tick)
            elif kind == "CORE_DESTROYED":
                self.active_targets.clear()
                self.core_full = False

    def _trim(self) -> None:
        while len(self.completed_targets) > MAX_COMPLETED_TARGETS:
            self.completed_targets.pop(next(iter(self.completed_targets)))
        while len(self.failed_targets) > MAX_FAILED_TARGETS:
            self.failed_targets.pop(next(iter(self.failed_targets)))

    def _build_band(self, core: Position, radius: int) -> list[Position]:
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

    def _refill_frontier(self, state: Snapshot, *, force: bool = False) -> None:
        if state.core_position is None or (self.frontier_candidates and not force):
            return
        if force:
            self.frontier_candidates.clear()
        if self.band_radius <= MAX_BAND_RADIUS:
            self.frontier_candidates.extend(self._build_band(state.core_position, self.band_radius))
            self.band_radius += BAND_INCREMENT
        else:
            self.frontier_candidates.extend(self._build_band(state.core_position, MAX_BAND_RADIUS))
        while len(self.frontier_candidates) > MAX_FRONTIER_CANDIDATES:
            self.frontier_candidates.pop()

    def next_frontier(self, state: Snapshot, worker: Unit, obstacles: frozenset[Position],
                      occupied: set[Position], reserved: set[Position] | None = None) -> tuple[Position | None, PathResult]:
        reserved = reserved or set()
        self._refill_frontier(state)
        active = self.active_targets.get(worker.id)
        if active is not None and active not in reserved and active not in self.failed_targets:
            result = plan_path(worker.position, {active}, obstacles, occupied)
            if result.status == "FOUND":
                return active, result
            self.active_targets.pop(worker.id, None)
        candidates = list(self.frontier_candidates)
        self.frontier_candidates.clear()
        best: tuple[tuple[int, int, int, int, int], Position, PathResult] | None = None
        for target in candidates:
            failures, retry_after = self.failed_targets.get(target, (0, 0))
            if target in reserved or retry_after > state.tick or target in obstacles or target in occupied:
                self.frontier_candidates.append(target)
                continue
            result = plan_path(worker.position, {target}, obstacles, occupied)
            if result.status != "FOUND":
                self.failed_targets[target] = (failures + 1, state.tick + min(12, 2 + failures * 2))
                self.frontier_candidates.append(target)
                continue
            completed_tick = self.completed_targets.get(target, -1)
            age_penalty = 0 if completed_tick < 0 else 1
            # Deterministic coverage score: unvisited before revisits, then
            # true path length, then band progress and coordinate tie-break.
            score = (age_penalty, result.path_length, -self.band_radius, target[0], target[1])
            if best is None or score < best[0]:
                best = (score, target, result)
            else:
                self.frontier_candidates.append(target)
        if best is None:
            # A candidate batch can become permanently stale after accumulated
            # terrain observations or retry backoff. Drop it and advance one
            # deterministic band before declaring a genuine no-frontier state.
            self._refill_frontier(state, force=True)
            retry_candidates = list(self.frontier_candidates)
            self.frontier_candidates.clear()
            for target in retry_candidates:
                failures, retry_after = self.failed_targets.get(target, (0, 0))
                if target in reserved or retry_after > state.tick or target in obstacles or target in occupied:
                    self.frontier_candidates.append(target)
                    continue
                result = plan_path(worker.position, {target}, obstacles, occupied)
                if result.status != "FOUND":
                    self.failed_targets[target] = (failures + 1, state.tick + min(12, 2 + failures * 2))
                    self.frontier_candidates.append(target)
                    continue
                completed_tick = self.completed_targets.get(target, -1)
                score = (0 if completed_tick < 0 else 1, result.path_length,
                         -self.band_radius, target[0], target[1])
                if best is None or score < best[0]:
                    best = (score, target, result)
                else:
                    self.frontier_candidates.append(target)
            if best is None:
                self._trim()
                return None, PathResult(None, "NO_PATH", 0, 0, None)
        _, target, result = best
        self.active_targets[worker.id] = target
        self.active_target = target
        self._trim()
        return target, result

    def complete_frontier_if_reached(self, worker: Unit, tick: int) -> None:
        if self.active_targets.get(worker.id) == worker.position:
            self.completed_targets[worker.position] = tick
            self.active_targets.pop(worker.id, None)

    def visible_targets(self, state: Snapshot) -> list[Position]:
        return sorted((pos for pos in state.resource_cells
                       if self.resources.get(pos, ResourceObservation(state.tick)).status == "visible"),
                      key=lambda pos: (pos[0], pos[1]))

    def can_spawn_worker(self, state: Snapshot, *, allow_core_full_recovery: bool = False) -> bool:
        if state.core_position is None or (self.core_full and not allow_core_full_recovery):
            return False
        if len(state.workers) >= MAX_ECONOMY_WORKERS or state.resources < SPAWN_MIN_RESOURCES:
            return False
        if len(self.ledger.deposits) < SPAWN_DEPOSITS_REQUIRED or self.ledger.core_damage_ticks:
            return False
        # Core occupies one slot. Do not spawn while a Unit shares its cell.
        return all(unit.position != state.core_position for unit in state.units)


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
    path_length: int = 0
    band_radius: int = 0
    assignments: int = 0

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"unit_actions": self.unit_actions}
        if self.core_action is not None:
            result["core_action"] = self.core_action
        return result


def _plan(actions: dict[str, dict[str, Any]], memory: ExplorationMemory, *,
          policy_state: str, target: Position | None = None, waypoint: Position | None = None,
          path: PathResult | None = None, core_action: dict[str, Any] | None = None) -> Plan:
    memory.policy_state = policy_state
    memory.last_path = path
    return Plan(actions, core_action=core_action, policy_state=policy_state, active_target=target,
                waypoint=waypoint, last_event_types=memory.last_event_types,
                path_status=path.status if path else "NONE",
                path_nodes=path.explored_nodes if path else 0,
                path_length=path.path_length if path else 0,
                band_radius=memory.band_radius, assignments=len(memory.active_targets))


def economy_plan(state: Snapshot, memory: ExplorationMemory | None = None) -> Plan:
    memory = memory or ExplorationMemory()
    memory.observe(state)
    memory.apply_events(state)
    if state.status != "ACTIVE":
        return _plan({}, memory, policy_state="PAUSE")

    # Explicit safety default for non-Worker units. Manual actions still have
    # official priority over Agent actions on the same tick.
    actions = {unit.id: {"type": "WAIT"} for unit in state.units if unit.unit_type != "WORKER"}
    workers = sorted(state.workers, key=lambda unit: unit.id)
    if not workers:
        core_action = {"type": "SPAWN", "unit_type": "WORKER"} if memory.can_spawn_worker(state) else None
        return _plan(actions, memory, policy_state="SPAWN_BOOT" if core_action else "WAIT_NO_WORKER",
                     core_action=core_action)

    obstacles = frozenset(memory.permanent_obstacles | set(state.obstacle_cells))
    worker_positions = {worker.id: worker.position for worker in workers}
    resource_reserved: set[Position] = set()
    frontier_reserved: set[Position] = set()
    primary_state = "EXPLORE"
    primary_target: Position | None = None
    primary_path: PathResult | None = None
    recovery_worker_id: str | None = None

    # Core-full recovery is a Core-level transaction. At most one carrying
    # Worker can be the recovery leader; a second carrier must first vacate the
    # Core cell so the next Tick can legally create capacity.
    core_carriers = [worker for worker in workers
                     if worker.cargo > 0 and worker.position == state.core_position]
    recovery_leader = core_carriers[0] if len(core_carriers) == 1 else None
    core_evictor = core_carriers[-1] if len(core_carriers) > 1 else None

    # Cargo has absolute priority. It is resolved before all new resource work.
    for worker in workers:
        occupied = {pos for other_id, pos in worker_positions.items() if other_id != worker.id}
        if worker.cargo <= 0:
            continue
        if memory.core_full and state.core_position and worker.position != state.core_position:
            # While capacity recovery is in flight the Core cell is reserved.
            # Do not make additional carriers contest its two entity slots.
            actions[worker.id] = {"type": "WAIT"}
            if primary_state not in {"CORE_FULL_EVICT", "CORE_FULL_RECOVERY"}:
                primary_state = "CORE_FULL_HOLD"
            continue
        if state.core_position and worker.position == state.core_position:
            if memory.core_full:
                recovery_direction = next(
                    (direction for direction, delta in DIRECTIONS.items()
                     if (worker.position[0] + delta[0], worker.position[1] + delta[1]) not in obstacles
                     and (worker.position[0] + delta[0], worker.position[1] + delta[1]) not in occupied),
                    None,
                )
                if worker is core_evictor and recovery_direction:
                    actions[worker.id] = {"type": "MOVE", "direction": recovery_direction}
                    primary_state = "CORE_FULL_EVICT"
                    primary_target = state.core_position
                elif (worker is recovery_leader and recovery_direction
                        and state.tick >= memory.recovery_cooldown_until
                        and state.resources >= 5
                        and len(workers) < MAX_ECONOMY_WORKERS
                        and all(other.position != state.core_position
                                for other in workers if other.id != worker.id)
                        and not memory.ledger.core_damage_ticks):
                    actions[worker.id] = {"type": "MOVE", "direction": recovery_direction}
                    primary_state = "CORE_FULL_RECOVERY"
                    primary_target = state.core_position
                    recovery_worker_id = worker.id
                else:
                    actions[worker.id] = {"type": "WAIT"}
                    primary_state = "CORE_FULL"
            else:
                actions[worker.id] = {"type": "DEPOSIT"}
                primary_state = "DEPOSIT"
            continue
        path = plan_path(worker.position, {state.core_position} if state.core_position else set(), obstacles, occupied)
        actions[worker.id] = {"type": "MOVE", "direction": path.direction} if path.direction else {"type": "WAIT"}
        primary_state = "RETURN_CORE" if path.status == "FOUND" else "RETURN_BLOCKED"
        primary_target, primary_path = state.core_position, path

    empty_workers = [worker for worker in workers if worker.cargo <= 0]
    candidates: list[tuple[int, str, Position, Unit, PathResult]] = []
    for worker in empty_workers:
        occupied = {pos for other_id, pos in worker_positions.items() if other_id != worker.id}
        for resource in memory.visible_targets(state):
            path = plan_path(worker.position, {resource}, obstacles, occupied)
            if path.status in {"FOUND", "START_AT_GOAL"}:
                candidates.append((path.path_length, worker.id, resource, worker, path))
    assigned_workers: set[str] = set()
    for _, _, resource, worker, path in sorted(candidates, key=lambda item: (item[0], item[1], item[2])):
        if worker.id in assigned_workers or resource in resource_reserved:
            continue
        assigned_workers.add(worker.id)
        resource_reserved.add(resource)
        memory.active_targets[worker.id] = resource
        if path.status == "START_AT_GOAL":
            actions[worker.id] = {"type": "HARVEST"}
            primary_state = "HARVEST"
        else:
            actions[worker.id] = {"type": "MOVE", "direction": path.direction}
            primary_state = "TO_RESOURCE"
        primary_target, primary_path = resource, path

    for worker in empty_workers:
        if worker.id in assigned_workers:
            continue
        memory.complete_frontier_if_reached(worker, state.tick)
        occupied = {pos for other_id, pos in worker_positions.items() if other_id != worker.id}
        target, path = memory.next_frontier(state, worker, obstacles, occupied, frontier_reserved)
        if target is not None and path.direction:
            actions[worker.id] = {"type": "MOVE", "direction": path.direction}
            frontier_reserved.add(target)
            if primary_target is None:
                primary_state, primary_target, primary_path = "EXPLORE", target, path
        else:
            actions[worker.id] = {"type": "WAIT"}
            if primary_target is None:
                primary_state, primary_path = "NO_FRONTIER", path

    recovery_spawn = (
        recovery_worker_id is not None
        and len(workers) < MAX_ECONOMY_WORKERS
        and state.resources >= 5
        and not memory.ledger.core_damage_ticks
    )
    core_action = ({"type": "SPAWN", "unit_type": "WORKER"}
                   if recovery_spawn or memory.can_spawn_worker(state) else None)
    return _plan(actions, memory, policy_state=primary_state, target=primary_target,
                 waypoint=primary_target if primary_state == "EXPLORE" else None,
                 path=primary_path, core_action=core_action)
