from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

from .allocator import allocate_visible_resources
from .combat import (guard_slots, ranger_actions, ranger_firing_cells, select_ranger_decision,
                     select_vanguard_decision)
from .model import Position, Snapshot, Unit
from .rules import (
    CORE_RESOURCE_CAPACITY_PER_UNIT,
    CORE_RESOURCE_MINIMUM_CAPACITY,
    UNIT_BASE_COSTS,
    core_resource_capacity,
    unit_production_cost,
)
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
COMBAT_DAMAGE_COOLDOWN_TICKS = 12
COMBAT_LOSS_COOLDOWN_TICKS = 40
COMBAT_EPISODE_IDLE_TICKS = 8
WORKER_FRONTIER_RADII = (21, 33, 51, 75)
MAX_ECONOMY_WORKERS = 8
MAX_EXTERNAL_RECOVERY_POPULATION = 19
COMBAT_POPULATION_CEILING = 20
COMBAT_UPKEEP_RESERVE_TICKS = 20
CAPACITY_RECOVERY_COOLDOWN = 4
TRAFFIC_EDGE_TTL = 2
TRAFFIC_CORE_TTL = 4
CORE_INGRESS_RADIUS = 3
CORE_INGRESS_STALL_TICKS = 4
CORE_DEFENSE_RESERVE = 10
MAX_CORE_HP = 5
MAX_CORE_SHIELD = 5
MAX_DYNAMIC_EDGES = 512
MAX_DYNAMIC_CELLS = 256
FRONTIER_COMPLETION_COOLDOWN_TICKS = 16
MAX_FALLBACK_FRONTIER_CANDIDATES = 8
THREAT_RADIUS = 3
CORE_GUARD_RADIUS = 3
COMBAT_RESERVE = 20
COMBAT_SPAWN_COOLDOWN = 4
COMBAT_SPAWN_TIMEOUT = 4
ESCORT_LEASE_TICKS = 120
COMBAT_PRODUCTION_MODES = {"production", "positioning", "live-sweep", "live-precision", "live-cell", "live"}
COMBAT_MOVEMENT_MODES = {"positioning", "live-sweep", "live-precision", "live-cell", "live"}
def upkeep_for_population(population: int) -> int:
    tier = max(0, population) // 20
    return tier * (tier + 1) // 2


@dataclass
class PendingCombatSpawn:
    unit_type: str
    requested_tick: int
    prior_ids: frozenset[str]
    accepted_tick: int | None = None


@dataclass
class CombatMemory:
    home_vanguard_id: str | None = None
    home_ranger_id: str | None = None
    pending_spawn: PendingCombatSpawn | None = None
    spawn_cooldown_until: int = 0
    last_spawn_result: str | None = None
    last_role_transition: str | None = None
    escort_worker_id: str | None = None
    escort_until_tick: int = 0
    escort_cooldown_until: int = 0

    def reconcile_escort(self, state: Snapshot, threatened_worker_ids: set[str]) -> None:
        workers = {worker.id: worker for worker in state.workers}
        current = workers.get(self.escort_worker_id or "")
        if (current is None or current.cargo > 0 or (current.hp is not None and current.hp < 2)
                or state.tick >= self.escort_until_tick
                or (state.core_position is not None
                    and manhattan(current.position, state.core_position) <= CORE_GUARD_RADIUS)):
            if self.escort_worker_id is not None:
                self.escort_cooldown_until = max(
                    self.escort_cooldown_until, state.tick + ESCORT_LEASE_TICKS,
                )
            self.escort_worker_id = None
        defenders = [
            unit for unit in state.units
            if unit.id in {self.home_vanguard_id, self.home_ranger_id}
        ]
        defenders_home = (
            state.core_position is not None
            and all(manhattan(unit.position, state.core_position) <= CORE_GUARD_RADIUS
                    for unit in defenders)
        )
        if (self.escort_worker_id is None and state.tick >= self.escort_cooldown_until
                and defenders_home):
            candidates = sorted(
                (worker for worker in state.workers
                 if worker.id in threatened_worker_ids and worker.cargo == 0
                 and (worker.hp is None or worker.hp >= 2)
                 and state.core_position is not None
                 and manhattan(worker.position, state.core_position) > CORE_GUARD_RADIUS),
                key=lambda worker: worker.id,
            )
            if candidates:
                self.escort_worker_id = candidates[0].id
                self.escort_until_tick = state.tick + ESCORT_LEASE_TICKS

    def request_spawn(self, unit_type: str, tick: int, units: tuple[Unit, ...]) -> None:
        self.pending_spawn = PendingCombatSpawn(
            unit_type, tick, frozenset(unit.id for unit in units if unit.unit_type == unit_type)
        )
        self.last_spawn_result = "REQUESTED"

    def mark_spawn_failed(self, tick: int) -> None:
        self.pending_spawn = None
        self.spawn_cooldown_until = max(self.spawn_cooldown_until, tick + COMBAT_SPAWN_COOLDOWN)
        self.last_spawn_result = "FAILED"

    def mark_spawn_accepted(self, tick: int) -> None:
        if self.pending_spawn is not None and self.pending_spawn.requested_tick == tick:
            self.pending_spawn.accepted_tick = tick
            self.last_spawn_result = "ACCEPTED"

    def reconcile_roles(self, units: tuple[Unit, ...], tick: int) -> None:
        by_type = {
            unit_type: sorted((unit for unit in units if unit.unit_type == unit_type), key=lambda unit: unit.id)
            for unit_type in ("VANGUARD", "RANGER")
        }
        ids = {unit.id for unit in units}
        if self.home_vanguard_id not in ids:
            self.home_vanguard_id = None
        if self.home_ranger_id not in ids:
            self.home_ranger_id = None
        if self.pending_spawn is not None:
            candidates = [unit for unit in by_type[self.pending_spawn.unit_type]
                          if unit.id not in self.pending_spawn.prior_ids]
            if candidates and self.pending_spawn.accepted_tick is not None:
                selected = candidates[0].id
                if self.pending_spawn.unit_type == "VANGUARD":
                    self.home_vanguard_id = selected
                else:
                    self.home_ranger_id = selected
                self.last_spawn_result = "CONFIRMED"
                self.last_role_transition = f"{self.pending_spawn.unit_type}:{selected}"
                self.pending_spawn = None
            elif tick - self.pending_spawn.requested_tick >= COMBAT_SPAWN_TIMEOUT:
                self.mark_spawn_failed(tick)
        pending_type = self.pending_spawn.unit_type if self.pending_spawn is not None else None
        if self.home_vanguard_id is None and by_type["VANGUARD"] and pending_type != "VANGUARD":
            self.home_vanguard_id = by_type["VANGUARD"][0].id
        if self.home_ranger_id is None and by_type["RANGER"] and pending_type != "RANGER":
            self.home_ranger_id = by_type["RANGER"][0].id

    def choose_spawn(self, state: Snapshot, *, production_guard: bool, core_full: bool,
                     core_occupied: bool, recent_deposits: int) -> str | None:
        if (not production_guard or core_full or core_occupied or self.pending_spawn is not None
                or state.tick < self.spawn_cooldown_until or state.core_position is None
                or state.core_state not in {None, "NORMAL"} or state.population >= COMBAT_POPULATION_CEILING
                or state.upkeep_next_tick not in {None, 0}):
            return None
        missing = "VANGUARD" if self.home_vanguard_id is None else (
            "RANGER" if self.home_ranger_id is None else None
        )
        if missing is None:
            return None
        if state.population == COMBAT_POPULATION_CEILING - 1 and missing != "RANGER":
            return None
        cost = unit_production_cost(missing, state.population)
        projected_upkeep = upkeep_for_population(state.population + 1)
        if projected_upkeep and recent_deposits < projected_upkeep * COMBAT_UPKEEP_RESERVE_TICKS:
            return None
        reserve = COMBAT_RESERVE + projected_upkeep * COMBAT_UPKEEP_RESERVE_TICKS
        return missing if state.resources - cost >= reserve else None

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
    combat_cargo_drops: deque[dict[str, Any]] = field(default_factory=deque)
    combat_cooldown_until: int = 0
    controlled_unit_ids: set[str] = field(default_factory=set)
    pending_friendly_deaths: dict[str, int] = field(default_factory=dict)
    combat_episode: dict[str, int] | None = None
    completed_combat_episodes: deque[dict[str, int]] = field(default_factory=deque)
    pending_combat_submissions: dict[tuple[str, int], str] = field(default_factory=dict)

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

    def combat_touch(self, tick: int) -> dict[str, int]:
        if self.combat_episode is None:
            self.combat_episode = {
                "start_tick": tick, "last_tick": tick,
                "shots_hit": 0, "shots_missed": 0, "sweeps": 0, "sweep_targets_hit": 0,
                "outgoing_damage": 0, "incoming_damage": 0,
                "friendly_deaths": 0, "enemy_destruction_participations": 0,
                "friendly_cargo_lost": 0,
                "precision_shots": 0, "cell_intercept_shots": 0,
                "harvests": 0, "deposits": 0,
            }
        self.combat_episode["last_tick"] = tick
        return self.combat_episode

    def combat_close_if_idle(self, tick: int) -> None:
        episode = self.combat_episode
        if episode is None or tick - episode["last_tick"] < COMBAT_EPISODE_IDLE_TICKS:
            return
        episode["end_tick"] = episode["last_tick"]
        self.completed_combat_episodes.append(dict(episode))
        while len(self.completed_combat_episodes) > 32:
            self.completed_combat_episodes.popleft()
        self.combat_episode = None

    def finalize_combat_episode(self, reason: str) -> None:
        if self.combat_episode is None:
            return
        episode: dict[str, Any] = dict(self.combat_episode)
        episode["end_tick"] = episode["last_tick"]
        episode["outcome"] = "INCOMPLETE"
        episode["close_reason"] = reason
        self.completed_combat_episodes.append(episode)
        while len(self.completed_combat_episodes) > 32:
            self.completed_combat_episodes.popleft()
        self.combat_episode = None

    def record_combat_submission(self, actor_id: str, target_mode: str, tick: int) -> None:
        self.pending_combat_submissions[(actor_id, tick)] = target_mode
        while len(self.pending_combat_submissions) > 64:
            self.pending_combat_submissions.pop(next(iter(self.pending_combat_submissions)))

    def consume_combat_submission(self, actor_id: str, tick: int) -> str | None:
        candidates = sorted(
            (submitted_tick, mode)
            for (submitted_actor, submitted_tick), mode in self.pending_combat_submissions.items()
            if submitted_actor == actor_id and submitted_tick <= tick
        )
        if not candidates:
            return None
        submitted_tick, mode = candidates[0]
        self.pending_combat_submissions.pop((actor_id, submitted_tick), None)
        return mode

    def trim(self, tick: int) -> None:
        self.combat_close_if_idle(tick)
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
        self.pending_combat_submissions = {
            key: mode for key, mode in self.pending_combat_submissions.items()
            if key[1] >= tick - RISK_WINDOW_TICKS
        }


