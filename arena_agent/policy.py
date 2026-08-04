from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .allocator import allocate_visible_resources
from .combat import ranger_actions
from .model import Position, Snapshot, Unit
from .path import (DIRECTIONS, FRONTIER_PATH_NODE_CAP, MAX_FRONTIER_PATH_EVALUATIONS,
                   PathResult, first_step, plan_frontier_path, plan_path, step_position)

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
RISK_WINDOW_TICKS = 20
WORKER_FRONTIER_RADII = (21, 33, 51, 75)
MAX_ECONOMY_WORKERS = 8
MAX_EXTERNAL_RECOVERY_POPULATION = 19
CAPACITY_RECOVERY_COOLDOWN = 4
TRAFFIC_EDGE_TTL = 2
TRAFFIC_CORE_TTL = 4
CORE_INGRESS_RADIUS = 3
CORE_DEFENSE_RESERVE = 10
MAX_CORE_HP = 5
MAX_CORE_SHIELD = 5
MAX_DYNAMIC_EDGES = 512
MAX_DYNAMIC_CELLS = 256

@dataclass
class ResourceObservation:
    last_seen_tick: int
    status: str = "visible"
    failure_count: int = 0


@dataclass
class EventLedger:
    seen_ids: set[str] = field(default_factory=set)
    order: deque[str] = field(default_factory=deque)
    deposits: deque[int] = field(default_factory=deque)
    core_damage_ticks: deque[int] = field(default_factory=deque)
    worker_damage_ticks: dict[str, int] = field(default_factory=dict)
    pending_harvests: dict[str, int] = field(default_factory=dict)
    deposit_latencies: deque[int] = field(default_factory=deque)
    upkeep_events: deque[dict[str, int]] = field(default_factory=deque)
    resource_overflow_amount: int = 0
    core_defense_events: deque[str] = field(default_factory=deque)
    combat_events: deque[dict[str, Any]] = field(default_factory=deque)
    combat_deaths: deque[dict[str, Any]] = field(default_factory=deque)

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

    def record_worker_damage(self, worker_id: str, tick: int) -> None:
        self.worker_damage_ticks[worker_id] = tick

    def record_harvest(self, worker_id: str, tick: int) -> None:
        self.pending_harvests[worker_id] = tick

    def record_deposit_latency(self, worker_id: str, tick: int) -> int | None:
        harvested_tick = self.pending_harvests.pop(worker_id, None)
        if harvested_tick is None:
            return None
        latency = max(0, tick - harvested_tick)
        self.deposit_latencies.append(latency)
        while len(self.deposit_latencies) > MAX_EVENT_IDS:
            self.deposit_latencies.popleft()
        return latency

    def trim(self, tick: int) -> None:
        while self.deposits and self.deposits[0] < tick - SPAWN_WINDOW_TICKS:
            self.deposits.popleft()
        while self.core_damage_ticks and self.core_damage_ticks[0] < tick - RISK_WINDOW_TICKS:
            self.core_damage_ticks.popleft()
        self.worker_damage_ticks = {
            worker_id: damaged_tick for worker_id, damaged_tick in self.worker_damage_ticks.items()
            if damaged_tick >= tick - RISK_WINDOW_TICKS
        }
        self.pending_harvests = {
            worker_id: harvested_tick for worker_id, harvested_tick in self.pending_harvests.items()
            if harvested_tick >= tick - RISK_WINDOW_TICKS
        }