@dataclass
class TrafficMemory:
    blocked_edges: dict[tuple[str, Position, str], int] = field(default_factory=dict)
    blocked_cells: dict[Position, int] = field(default_factory=dict)
    repeated_failures: dict[tuple[str, Position, str, str], int] = field(default_factory=dict)
    last_planned_edges: dict[str, tuple[Position, str]] = field(default_factory=dict)
    ingress_queue: tuple[str, ...] = ()
    local_ingress_holder: str | None = None
    local_ingress_distance: int | None = None
    local_ingress_stall_ticks: int = 0
    ingress_yield_until: dict[str, int] = field(default_factory=dict)
    holds: dict[str, str] = field(default_factory=dict)

    def trim(self, tick: int) -> None:
        self.blocked_edges = {key: until for key, until in self.blocked_edges.items() if until > tick}
        self.blocked_cells = {cell: until for cell, until in self.blocked_cells.items() if until > tick}
        while len(self.blocked_edges) > MAX_DYNAMIC_EDGES:
            self.blocked_edges.pop(next(iter(self.blocked_edges)))
        while len(self.blocked_cells) > MAX_DYNAMIC_CELLS:
            self.blocked_cells.pop(next(iter(self.blocked_cells)))
        self.holds.clear()
        self.ingress_yield_until = {
            worker_id: until for worker_id, until in self.ingress_yield_until.items() if until > tick
        }

    def select_local_ingress_head(self, candidates: list[tuple[str, str, int]], tick: int) -> str | None:
        details = {worker_id: (kind, distance) for worker_id, kind, distance in candidates}
        local = [worker_id for worker_id in self.ingress_queue
                 if details.get(worker_id, ("", CORE_INGRESS_RADIUS + 1))[1] <= CORE_INGRESS_RADIUS]
        if not local:
            self.local_ingress_holder = None
            self.local_ingress_distance = None
            self.local_ingress_stall_ticks = 0
            return None

        def rank(worker_id: str) -> tuple[int, int]:
            kind = details[worker_id][0]
            return ({"RETURN_CORE": 0, "RETURN_HEAL": 1, "RETURN_SAFE": 2}.get(kind, 3),
                    self.ingress_queue.index(worker_id))

        available = [worker_id for worker_id in local if self.ingress_yield_until.get(worker_id, 0) <= tick]
        selected = min(available or local, key=rank)
        distance = details[selected][1]
        if selected == self.local_ingress_holder:
            self.local_ingress_stall_ticks = (
                0 if self.local_ingress_distance is None or distance < self.local_ingress_distance
                else self.local_ingress_stall_ticks + 1
            )
            if self.local_ingress_stall_ticks >= CORE_INGRESS_STALL_TICKS and len(local) > 1:
                self.ingress_yield_until[selected] = tick + CORE_INGRESS_STALL_TICKS
                alternatives = [worker_id for worker_id in local if worker_id != selected]
                selected = min(alternatives, key=rank)
                distance = details[selected][1]
                self.local_ingress_stall_ticks = 0
        else:
            self.local_ingress_stall_ticks = 0
        self.local_ingress_holder = selected
        self.local_ingress_distance = distance
        return selected

    def reconcile_ingress_queue(self, candidates: list[tuple[str, str, int]]) -> None:
        """Keep existing Core-approach order stable; only prioritize newly queued cargo."""
        active = {worker_id: (kind, distance) for worker_id, kind, distance in candidates}
        retained = [worker_id for worker_id in self.ingress_queue if worker_id in active]
        retained_set = set(retained)
        new = sorted(
            (item for item in candidates if item[0] not in retained_set),
            key=lambda item: (0 if item[1] == "RETURN_CORE" else 1, item[2], item[0]),
        )
        ordered = retained + [worker_id for worker_id, _, _ in new]
        arrived = {
            worker_id for worker_id, kind, distance in candidates
            if kind == "RETURN_CORE" and distance == 0
        }
        self.ingress_queue = tuple(
            [worker_id for worker_id in ordered if worker_id in arrived]
            + [worker_id for worker_id in ordered if worker_id not in arrived]
        )

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
    completion_cooldowns: dict[Position, int] = field(default_factory=dict)
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
    frontier_selection_sources: dict[str, str] = field(default_factory=dict)
    frontier_no_candidate_reasons: dict[str, int] = field(default_factory=dict)
    frontier_fallback_assignments: int = 0
    frontier_arrival_wait_ticks: int = 0
    frontier_completion_transitions: set[str] = field(default_factory=set)
    safe_retreat_workers: set[str] = field(default_factory=set)
    combat: CombatMemory = field(default_factory=CombatMemory)

    def _reset_for_core(self, state: Snapshot) -> None:
        self.route_core = state.core_position
        self.route_core_id = state.core_id
        self.band_radius = INITIAL_BAND_RADIUS
        self.frontier_candidates.clear()
        self.completed_targets.clear()
        self.completion_cooldowns.clear()
        self.failed_targets.clear()
        self.frontier_failure_reasons.clear()
        self.frontier_path_evaluations = 0
        self.frontier_path_nodes = 0
        self.frontier_worker_evaluations.clear()
        self.active_targets.clear()
        self.active_target = None
        self.safe_retreat_workers.clear()
        self.core_full = False
        self.recovery_cooldown_until = 0
        self.allocation_count = 0
        self.allocation_total_cost = 0
        self.traffic = TrafficMemory()
        self.combat = CombatMemory()

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
        self.completion_cooldowns = {
            target: until for target, until in self.completion_cooldowns.items()
            if until > state.tick
        }
        self.safe_retreat_workers = {
            worker.id for worker in state.workers
            if worker.id in self.safe_retreat_workers
            and worker.cargo <= 0
            and (worker.hp is None or worker.hp > 1)
            and (state.core_position is None
                 or manhattan(worker.position, state.core_position) > CORE_GUARD_RADIUS)
        }
        self.ledger.trim(state.tick)
        self.traffic.trim(state.tick)


    def apply_events(self, state: Snapshot) -> None:
        self.last_event_types = tuple(str(event.get("event_type")) for event in state.events)
        current_controlled_ids = {unit.id for unit in state.units}
        controlled_ids = self.ledger.controlled_unit_ids | current_controlled_ids
        for target_id in list(self.ledger.pending_friendly_deaths):
            if target_id in current_controlled_ids:
                self.ledger.pending_friendly_deaths.pop(target_id, None)
                continue
            episode = self.ledger.combat_touch(state.tick)
            episode["friendly_deaths"] += 1
            self.ledger.pending_friendly_deaths.pop(target_id, None)
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
                if self.ledger.combat_episode is not None:
                    self.ledger.combat_episode["harvests"] += 1
            elif kind == "HARVEST_FAILED" and pos is not None:
                obs = self.resources.setdefault(pos, ResourceObservation(0))
                obs.status = "failed"
                obs.last_seen_tick = state.tick
                obs.failure_count += 1
                self.active_targets.pop(actor_id, None)
            elif kind == "DEPOSIT_FAILED":
                self.core_full = event.get("reason_code") == "CORE_RESOURCE_FULL"
            elif kind == "CORE_SPAWN_FAILED":
                self.recovery_cooldown_until = max(self.recovery_cooldown_until, state.tick + CAPACITY_RECOVERY_COOLDOWN)
                if self.combat.pending_spawn is not None:
                    self.combat.mark_spawn_failed(state.tick)
            elif kind == "CORE_SPAWN_SUCCEEDED":
                values = event.get("values", {})
                if (self.combat.pending_spawn is not None
                        and values.get("unit_type") == self.combat.pending_spawn.unit_type):
                    self.combat.last_spawn_result = "RESOLVED"
            elif kind == "DEPOSIT_SUCCEEDED":
                self.core_full = False
                self.ledger.record_deposit(state.tick)
                self.ledger.record_deposit_latency(actor_id, state.tick)
                self.active_targets.pop(actor_id, None)
                if self.ledger.combat_episode is not None:
                    self.ledger.combat_episode["deposits"] += 1
            elif kind == "UNIT_MOVE_FAILED" and pos is not None:
                reason = str(event.get("reason_code", "UNKNOWN"))
                self.traffic.mark_failure(actor_id, pos, reason, state.tick)
            elif kind == "UNIT_MOVE_SUCCEEDED":
                memory_edge = self.traffic.last_planned_edges.pop(actor_id, None)
                if memory_edge is not None:
                    self.traffic.blocked_edges.pop((actor_id, memory_edge[0], memory_edge[1]), None)
            elif kind in {"SWEEP_RESOLVED", "SHOT_HIT", "SHOT_MISSED", "DESTRUCTION_PARTICIPATION"}:
                targets_hit = int(event.get("values", {}).get("targets_hit", 0))
                damage = int(event.get("values", {}).get("damage", 0))
                episode = self.ledger.combat_touch(state.tick)
                if kind == "SWEEP_RESOLVED":
                    episode["sweeps"] += 1
                    episode["sweep_targets_hit"] += targets_hit
                elif kind == "SHOT_HIT":
                    episode["shots_hit"] += 1
                    episode["outgoing_damage"] += damage
                    mode = self.ledger.consume_combat_submission(actor_id, state.tick)
                    if mode == "PRECISION_CURRENT":
                        episode["precision_shots"] += 1
                    elif mode == "CELL_INTERCEPT":
                        episode["cell_intercept_shots"] += 1
                elif kind == "SHOT_MISSED":
                    episode["shots_missed"] += 1
                    mode = self.ledger.consume_combat_submission(actor_id, state.tick)
                    if mode == "PRECISION_CURRENT":
                        episode["precision_shots"] += 1
                    elif mode == "CELL_INTERCEPT":
                        episode["cell_intercept_shots"] += 1
                elif kind == "DESTRUCTION_PARTICIPATION":
                    episode["enemy_destruction_participations"] += 1
                self.ledger.combat_events.append({
                    "type": kind,
                    "actor_id": actor_id,
                    "target_id": str(event.get("target_id", "")) or None,
                    "targets_hit": targets_hit,
                    "damage": damage,
                })
                while len(self.ledger.combat_events) > MAX_EVENT_IDS:
                    self.ledger.combat_events.popleft()
            elif kind == "UNIT_DAMAGED" and event.get("reason_code") == "ATTACK":
                target_id = str(event.get("target_id", ""))
                was_friendly = target_id in controlled_ids
                if was_friendly:
                    self.ledger.record_worker_damage(target_id, state.tick)
                hp = int(event.get("values", {}).get("hp", -1))
                episode = self.ledger.combat_touch(state.tick)
                damage = int(event.get("values", {}).get("damage", 0))
                if was_friendly:
                    episode["incoming_damage"] += damage
                self.ledger.combat_events.append({
                    "type": "UNIT_DAMAGED", "target_id": target_id,
                    "damage": int(event.get("values", {}).get("damage", 0)), "hp": hp,
                })
                if was_friendly:
                    self.ledger.combat_cooldown_until = max(
                        self.ledger.combat_cooldown_until, state.tick + COMBAT_DAMAGE_COOLDOWN_TICKS
                    )
                if hp == 0:
                    self.ledger.combat_deaths.append({"target_id": target_id, "tick": state.tick})
                    if was_friendly:
                        self.ledger.pending_friendly_deaths[target_id] = state.tick
                    if was_friendly:
                        self.ledger.combat_cooldown_until = max(
                            self.ledger.combat_cooldown_until, state.tick + COMBAT_LOSS_COOLDOWN_TICKS
                        )
                while len(self.ledger.combat_events) > MAX_EVENT_IDS:
                    self.ledger.combat_events.popleft()
                while len(self.ledger.combat_deaths) > MAX_EVENT_IDS:
                    self.ledger.combat_deaths.popleft()
            elif kind == "WORKER_CARGO_DROPPED":
                owner_id = actor_id
                entry = {"worker_id": owner_id or None, "amount": int(event.get("values", {}).get("amount", 0)),
                         "tick": state.tick}
                self.ledger.combat_cargo_drops.append(entry)
                if owner_id in controlled_ids:
                    episode = self.ledger.combat_touch(state.tick)
                    episode["friendly_cargo_lost"] += entry["amount"]
                    self.ledger.combat_cooldown_until = max(
                        self.ledger.combat_cooldown_until, state.tick + COMBAT_LOSS_COOLDOWN_TICKS
                    )
                while len(self.ledger.combat_cargo_drops) > MAX_EVENT_IDS:
                    self.ledger.combat_cargo_drops.popleft()
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
        self.ledger.controlled_unit_ids = current_controlled_ids
        self.combat.reconcile_roles(state.units, state.tick)

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
        self.frontier_selection_sources.clear()
        self.frontier_completion_transitions.clear()

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
        if active == worker.position:
            self.complete_frontier_if_reached(worker, state.tick)
            active = None
        if (active is not None and within_budget(active) and active not in reserved
                and active not in self.failed_targets and active not in self.completion_cooldowns):
            result = self.frontier_path(worker.id, worker.position, active, obstacles, occupied)
            if result.status == "FOUND":
                self.frontier_selection_sources[worker.id] = "active"
                return active, result
            if result.status == "FRONTIER_BUDGET":
                self.frontier_selection_sources[worker.id] = "budget_deferred"
                self.frontier_no_candidate_reasons["FRONTIER_BUDGET_DEFERRED"] = (
                    self.frontier_no_candidate_reasons.get("FRONTIER_BUDGET_DEFERRED", 0) + 1
                )
                return None, result
            self.active_targets.pop(worker.id, None)
        budget_deferred = False
        candidates = list(self.frontier_candidates)
        self.frontier_candidates.clear()
        best: tuple[tuple[int, int, int, int, int], Position, PathResult] | None = None
        for target in candidates:
            failures, retry_after = self.failed_targets.get(target, (0, 0))
            if (not within_budget(target) or target in reserved or retry_after > state.tick
                    or target in obstacles or target in occupied or target in self.completion_cooldowns
                    or target == worker.position):
                self.frontier_candidates.append(target)
                continue
            result = self.frontier_path(worker.id, worker.position, target, obstacles, occupied)
            if result.status == "FRONTIER_BUDGET":
                budget_deferred = True
                self.frontier_candidates.appendleft(target)
                break
            if result.status != "FOUND":
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
        force_refill_selected = False
        if best is None and not budget_deferred:
            self._refill_frontier(state, force=True)
            for target in list(self.frontier_candidates)[:MAX_FALLBACK_FRONTIER_CANDIDATES]:
                failures, retry_after = self.failed_targets.get(target, (0, 0))
                if (not within_budget(target) or target in reserved or retry_after > state.tick
                        or target in obstacles or target in occupied or target in self.completion_cooldowns
                        or target == worker.position):
                    continue
                result = self.frontier_path(worker.id, worker.position, target, obstacles, occupied)
                if result.status == "FRONTIER_BUDGET":
                    budget_deferred = True
                    break
                if result.status != "FOUND":
                    self.failed_targets[target] = (failures + 1, self.frontier_retry_after(result.status, failures, state.tick))
                    self.frontier_failure_reasons[result.status] = (
                        self.frontier_failure_reasons.get(result.status, 0) + 1
                    )
                    continue
                score = (result.path_length, target[0], target[1])
                if best is None or score < best[0]:
                    best = (score, target, result)
                    force_refill_selected = True
        if best is None and not budget_deferred and max_radius is not None and state.core_position is not None:
            fallback = sorted(
                (
                    target for target in self._build_band(state.core_position, max_radius)
                    if target not in reserved and target not in obstacles and target not in occupied
                    and target not in self.completion_cooldowns and target != worker.position
                    and self.failed_targets.get(target, (0, 0))[1] <= state.tick
                ),
                key=lambda target: (abs(target[0] - worker.position[0]) + abs(target[1] - worker.position[1]),
                                    target[0], target[1]),
            )[:MAX_FALLBACK_FRONTIER_CANDIDATES]
            for target in fallback:
                result = self.frontier_path(worker.id, worker.position, target, obstacles, occupied)
                if result.status == "FRONTIER_BUDGET":
                    budget_deferred = True
                    break
                if result.status == "FOUND":
                    score = (result.path_length, target[0], target[1])
                    if best is None or score < best[0]:
                        best = (score, target, result)
            if best is not None:
                self.frontier_fallback_assignments += 1
                self.frontier_selection_sources[worker.id] = "fallback"
        if best is None:
            reason = "FRONTIER_BUDGET_DEFERRED" if budget_deferred else "FRONTIER_NO_CANDIDATE"
            self.frontier_selection_sources[worker.id] = "none"
            self.frontier_no_candidate_reasons[reason] = self.frontier_no_candidate_reasons.get(reason, 0) + 1
            self._trim()
            return None, PathResult(None, reason, 0, 0, None)
        _, target, result = best
        self.active_targets[worker.id] = target
        self.active_target = target
        if force_refill_selected:
            self.frontier_fallback_assignments += 1
            self.frontier_selection_sources[worker.id] = "fallback"
        else:
            self.frontier_selection_sources.setdefault(worker.id, "candidate")
        self._trim()
        return target, result

    def complete_frontier_if_reached(self, worker: Unit, tick: int) -> bool:
        if self.active_targets.get(worker.id) != worker.position:
            return False
        self.completed_targets[worker.position] = tick
        self.completion_cooldowns[worker.position] = tick + FRONTIER_COMPLETION_COOLDOWN_TICKS
        self.active_targets.pop(worker.id, None)
        self.frontier_candidates = deque(target for target in self.frontier_candidates if target != worker.position)
        return True

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
    worker_intents: dict[str, tuple[Position, str]] = field(default_factory=dict)
    combat_decisions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        output: dict[str, Any] = {"unit_actions": self.unit_actions}
        if self.core_action is not None:
            output["core_action"] = self.core_action
        return output


def manhattan(left: Position, right: Position) -> int:
    return abs(left[0] - right[0]) + abs(left[1] - right[1])


def threatened_workers(state: Snapshot) -> set[str]:
    return {
        worker.id for worker in state.workers
        if any(manhattan(worker.position, enemy.position) <= THREAT_RADIUS for enemy in state.visible_enemies)
    }


def core_threatened(state: Snapshot) -> bool:
    return state.core_position is not None and any(
        manhattan(state.core_position, enemy.position) <= CORE_GUARD_RADIUS
        for enemy in state.visible_enemies
    )


def defender_in_core_guard(position: Position, core: Position | None) -> bool:
    return core is not None and manhattan(position, core) <= CORE_GUARD_RADIUS


def vanguard_guard_actions(state: Snapshot, *, threatened_worker_ids: set[str]) -> dict[str, dict[str, Any]]:
    actions: dict[str, dict[str, Any]] = {}
    economy_risk = bool(threatened_worker_ids) or core_threatened(state)
    for vanguard in state.vanguards:
        if not economy_risk or not defender_in_core_guard(vanguard.position, state.core_position):
            actions[vanguard.id] = {"type": "WAIT"}
            continue
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


def ranger_fire_allowed(state: Snapshot, memory: ExplorationMemory, *, threatened_worker_ids: set[str] | None = None) -> bool:
    threatened_worker_ids = threatened_worker_ids or set()
    if any(worker.id in threatened_worker_ids and worker.cargo > 0 for worker in state.workers):
        return False
    return (
        not memory.core_full
        and state.population <= COMBAT_POPULATION_CEILING
        and state.tick >= memory.ledger.combat_cooldown_until
        and not memory.ledger.worker_damage_ticks
    )