@dataclass
class TrafficMemory:
    blocked_edges: dict[tuple[str, Position, str], int] = field(default_factory=dict)
    blocked_cells: dict[Position, int] = field(default_factory=dict)
    repeated_failures: dict[tuple[str, Position, str, str], int] = field(default_factory=dict)
    last_planned_edges: dict[str, tuple[Position, str]] = field(default_factory=dict)
    ingress_queue: tuple[str, ...] = ()
    holds: dict[str, str] = field(default_factory=dict)

    def trim(self, tick: int) -> None:
        self.blocked_edges = {key: until for key, until in self.blocked_edges.items() if until > tick}
        self.blocked_cells = {cell: until for cell, until in self.blocked_cells.items() if until > tick}
        while len(self.blocked_edges) > MAX_DYNAMIC_EDGES:
            self.blocked_edges.pop(next(iter(self.blocked_edges)))
        while len(self.blocked_cells) > MAX_DYNAMIC_CELLS:
            self.blocked_cells.pop(next(iter(self.blocked_cells)))
        self.holds.clear()

    def mark_failure(self, worker_id: str, position: Position, reason: str, tick: int) -> None:
        planned = self.last_planned_edges.get(worker_id)
        direction = planned[1] if planned is not None and planned[0] == position else ""
        ttl = TRAFFIC_CORE_TTL if reason == "CELL_UNIT_LIMIT" else TRAFFIC_EDGE_TTL
        if direction:
            self.blocked_edges[(worker_id, position, direction)] = tick + ttl
            repeat_key = (worker_id, position, direction, reason)
            self.repeated_failures[repeat_key] = self.repeated_failures.get(repeat_key, 0) + 1
        self.blocked_cells[position] = tick + ttl

    def mark_planned_move(self, worker_id: str, position: Position, direction: str) -> None:
        self.last_planned_edges[worker_id] = (position, direction)

    def is_edge_blocked(self, worker_id: str, position: Position, direction: str, tick: int) -> bool:
        return self.blocked_edges.get((worker_id, position, direction), 0) > tick


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
    frontier_failure_reasons: dict[str, int] = field(default_factory=dict)
    frontier_path_evaluations: int = 0
    frontier_path_nodes: int = 0
    frontier_worker_evaluations: dict[str, int] = field(default_factory=dict)
    active_targets: dict[str, Position] = field(default_factory=dict)
    active_target: Position | None = None
    policy_state: str = "BOOT"
    last_event_types: tuple[str, ...] = ()
    core_full: bool = False
    recovery_cooldown_until: int = 0
    ledger: EventLedger = field(default_factory=EventLedger)
    traffic: TrafficMemory = field(default_factory=TrafficMemory)
    allocation_count: int = 0
    allocation_total_cost: int = 0
    last_path: PathResult | None = None

    def _reset_for_core(self, state: Snapshot) -> None:
        self.route_core = state.core_position
        self.route_core_id = state.core_id
        self.band_radius = INITIAL_BAND_RADIUS
        self.frontier_candidates.clear()
        self.completed_targets.clear()
        self.failed_targets.clear()
        self.frontier_failure_reasons.clear()
        self.frontier_path_evaluations = 0
        self.frontier_path_nodes = 0
        self.frontier_worker_evaluations.clear()
        self.active_targets.clear()
        self.active_target = None
        self.core_full = False
        self.recovery_cooldown_until = 0
        self.allocation_count = 0
        self.allocation_total_cost = 0
        self.traffic = TrafficMemory()

    def observe(self, state: Snapshot) -> None:
        core_changed = self.route_core is not None and (
            self.route_core != state.core_position
            or (self.route_core_id is not None and self.route_core_id != state.core_id)
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
        self.traffic.trim(state.tick)

    def apply_events(self, state: Snapshot) -> None:
        self.last_event_types = tuple(str(event.get("event_type")) for event in state.events)
        for event in state.events:
            if not self.ledger.accept(event):
                continue
            kind = str(event.get("event_type"))
            raw_pos = event.get("position")
            pos = position_from_event(raw_pos)
            actor_id = str(event.get("actor_id", ""))
            if kind == "HARVEST_SUCCEEDED" and pos is not None:
                self.resources.setdefault(pos, ResourceObservation(0)).status = "depleted"
                self.ledger.record_harvest(actor_id, state.tick)
            elif kind == "HARVEST_FAILED" and pos is not None:
                obs = self.resources.setdefault(pos, ResourceObservation(0))
                obs.status = "failed"
                obs.failure_count += 1
                self.active_targets.pop(actor_id, None)
            elif kind == "DEPOSIT_FAILED":
                self.core_full = event.get("reason_code") == "CORE_RESOURCE_FULL"
            elif kind == "CORE_SPAWN_FAILED":
                self.recovery_cooldown_until = max(self.recovery_cooldown_until, state.tick + CAPACITY_RECOVERY_COOLDOWN)
            elif kind == "DEPOSIT_SUCCEEDED":
                self.core_full = False
                self.ledger.record_deposit(state.tick)
                self.ledger.record_deposit_latency(actor_id, state.tick)
                self.active_targets.pop(actor_id, None)
            elif kind == "UNIT_MOVE_FAILED" and pos is not None:
                reason = str(event.get("reason_code", "UNKNOWN"))
                self.traffic.mark_failure(actor_id, pos, reason, state.tick)
            elif kind == "UNIT_MOVE_SUCCEEDED":
                memory_edge = self.traffic.last_planned_edges.pop(actor_id, None)
                if memory_edge is not None:
                    self.traffic.blocked_edges.pop((actor_id, memory_edge[0], memory_edge[1]), None)
            elif kind in {"SWEEP_RESOLVED", "SHOT_HIT", "SHOT_MISSED", "DESTRUCTION_PARTICIPATION"}:
                self.ledger.combat_events.append({
                    "type": kind,
                    "actor_id": actor_id,
                    "target_id": str(event.get("target_id", "")) or None,
                    "targets_hit": int(event.get("values", {}).get("targets_hit", 0)),
                    "damage": int(event.get("values", {}).get("damage", 0)),
                })
                while len(self.ledger.combat_events) > MAX_EVENT_IDS:
                    self.ledger.combat_events.popleft()
            elif kind == "UNIT_DAMAGED" and event.get("reason_code") == "ATTACK":
                target_id = str(event.get("target_id", ""))
                self.ledger.record_worker_damage(target_id, state.tick)
                hp = int(event.get("values", {}).get("hp", -1))
                self.ledger.combat_events.append({
                    "type": "UNIT_DAMAGED", "target_id": target_id,
                    "damage": int(event.get("values", {}).get("damage", 0)), "hp": hp,
                })
                if hp == 0:
                    self.ledger.combat_deaths.append({"target_id": target_id, "tick": state.tick})
                while len(self.ledger.combat_events) > MAX_EVENT_IDS:
                    self.ledger.combat_events.popleft()
                while len(self.ledger.combat_deaths) > MAX_EVENT_IDS:
                    self.ledger.combat_deaths.popleft()
            elif kind == "UPKEEP_PAID":
                values = event.get("values", {})
                self.ledger.upkeep_events.append({key: int(values.get(key, 0)) for key in ("due", "paid", "deficit")})
                while len(self.ledger.upkeep_events) > MAX_EVENT_IDS:
                    self.ledger.upkeep_events.popleft()
            elif kind == "CORE_RESOURCE_OVERFLOW_DESTROYED":
                self.ledger.resource_overflow_amount += int(event.get("values", {}).get("amount", 0))
            elif kind in {"CORE_HEAL_SUCCEEDED", "CORE_REPAIR_SUCCEEDED", "CORE_HEAL_FAILED", "CORE_REPAIR_FAILED"}:
                self.ledger.core_defense_events.append(kind)
                while len(self.ledger.core_defense_events) > MAX_EVENT_IDS:
                    self.ledger.core_defense_events.popleft()
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
        radius = self.band_radius if self.band_radius <= MAX_BAND_RADIUS else MAX_BAND_RADIUS
        self.frontier_candidates.extend(self._build_band(state.core_position, radius))
        if self.band_radius <= MAX_BAND_RADIUS:
            self.band_radius += BAND_INCREMENT
        while len(self.frontier_candidates) > MAX_FRONTIER_CANDIDATES:
            self.frontier_candidates.pop()

    def frontier_retry_after(self, status: str, failures: int, tick: int) -> int:
        if status == "NODE_CAP":
            return tick + min(60, 8 * (failures + 1))
        return tick + min(12, 2 + failures * 2)

    def begin_frontier_budget(self) -> None:
        self.frontier_path_evaluations = 0
        self.frontier_path_nodes = 0
        self.frontier_worker_evaluations.clear()

    def frontier_path(self, worker_id: str, start: Position, target: Position, obstacles: frozenset[Position],
                      occupied: set[Position]) -> PathResult:
        used = self.frontier_worker_evaluations.get(worker_id, 0)
        if used >= MAX_FRONTIER_PATH_EVALUATIONS:
            return PathResult(None, "FRONTIER_BUDGET", 0, 0, None)
        self.frontier_worker_evaluations[worker_id] = used + 1
        self.frontier_path_evaluations += 1
        result = plan_frontier_path(start, target, obstacles, occupied, FRONTIER_PATH_NODE_CAP)
        self.frontier_path_nodes += result.explored_nodes
        return result

    def worker_frontier_radius(self, worker_index: int) -> int:
        return WORKER_FRONTIER_RADII[min(worker_index, len(WORKER_FRONTIER_RADII) - 1)]

    def next_frontier(self, state: Snapshot, worker: Unit, obstacles: frozenset[Position],
                      occupied: set[Position], reserved: set[Position] | None = None,
                      max_radius: int | None = None) -> tuple[Position | None, PathResult]:
        reserved = reserved or set()
        self._refill_frontier(state)

        def within_budget(target: Position) -> bool:
            return (max_radius is None or state.core_position is None
                    or max(abs(target[0] - state.core_position[0]), abs(target[1] - state.core_position[1])) <= max_radius)

        active = self.active_targets.get(worker.id)
        if active is not None and within_budget(active) and active not in reserved and active not in self.failed_targets:
            result = self.frontier_path(worker.id, worker.position, active, obstacles, occupied)
            if result.status in {"FOUND", "START_AT_GOAL"}:
                return active, result
            if result.status == "FRONTIER_BUDGET":
                return active, result
            self.active_targets.pop(worker.id, None)
        candidates = list(self.frontier_candidates)
        self.frontier_candidates.clear()
        best: tuple[tuple[int, int, int, int, int], Position, PathResult] | None = None
        for target in candidates:
            failures, retry_after = self.failed_targets.get(target, (0, 0))
            if (not within_budget(target) or target in reserved or retry_after > state.tick
                    or target in obstacles or target in occupied):
                self.frontier_candidates.append(target)
                continue
            result = self.frontier_path(worker.id, worker.position, target, obstacles, occupied)
            if result.status == "FRONTIER_BUDGET":
                self.frontier_candidates.appendleft(target)
                break
            if result.status not in {"FOUND", "START_AT_GOAL"}:
                self.failed_targets[target] = (failures + 1, self.frontier_retry_after(result.status, failures, state.tick))
                self.frontier_failure_reasons[result.status] = (
                    self.frontier_failure_reasons.get(result.status, 0) + 1
                )
                self.frontier_candidates.append(target)
                continue
            score = (0 if target not in self.completed_targets else 1, result.path_length,
                     -self.band_radius, target[0], target[1])
            if best is None or score < best[0]:
                best = (score, target, result)
            else:
                self.frontier_candidates.append(target)
        if best is None:
            self._refill_frontier(state, force=True)
            for target in list(self.frontier_candidates):
                if not within_budget(target) or target in reserved or target in obstacles or target in occupied:
                    continue
                result = self.frontier_path(worker.id, worker.position, target, obstacles, occupied)
                if result.status == "FRONTIER_BUDGET":
                    break
                if result.status in {"FOUND", "START_AT_GOAL"}:
                    score = (0 if target not in self.completed_targets else 1, result.path_length,
                             target[0], target[1])
                    if best is None or score < best[0]:
                        best = (score, target, result)
        if best is None and max_radius is not None and state.core_position is not None:
            for target in self._build_band(state.core_position, max_radius):
                if target in reserved or target in obstacles or target in occupied:
                    continue
                result = self.frontier_path(worker.id, worker.position, target, obstacles, occupied)
                if result.status == "FRONTIER_BUDGET":
                    break
                if result.status in {"FOUND", "START_AT_GOAL"}:
                    score = (target in self.completed_targets, result.path_length, target[0], target[1])
                    if best is None or score < best[0]:
                        best = (score, target, result)
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

    def can_spawn_worker(self, state: Snapshot) -> bool:
        if state.core_position is None or self.core_full:
            return False
        if len(state.workers) >= MAX_ECONOMY_WORKERS or state.resources < SPAWN_MIN_RESOURCES:
            return False
        if len(self.ledger.deposits) < SPAWN_DEPOSITS_REQUIRED or self.ledger.core_damage_ticks:
            return False
        return all(unit.position != state.core_position for unit in state.units)


def position_from_event(raw: Any) -> Position | None:
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return int(raw[0]), int(raw[1])
    return None


@dataclass(frozen=True)
class Plan:
    unit_actions: dict[str, dict[str, Any]
    ]
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
        output: dict[str, Any] = {"unit_actions": self.unit_actions}
        if self.core_action is not None:
            output["core_action"] = self.core_action
        return output


def vanguard_guard_actions(state: Snapshot) -> dict[str, dict[str, Any]]:
    actions: dict[str, dict[str, Any]] = {}
    for vanguard in state.vanguards:
        adjacent = [enemy for enemy in state.visible_enemies
                    if abs(enemy.position[0] - vanguard.position[0]) + abs(enemy.position[1] - vanguard.position[1]) == 1]
        if not adjacent:
            actions[vanguard.id] = {"type": "WAIT"}
            continue
        enemy = min(adjacent, key=lambda item: (item.kind != "UNIT", item.id))
        dx, dy = enemy.position[0] - vanguard.position[0], enemy.position[1] - vanguard.position[1]
        direction = next(name for name, delta in DIRECTIONS.items() if delta == (dx, dy))
        actions[vanguard.id] = {"type": "SWEEP", "direction": direction}
    return actions


def traffic_priority(worker: Unit, core: Position | None) -> tuple[int, str]:
    if worker.cargo > 0:
        return 0, worker.id
    if worker.hp is not None and worker.hp <= 1:
        return 1, worker.id
    return 2, worker.id


def choose_traffic_move(worker: Unit, target: Position, state: Snapshot, obstacles: frozenset[Position],
                         occupied: set[Position], reserved_destinations: set[Position],
                         reserved_edges: set[tuple[Position, Position]], memory: ExplorationMemory) -> PathResult:
    dynamic_cells = {cell for cell, until in memory.traffic.blocked_cells.items() if until > state.tick}
    blockers = obstacles | dynamic_cells
    candidates: list[tuple[int, int, str, PathResult]] = []
    for rank, direction in enumerate(DIRECTIONS):
        if memory.traffic.is_edge_blocked(worker.id, worker.position, direction, state.tick):
            continue
        nxt = step_position(worker.position, direction)
        if nxt in blockers or nxt in occupied or nxt in reserved_destinations or (nxt, worker.position) in reserved_edges:
            continue
        path = plan_path(nxt, {target}, blockers, occupied - {worker.position})
        if path.status in {"FOUND", "START_AT_GOAL"}:
            candidates.append((path.path_length, rank, direction, path))
    if not candidates:
        return PathResult(None, "TRAFFIC_HOLD", 0, 0, None)
    length, _, direction, path = min(candidates)
    return PathResult(direction, "FOUND", length + 1, path.explored_nodes, path.target)


def choose_core_defense_action(state: Snapshot, memory: ExplorationMemory) -> dict[str, Any] | None:
    """Use the sole Core action conservatively from current authoritative state."""
    if (memory.core_full or memory.ledger.core_damage_ticks or state.core_state not in {None, "NORMAL"}
            or state.resources < CORE_DEFENSE_RESERVE):
        return None
    if state.core_hp is not None and state.core_hp < MAX_CORE_HP:
        return {"type": "HEAL"}
    if state.core_shield is not None and state.core_shield < MAX_CORE_SHIELD:
        return {"type": "REPAIR_SHIELD"}
    return None


def _plan(actions: dict[str, dict[str, Any]], memory: ExplorationMemory, *, policy_state: str,
          target: Position | None = None, waypoint: Position | None = None,
          path: PathResult | None = None, core_action: dict[str, Any] | None = None) -> Plan:
    memory.policy_state = policy_state
    memory.last_path = path
    return Plan(actions, core_action, policy_state, target, waypoint, memory.last_event_types,
                path.status if path else "NONE", path.explored_nodes if path else 0,
                path.path_length if path else 0, memory.band_radius, len(memory.active_targets))


def economy_plan(state: Snapshot, memory: ExplorationMemory | None = None) -> Plan:
    memory = memory or ExplorationMemory()
    memory.observe(state)
    memory.apply_events(state)
    memory.allocation_count = 0
    memory.allocation_total_cost = 0
    memory.begin_frontier_budget()
    if state.status != "ACTIVE":
        return _plan({}, memory, policy_state="PAUSE")

    actions = {unit.id: {"type": "WAIT"} for unit in state.units if unit.unit_type not in {"WORKER", "RANGER"}}
    actions.update(vanguard_guard_actions(state))
    actions.update(ranger_actions(state))
    workers = sorted(state.workers, key=lambda unit: unit.id)
    if not workers:
        core_action = {"type": "SPAWN", "unit_type": "WORKER"} if memory.can_spawn_worker(state) else None
        return _plan(actions, memory, policy_state="SPAWN_BOOT" if core_action else "WAIT_NO_WORKER", core_action=core_action)

    obstacles = frozenset(memory.permanent_obstacles | set(state.obstacle_cells))
    positions = {worker.id: worker.position for worker in workers}
    reserved_destinations: set[Position] = set()
    reserved_edges: set[tuple[Position, Position]] = set()
    desired: dict[str, tuple[Unit, Position, str]] = {}
    core_action: dict[str, Any] | None = None
    primary_state, primary_target, primary_path = "EXPLORE", None, None

    # Core-full transaction remains above normal traffic scheduling.
    core_carriers = [w for w in workers if w.cargo > 0 and w.position == state.core_position]
    if memory.core_full and core_carriers:
        leader = core_carriers[0] if len(core_carriers) == 1 else None
        evictor = core_carriers[-1] if len(core_carriers) > 1 else None
        for worker in workers:
            actions[worker.id] = {"type": "WAIT"}
            if worker.cargo > 0 and worker.position != state.core_position:
                memory.traffic.holds[worker.id] = "CORE_FULL_HOLD"
        actor = evictor or leader
        if actor is not None:
            external_over_cap = len(workers) > MAX_ECONOMY_WORKERS
            recovery_allowed = (
                actor is leader and state.tick >= memory.recovery_cooldown_until
                and state.resources >= 5
                and not memory.ledger.core_damage_ticks
                and all(other.position != state.core_position for other in workers if other.id != actor.id)
            )
            recovery_ceiling_reached = state.population >= MAX_EXTERNAL_RECOVERY_POPULATION
            if actor is evictor or (recovery_allowed and not recovery_ceiling_reached):
                occupied = {pos for ident, pos in positions.items() if ident != actor.id}
                direction = next((d for d in DIRECTIONS if step_position(actor.position, d) not in obstacles | occupied), None)
                if direction:
                    actions[actor.id] = {"type": "MOVE", "direction": direction}
                    reserved_destinations.add(step_position(actor.position, direction))
                    reserved_edges.add((actor.position, step_position(actor.position, direction)))
                    if actor is evictor:
                        return _plan(actions, memory, policy_state="CORE_FULL_EVICT", target=state.core_position)
                    core_action = {"type": "SPAWN", "unit_type": "WORKER"}
                    policy_state = ("CORE_FULL_EXTERNAL_CAP_RECOVERY" if external_over_cap
                                    else "CORE_FULL_RECOVERY")
                    return _plan(actions, memory, policy_state=policy_state, target=state.core_position, core_action=core_action)
        if (actor is leader and recovery_allowed
                and state.population >= MAX_EXTERNAL_RECOVERY_POPULATION):
            return _plan(actions, memory, policy_state="CORE_FULL_EXTERNAL_CAP_HOLD",
                         target=state.core_position)
        return _plan(actions, memory, policy_state="CORE_FULL")

    # Task assignment: cargo, risk, visible resource, then bounded frontier.
    resource_reserved: set[Position] = set()
    frontier_reserved: set[Position] = set()
    for worker in workers:
        if worker.cargo > 0 and state.core_position:
            desired[worker.id] = (worker, state.core_position, "RETURN_CORE")
    for worker in workers:
        if worker.id in desired or worker.hp is None or worker.hp > 1 or state.core_position is None:
            continue
        desired[worker.id] = (worker, state.core_position, "RETURN_HEAL")
    empty = [w for w in workers if w.id not in desired]
    eligible_resources = [worker for worker in empty if worker.hp is None or worker.hp > 1]
    resource_assignments = allocate_visible_resources(
        eligible_resources,
        memory.visible_targets(state),
        lambda worker, resource: plan_path(
            worker.position, {resource}, obstacles, set(positions.values()) - {worker.position},
        ),
    )
    memory.allocation_count = len(resource_assignments)
    memory.allocation_total_cost = sum(assignment.cost for assignment in resource_assignments)
    for assignment in resource_assignments:
        worker = next(worker for worker in eligible_resources if worker.id == assignment.worker_id)
        desired[worker.id] = (worker, assignment.resource, "RESOURCE")
        resource_reserved.add(assignment.resource)
    for index, worker in enumerate(w for w in workers if w.id not in desired):
        memory.complete_frontier_if_reached(worker, state.tick)
        target, path = memory.next_frontier(state, worker, obstacles, set(positions.values()) - {worker.position},
                                            frontier_reserved, memory.worker_frontier_radius(index))
        if target is not None:
            desired[worker.id] = (worker, target, "EXPLORE")
            frontier_reserved.add(target)
        else:
            actions[worker.id] = {"type": "WAIT"}
            memory.traffic.holds[worker.id] = "NO_FRONTIER"

    # Core ingress queue for ordinary returns; only the nearest/UUID-stable carrier approaches Core.
    carriers = sorted((w for w, target, kind in desired.values() if kind == "RETURN_CORE"),
                      key=lambda w: (abs(w.position[0] - state.core_position[0]) + abs(w.position[1] - state.core_position[1]), w.id)) if state.core_position else []
    memory.traffic.ingress_queue = tuple(w.id for w in carriers)

    for worker, target, kind in sorted(desired.values(), key=lambda item: traffic_priority(item[0], state.core_position)):
        if (kind == "RETURN_CORE" and memory.traffic.ingress_queue
                and worker.id != memory.traffic.ingress_queue[0]
                and state.core_position is not None
                and abs(worker.position[0] - state.core_position[0]) + abs(worker.position[1] - state.core_position[1]) <= CORE_INGRESS_RADIUS):
            actions[worker.id] = {"type": "WAIT"}
            memory.traffic.holds[worker.id] = "CORE_INGRESS_HOLD"
            continue
        if worker.position == target:
            if kind == "RETURN_CORE":
                actions[worker.id] = {"type": "DEPOSIT"}
                primary_state, primary_target = "DEPOSIT", target
            elif kind == "RETURN_HEAL":
                actions[worker.id] = {"type": "HEAL"}
                primary_state, primary_target = "HEAL_WORKER", target
            elif kind == "RESOURCE":
                actions[worker.id] = {"type": "HARVEST"}
                primary_state, primary_target = "HARVEST", target
            continue
        occupied = set(positions.values()) - {worker.position}
        path = choose_traffic_move(worker, target, state, obstacles, occupied,
                                   reserved_destinations, reserved_edges, memory)
        if path.direction is None:
            actions[worker.id] = {"type": "WAIT"}
            memory.traffic.holds[worker.id] = path.status
            continue
        destination = step_position(worker.position, path.direction)
        actions[worker.id] = {"type": "MOVE", "direction": path.direction}
        reserved_destinations.add(destination)
        reserved_edges.add((worker.position, destination))
        memory.traffic.mark_planned_move(worker.id, worker.position, path.direction)
        if kind == "RETURN_CORE":
            primary_state, primary_target, primary_path = "RETURN_CORE", target, path
        elif kind == "RETURN_HEAL":
            primary_state, primary_target, primary_path = "RETURN_HEAL", target, path
        elif kind == "RESOURCE":
            primary_state, primary_target, primary_path = "TO_RESOURCE", target, path
        elif primary_target is None:
            primary_state, primary_target, primary_path = "EXPLORE", target, path

    if core_action is None:
        core_action = choose_core_defense_action(state, memory)
    if core_action is None and memory.can_spawn_worker(state):
        core_action = {"type": "SPAWN", "unit_type": "WORKER"}
    return _plan(actions, memory, policy_state=primary_state, target=primary_target,
                 waypoint=primary_target if primary_state == "EXPLORE" else None,
                 path=primary_path, core_action=core_action)