def core_local_ranger_ids(state: Snapshot) -> set[str]:
    return {
        ranger.id for ranger in state.rangers
        if defender_in_core_guard(ranger.position, state.core_position)
    }


def traffic_priority(worker: Unit, core: Position | None) -> tuple[int, str]:
    if worker.cargo > 0:
        return 0, worker.id
    if worker.hp is not None and worker.hp <= 1:
        return 1, worker.id
    return 2, worker.id


def friendly_cell_loads(state: Snapshot) -> Counter[Position]:
    loads: Counter[Position] = Counter()
    if state.core_position is not None:
        loads[state.core_position] += 1
    loads.update(unit.position for unit in state.units)
    return loads


def movement_blocked_cells(state: Snapshot, moving_unit_id: str | None = None,
                           reserved_arrivals: Counter[Position] | None = None,
                           reserved_departures: Counter[Position] | None = None) -> set[Position]:
    """Cells with no legal friendly capacity under the current v0.13 snapshot."""
    loads = friendly_cell_loads(state)
    for cell, count in (reserved_departures or {}).items():
        loads[cell] -= count
    for cell, count in (reserved_arrivals or {}).items():
        loads[cell] += count
    if moving_unit_id is not None:
        moving = next((unit for unit in state.units if unit.id == moving_unit_id), None)
        if moving is not None:
            loads[moving.position] -= 1
    blocked = {cell for cell, load in loads.items() if load >= 2}
    blocked.update(enemy.position for enemy in state.visible_enemies)
    return blocked


def choose_traffic_move(worker: Unit, target: Position, state: Snapshot, obstacles: frozenset[Position],
                         reserved_arrivals: Counter[Position],
                         reserved_departures: Counter[Position],
                         reserved_edges: set[tuple[Position, Position]], memory: ExplorationMemory) -> PathResult:
    dynamic_cells = {cell for cell, until in memory.traffic.blocked_cells.items() if until > state.tick}
    blockers = obstacles | dynamic_cells
    occupied = movement_blocked_cells(
        state, worker.id, reserved_arrivals, reserved_departures,
    )
    candidates: list[tuple[int, int, str, PathResult]] = []
    for rank, direction in enumerate(DIRECTIONS):
        if memory.traffic.is_edge_blocked(worker.id, worker.position, direction, state.tick):
            continue
        nxt = step_position(worker.position, direction)
        if nxt in blockers or nxt in occupied or (nxt, worker.position) in reserved_edges:
            continue
        path = plan_path(nxt, {target}, blockers, occupied)
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
          path: PathResult | None = None, core_action: dict[str, Any] | None = None,
          worker_intents: dict[str, tuple[Position, str]] | None = None,
          combat_decisions: dict[str, dict[str, Any]] | None = None) -> Plan:
    memory.policy_state = policy_state
    memory.last_path = path
    return Plan(
        unit_actions=actions,
        core_action=core_action,
        policy_state=policy_state,
        active_target=target,
        waypoint=waypoint,
        last_event_types=memory.last_event_types,
        path_status=path.status if path else "NONE",
        path_nodes=path.explored_nodes if path else 0,
        path_length=path.path_length if path else 0,
        band_radius=memory.band_radius,
        assignments=len(memory.active_targets),
        worker_intents=worker_intents or {},
        combat_decisions=combat_decisions or {},
    )


def _combat_proposals(state: Snapshot, memory: ExplorationMemory,
                      combat_mode: str) -> dict[str, dict[str, Any]]:
    protected = {state.core_position} if state.core_position is not None else set()
    protected.update(worker.position for worker in state.workers if worker.cargo > 0 or (worker.hp or 2) <= 1)
    escort = next((worker for worker in state.workers
                   if worker.id == memory.combat.escort_worker_id), None)
    if escort is not None:
        protected.add(escort.position)
    proposals: dict[str, dict[str, Any]] = {}
    for unit in (*state.vanguards, *state.rangers):
        if unit.id not in {memory.combat.home_vanguard_id, memory.combat.home_ranger_id}:
            continue
        proposal_radius = None if combat_mode == "shadow" else THREAT_RADIUS
        if unit.unit_type == "VANGUARD":
            decision = select_vanguard_decision(
                unit, state, protected_cells=protected, protected_radius=proposal_radius,
            )
        else:
            decision = select_ranger_decision(
                unit, state, protected_cells=protected,
                allow_cell_intercept=combat_mode in {"shadow", "live-cell", "live"},
                protected_radius=proposal_radius,
            )
        action_type = decision.action.get("type")
        attack_enabled = (
            action_type == "SWEEP" and combat_mode in {"shadow", "live-sweep", "live-precision", "live-cell", "live"}
            or action_type == "SHOOT" and decision.target_mode == "PRECISION_CURRENT"
            and combat_mode in {"shadow", "live-precision", "live-cell", "live"}
            or action_type == "SHOOT" and decision.target_mode == "CELL_INTERCEPT"
            and combat_mode in {"shadow", "live-cell", "live"}
        )
        if action_type in {"SWEEP", "SHOOT"} and not attack_enabled:
            decision = type(decision)({"type": "WAIT"}, target_id=decision.target_id,
                                      target_position=decision.target_position,
                                      target_mode=decision.target_mode, reason="ATTACK_STAGE_DISABLED")
        if unit.unit_type == "RANGER" and not ranger_fire_allowed(
                state, memory, threatened_worker_ids=threatened_workers(state)):
            decision = type(decision)({"type": "WAIT"}, reason="RANGER_FIRE_FUSED")
        proposals[unit.id] = {
            "role": "HOME_VANGUARD" if unit.unit_type == "VANGUARD" else "HOME_RANGER",
            "state": "ATTACK" if decision.action.get("type") in {"SWEEP", "SHOOT"} else "HOLD",
            "target_mode": decision.target_mode,
            "candidate_cell": list(decision.target_position) if decision.target_position else None,
            "reason": decision.reason,
            "proposed_action": decision.action,
            "shadow": False,
        }
    return proposals


def _apply_defender_movement(state: Snapshot, memory: ExplorationMemory,
                             actions: dict[str, dict[str, Any]],
                             proposals: dict[str, dict[str, Any]],
                             reserved_arrivals: Counter[Position],
                             reserved_departures: Counter[Position],
                             reserved_edges: set[tuple[Position, Position]],
                             obstacles: frozenset[Position]) -> None:
    slots = guard_slots(state.core_position, obstacles) if state.core_position is not None else ()
    assigned_slots: set[Position] = set()
    positions = {unit.id: unit.position for unit in state.units}
    defenders = [unit for unit in state.units
                 if unit.id in {memory.combat.home_vanguard_id, memory.combat.home_ranger_id}]
    core = state.core_position
    current_threats = [
        enemy for enemy in state.visible_enemies
        if core is not None and manhattan(enemy.position, core) <= CORE_GUARD_RADIUS + 1
    ]
    escort = next((worker for worker in state.workers
                   if worker.id == memory.combat.escort_worker_id), None)
    escort_threats = [
        enemy for enemy in state.visible_enemies
        if escort is not None and manhattan(enemy.position, escort.position) <= THREAT_RADIUS
    ]
    threat_core = core if core is not None else (0, 0)
    for unit in sorted(defenders, key=lambda item: item.id):
        proposal = proposals.setdefault(unit.id, {
            "role": unit.unit_type, "state": "HOLD", "target_mode": None,
            "candidate_cell": None, "reason": "NO_PROPOSAL",
            "proposed_action": {"type": "WAIT"}, "shadow": False,
        })
        max_hp = 4 if unit.unit_type == "VANGUARD" else 2
        target: Position | None = None
        if unit.hp is not None and unit.hp < max_hp and state.core_position is not None:
            proposal["state"] = "RECOVER"
            proposal["reason"] = "INJURED_DEFENDER"
            if unit.position == state.core_position:
                proposal["proposed_action"] = {"type": "HEAL"}
                actions[unit.id] = {"type": "HEAL"}
                continue
            target = state.core_position
        elif proposal["proposed_action"].get("type") in {"SWEEP", "SHOOT"}:
            actions[unit.id] = proposal["proposed_action"]
            continue
        elif current_threats or escort_threats:
            active_threats = current_threats or escort_threats
            active_anchor = threat_core if current_threats else (
                escort.position if escort is not None else threat_core
            )
            threat = min(active_threats, key=lambda enemy: (
                manhattan(enemy.position, active_anchor), enemy.id
            ))
            candidates = (list(ranger_firing_cells(threat.position, obstacles))
                          if unit.unit_type == "RANGER" else [
                              step_position(threat.position, direction) for direction in DIRECTIONS
                              if step_position(threat.position, direction) not in obstacles
                          ])
            target = min(candidates, key=lambda cell: (manhattan(unit.position, cell), cell), default=None)
            proposal["state"] = "RESPOND"
            proposal["movement_reason"] = (
                "CURRENT_CORE_ZONE_THREAT" if current_threats else "CURRENT_ESCORT_THREAT"
            )
            proposal["candidate_cell"] = list(target) if target is not None else None
            if target is None:
                actions[unit.id] = {"type": "WAIT"}
                proposal["state"] = "BLOCKED"
                proposal["movement_reason"] = "NO_RESPONSE_CELL"
                continue
        elif escort is not None:
            preferred = (
                ("LEFT", "UP", "RIGHT", "DOWN")
                if unit.unit_type == "VANGUARD" else
                ("RIGHT", "DOWN", "LEFT", "UP")
            )
            candidates = [
                step_position(escort.position, direction) for direction in preferred
                if step_position(escort.position, direction) not in obstacles
                and step_position(escort.position, direction) not in assigned_slots
            ]
            target = min(candidates, key=lambda cell: (
                preferred.index(next(
                    direction for direction in preferred
                    if step_position(escort.position, direction) == cell
                )), manhattan(unit.position, cell), cell,
            ), default=None)
            if target is not None:
                assigned_slots.add(target)
            proposal["state"] = "ESCORT"
            proposal["movement_reason"] = "FRIENDLY_ESCORT_ANCHOR"
            proposal["candidate_cell"] = list(target) if target is not None else None
            if target is None or unit.position == target:
                actions[unit.id] = {"type": "WAIT"}
                continue
        else:
            target = next((slot for slot in slots if slot not in assigned_slots), None)
            if target is not None:
                assigned_slots.add(target)
            if target is None or unit.position == target:
                actions[unit.id] = {"type": "WAIT"}
                proposal["state"] = "HOLD"
                continue
            proposal["state"] = "ASSEMBLE"
            proposal["candidate_cell"] = list(target)

        move = choose_traffic_move(unit, target, state, obstacles,
                                   reserved_arrivals, reserved_departures,
                                   reserved_edges, memory)
        if move.direction is None:
            actions[unit.id] = {"type": "WAIT"}
            proposal["state"] = "BLOCKED"
            proposal["reason"] = move.status
            memory.traffic.holds[unit.id] = move.status
            continue
        destination = step_position(unit.position, move.direction)
        actions[unit.id] = {"type": "MOVE", "direction": move.direction}
        proposal["proposed_action"] = actions[unit.id]
        proposal["path_status"] = move.status
        proposal["path_length"] = move.path_length
        proposal["path_nodes"] = move.explored_nodes
        reserved_departures[unit.position] += 1
        reserved_arrivals[destination] += 1
        reserved_edges.add((unit.position, destination))
        memory.traffic.mark_planned_move(unit.id, unit.position, move.direction)


def economy_plan(state: Snapshot, memory: ExplorationMemory | None = None, *,
                 combat_mode: str = "current", combat_production_guard: bool = True) -> Plan:
    memory = memory or ExplorationMemory()
    memory.observe(state)
    memory.apply_events(state)
    # Resolution events describe the previous Tick. If this authoritative state
    # still exposes the cell as RESOURCE, current visibility wins over a prior
    # HARVEST_FAILED/depletion result.
    for resource in state.resource_cells:
        observation = memory.resources.setdefault(resource, ResourceObservation(state.tick))
        observation.last_seen_tick = state.tick
        observation.status = "visible"
    memory.allocation_count = 0
    memory.allocation_total_cost = 0
    memory.begin_frontier_budget()
    if state.status != "ACTIVE":
        return _plan({}, memory, policy_state="PAUSE")

    if (combat_mode in COMBAT_PRODUCTION_MODES
            and state.population >= COMBAT_POPULATION_CEILING
            and state.resources <= COMBAT_RESERVE
            and memory.combat.home_ranger_id is not None):
        ranger = next(
            (unit for unit in state.rangers
             if unit.id == memory.combat.home_ranger_id and unit.cargo == 0),
            None,
        )
        if ranger is not None:
            actions = {unit.id: {"type": "WAIT"} for unit in state.units}
            actions[ranger.id] = {"type": "SELF_DESTRUCT"}
            return _plan(actions, memory, policy_state="COMBAT_UPKEEP_FUSE")

    threatened_worker_ids = threatened_workers(state)
    memory.combat.reconcile_escort(state, threatened_worker_ids)
    memory.safe_retreat_workers.update(
        worker_id for worker_id in threatened_worker_ids
        if any(worker.id == worker_id and worker.cargo <= 0 and (worker.hp is None or worker.hp > 1)
               for worker in state.workers)
    )
    actions = {unit.id: {"type": "WAIT"} for unit in state.units if unit.unit_type not in {"WORKER", "RANGER"}}
    actions.update(vanguard_guard_actions(state, threatened_worker_ids=threatened_worker_ids))
    actions.update(ranger_actions(
        state,
        allow_fire=ranger_fire_allowed(state, memory, threatened_worker_ids=threatened_worker_ids),
        allowed_ranger_ids=core_local_ranger_ids(state),
    ))
    combat_decisions = _combat_proposals(state, memory, combat_mode) if combat_mode != "current" else {}
    if combat_mode == "shadow":
        for decision in combat_decisions.values():
            decision["shadow"] = True
    workers = sorted(state.workers, key=lambda unit: unit.id)
    if not workers:
        obstacles = frozenset(memory.permanent_obstacles | set(state.obstacle_cells))
        if combat_mode in COMBAT_MOVEMENT_MODES:
            if combat_mode == "positioning":
                for decision in combat_decisions.values():
                    if decision["proposed_action"].get("type") in {"SWEEP", "SHOOT"}:
                        decision["proposed_action"] = {"type": "WAIT"}
                        decision["reason"] = "ATTACKS_DISABLED"
            _apply_defender_movement(
                state, memory, actions, combat_decisions, Counter(), Counter(), set(), obstacles,
            )
        core_action = None
        if combat_mode in COMBAT_PRODUCTION_MODES:
            combat_spawn = memory.combat.choose_spawn(
                state, production_guard=combat_production_guard, core_full=memory.core_full,
                core_occupied=any(unit.position == state.core_position for unit in state.units),
                recent_deposits=len(memory.ledger.deposits),
            )
            if combat_spawn is not None:
                core_action = {"type": "SPAWN", "unit_type": combat_spawn}
                memory.combat.request_spawn(combat_spawn, state.tick, state.units)
        if core_action is None and memory.can_spawn_worker(state):
            core_action = {"type": "SPAWN", "unit_type": "WORKER"}
        return _plan(actions, memory, policy_state="SPAWN_BOOT" if core_action else "WAIT_NO_WORKER",
                     core_action=core_action, combat_decisions=combat_decisions)

    obstacles = frozenset(memory.permanent_obstacles | set(state.obstacle_cells))
    positions = {
        unit.id: unit.position
        for unit in (state.units if combat_mode in COMBAT_MOVEMENT_MODES else state.workers)
    }
    reserved_arrivals: Counter[Position] = Counter()
    reserved_departures: Counter[Position] = Counter()
    reserved_edges: set[tuple[Position, Position]] = set()
    desired: dict[str, tuple[Unit, Position, str]] = {}
    core_full_held_carriers: set[str] = set()
    core_action: dict[str, Any] | None = None
    primary_state, primary_target, primary_path = "EXPLORE", None, None

    # Core-full transaction remains above normal traffic scheduling.
    core_carriers = [w for w in workers if w.cargo > 0 and w.position == state.core_position]
    if memory.core_full and core_carriers:
        leader = core_carriers[0] if len(core_carriers) == 1 else None
        evictor = core_carriers[-1] if len(core_carriers) > 1 else None
        for worker in workers:
            if worker.cargo > 0:
                actions[worker.id] = {"type": "WAIT"}
                core_full_held_carriers.add(worker.id)
                if worker.position != state.core_position:
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
            if actor is evictor or recovery_allowed:
                blocked = movement_blocked_cells(
                    state, actor.id, reserved_arrivals, reserved_departures,
                )
                direction = next(
                    (d for d in DIRECTIONS
                     if step_position(actor.position, d) not in obstacles | blocked),
                    None,
                )
                if direction:
                    actions[actor.id] = {"type": "MOVE", "direction": direction}
                    destination = step_position(actor.position, direction)
                    reserved_departures[actor.position] += 1
                    reserved_arrivals[destination] += 1
                    reserved_edges.add((actor.position, step_position(actor.position, direction)))
                    if actor is evictor:
                        return _plan(actions, memory, policy_state="CORE_FULL_EVICT", target=state.core_position)
                    if combat_mode in COMBAT_PRODUCTION_MODES:
                        combat_spawn = memory.combat.choose_spawn(
                            state, production_guard=combat_production_guard,
                            core_full=False, core_occupied=False,
                            recent_deposits=len(memory.ledger.deposits),
                        )
                        if combat_spawn is not None:
                            memory.combat.request_spawn(combat_spawn, state.tick, state.units)
                            return _plan(
                                actions, memory,
                                policy_state="CORE_FULL_COMBAT_CAPACITY_RECOVERY",
                                target=state.core_position,
                                core_action={"type": "SPAWN", "unit_type": combat_spawn},
                                combat_decisions=combat_decisions,
                            )
                    if recovery_ceiling_reached:
                        worker_cost = unit_production_cost("WORKER", state.population)
                        pop19_worker_recovery = (
                            state.population == MAX_EXTERNAL_RECOVERY_POPULATION
                            and state.resources - worker_cost >= COMBAT_RESERVE
                            and state.core_state in {None, "NORMAL"}
                            and (state.core_hp is None or state.core_hp >= 5)
                            and (state.core_shield is None or state.core_shield >= 5)
                            and memory.combat.pending_spawn is None
                        )
                        if pop19_worker_recovery:
                            return _plan(
                                actions, memory,
                                policy_state="CORE_FULL_POP19_WORKER_RECOVERY",
                                target=state.core_position,
                                core_action={"type": "SPAWN", "unit_type": "WORKER"},
                                combat_decisions=combat_decisions,
                            )
                        actions[actor.id] = {"type": "WAIT"}
                        reserved_arrivals.clear()
                        reserved_departures.clear()
                        reserved_edges.clear()
                    else:
                        core_action = {"type": "SPAWN", "unit_type": "WORKER"}
                        policy_state = ("CORE_FULL_EXTERNAL_CAP_RECOVERY" if external_over_cap
                                        else "CORE_FULL_RECOVERY")
                        return _plan(actions, memory, policy_state=policy_state,
                                     target=state.core_position, core_action=core_action)
        # Keep only cargo transactions held. Empty Workers continue bounded
        # exploration below; Core-full state suppresses new harvest assignment.

    frontier_reserved: set[Position] = set()
    for worker in workers:
        if worker.id in core_full_held_carriers:
            desired[worker.id] = (worker, worker.position, "CORE_FULL_HOLD")
        elif worker.cargo > 0 and state.core_position:
            desired[worker.id] = (worker, state.core_position, "RETURN_CORE")
    for worker in workers:
        if worker.id in desired or worker.hp is None or worker.hp > 1 or state.core_position is None:
            continue
        desired[worker.id] = (worker, state.core_position, "RETURN_HEAL")
    for worker in workers:
        if worker.id in desired or worker.id not in memory.safe_retreat_workers or state.core_position is None:
            continue
        desired[worker.id] = (worker, state.core_position, "RETURN_SAFE")

    # Current-state resource underfoot preempts stale RESOURCE/EXPLORE and an
    # unthreatened sticky retreat, but never cargo return, healing, current
    # threat retreat, or Core-full harvest suppression.
    for worker in workers:
        existing = desired.get(worker.id)
        unthreatened_sticky_retreat = (
            existing is not None
            and existing[2] == "RETURN_SAFE"
            and worker.id not in threatened_worker_ids
        )
        if ((existing is None or unthreatened_sticky_retreat)
                and worker.cargo == 0
                and (worker.hp is None or worker.hp > 1)
                and not memory.core_full and worker.position in state.resource_cells):
            desired[worker.id] = (worker, worker.position, "RESOURCE")
    resource_reserved: set[Position] = {
        worker.position for worker, _, kind in desired.values() if kind == "RESOURCE"
    }

    empty = [w for w in workers if w.id not in desired]
    eligible_resources = [worker for worker in empty if worker.hp is None or worker.hp > 1]
    resource_assignments = allocate_visible_resources(
        eligible_resources,
        (() if memory.core_full else (
            resource for resource in memory.visible_targets(state)
            if resource not in resource_reserved
        )),
        lambda worker, resource: plan_path(
            worker.position, {resource}, obstacles, movement_blocked_cells(state, worker.id),
        ),
    )
    memory.allocation_count = len(resource_assignments)
    memory.allocation_total_cost = sum(assignment.cost for assignment in resource_assignments)
    for assignment in resource_assignments:
        worker = next(worker for worker in eligible_resources if worker.id == assignment.worker_id)
        desired[worker.id] = (worker, assignment.resource, "RESOURCE")
        resource_reserved.add(assignment.resource)
    for index, worker in enumerate(w for w in workers if w.id not in desired):
        if memory.complete_frontier_if_reached(worker, state.tick):
            memory.frontier_completion_transitions.add(worker.id)
        target, path = memory.next_frontier(state, worker, obstacles, movement_blocked_cells(state, worker.id),
                                            frontier_reserved, memory.worker_frontier_radius(index))
        if target is not None:
            desired[worker.id] = (worker, target, "EXPLORE")
            frontier_reserved.add(target)
        else:
            actions[worker.id] = {"type": "WAIT"}
            memory.traffic.holds[worker.id] = path.status
            if worker.id in memory.frontier_completion_transitions:
                memory.frontier_arrival_wait_ticks += 1

    # Core ingress queue serializes both cargo delivery and safe retreats near the Core.
    ingress_candidates = [
        (worker.id, kind, abs(worker.position[0] - state.core_position[0]) + abs(worker.position[1] - state.core_position[1]))
        for worker, target, kind in desired.values() if kind in {"RETURN_CORE", "RETURN_SAFE"}
    ] if state.core_position else []
    memory.traffic.reconcile_ingress_queue(ingress_candidates)
    local_ingress_head = memory.traffic.select_local_ingress_head(ingress_candidates, state.tick)

    for worker, target, kind in sorted(desired.values(), key=lambda item: traffic_priority(item[0], state.core_position)):
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
            elif kind == "RETURN_SAFE":
                actions[worker.id] = {"type": "WAIT"}
                memory.traffic.holds[worker.id] = "RETURN_SAFE_AT_CORE"
                primary_state, primary_target = "RETURN_SAFE", target
            elif kind == "CORE_FULL_HOLD":
                actions[worker.id] = {"type": "WAIT"}
                memory.traffic.holds[worker.id] = "CORE_FULL_HOLD"
            continue
        if (kind in {"RETURN_CORE", "RETURN_SAFE"} and local_ingress_head is not None
                and worker.id != local_ingress_head
                and state.core_position is not None
                and abs(worker.position[0] - state.core_position[0]) + abs(worker.position[1] - state.core_position[1]) <= CORE_INGRESS_RADIUS):
            actions[worker.id] = {"type": "WAIT"}
            memory.traffic.holds[worker.id] = "CORE_INGRESS_HOLD"
            continue
        path = choose_traffic_move(worker, target, state, obstacles,
                                   reserved_arrivals, reserved_departures,
                                   reserved_edges, memory)
        if path.direction is None:
            actions[worker.id] = {"type": "WAIT"}
            memory.traffic.holds[worker.id] = path.status
            continue
        destination = step_position(worker.position, path.direction)
        actions[worker.id] = {"type": "MOVE", "direction": path.direction}
        reserved_departures[worker.position] += 1
        reserved_arrivals[destination] += 1
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

    if combat_mode in COMBAT_MOVEMENT_MODES:
        if combat_mode == "positioning":
            for decision in combat_decisions.values():
                if decision["proposed_action"].get("type") in {"SWEEP", "SHOOT"}:
                    decision["proposed_action"] = {"type": "WAIT"}
                    decision["reason"] = "ATTACKS_DISABLED"
        _apply_defender_movement(state, memory, actions, combat_decisions,
                                 reserved_arrivals, reserved_departures,
                                 reserved_edges, obstacles)

    if core_action is None:
        core_action = choose_core_defense_action(state, memory)
    if core_action is None and combat_mode in COMBAT_PRODUCTION_MODES:
        combat_spawn = memory.combat.choose_spawn(
            state, production_guard=combat_production_guard, core_full=memory.core_full,
            core_occupied=any(unit.position == state.core_position for unit in state.units),
            recent_deposits=len(memory.ledger.deposits),
        )
        if combat_spawn is not None:
            core_action = {"type": "SPAWN", "unit_type": combat_spawn}
            memory.combat.request_spawn(combat_spawn, state.tick, state.units)
    if core_action is None and memory.can_spawn_worker(state):
        core_action = {"type": "SPAWN", "unit_type": "WORKER"}
    for worker in workers:
        actions.setdefault(worker.id, {"type": "WAIT"})
    worker_intents = {worker.id: (target, kind) for worker, target, kind in desired.values()}
    if memory.core_full and core_full_held_carriers:
        primary_state = "CORE_FULL_ACTIVE"
    return _plan(actions, memory, policy_state=primary_state, target=primary_target,
                 waypoint=primary_target if primary_state == "EXPLORE" else None,
                 path=primary_path, core_action=core_action, worker_intents=worker_intents,
                 combat_decisions=combat_decisions)
