import asyncio
import unittest
from unittest.mock import mock_open, patch
from arena_agent.allocator import allocate_visible_resources
from arena_agent.model import snapshot_from_state
from arena_agent.path import PathResult
from arena_agent.__main__ import PermanentAuthError, post_plan, record_received_source
from pathlib import Path
from arena_agent.policy import (
    ExplorationMemory, MAX_BAND_RADIUS, MAX_ECONOMY_WORKERS, PATH_NODE_CAP,
    economy_plan, first_step, plan_path,
)
from arena_agent.journal import Journal

class AgentTests(unittest.TestCase):
    def state(self, **kw):
        data = {
            "status":"ACTIVE", "resources":5, "population":1,
            "objects":[
                {"kind":"CORE","id":"core","controlled":True,"position":[0,0]},
                {"kind":"UNIT","id":"worker","controlled":True,"position":[2,0],"unit_type":"WORKER","cargo":0},
                {"kind":"OBSTACLE","positions":[[1,0]]},
                {"kind":"RESOURCE","positions":[[2,2]]},
            ], "events": []
        }
        data.update(kw)
        return snapshot_from_state(1, data)
    def test_received_source_audit_marks_manual_core_action_only(self):
        audit = {"manual_interventions": 0, "external_core_actions": 0,
                 "window_contaminated": False, "last_received": None}
        record_received_source(audit, {"source": "AGENT", "tick": 1,
                                       "plan": {"unit_actions": {}}})
        self.assertEqual((audit["manual_interventions"], audit["external_core_actions"]), (0, 0))
        self.assertFalse(audit["window_contaminated"])
        record_received_source(audit, {"source": "MANUAL", "tick": 2,
                                       "plan": {"core_action": {"type": "SPAWN", "unit_type": "WORKER"}}})
        self.assertEqual((audit["manual_interventions"], audit["external_core_actions"]), (1, 1))
        self.assertTrue(audit["window_contaminated"])
        self.assertEqual(audit["last_received"], {"source": "MANUAL", "tick": 2, "core_action": "SPAWN"})

    def test_global_resource_allocator_beats_worker_greedy_order(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker-a", "controlled": True,
                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0},
                {"kind": "UNIT", "id": "worker-b", "controlled": True,
                 "position": [2, 0], "unit_type": "WORKER", "cargo": 0},
            ], "events": []})
        resources = ((10, 0), (20, 0))
        lengths = {
            ("worker-a", (10, 0)): 1, ("worker-a", (20, 0)): 2,
            ("worker-b", (10, 0)): 2, ("worker-b", (20, 0)): 100,
        }
        def path_for(worker, resource):
            length = lengths[worker.id, resource]
            return PathResult("RIGHT", "FOUND", length, length, resource)
        result = allocate_visible_resources(s.workers, resources, path_for)
        self.assertEqual([(item.worker_id, item.resource) for item in result], [
            ("worker-a", (20, 0)), ("worker-b", (10, 0)),
        ])

    def test_resource_allocator_excludes_unreachable_pairs(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0},
            ], "events": []})
        result = allocate_visible_resources(s.workers, ((10, 0),),
            lambda worker, resource: PathResult(None, "NO_PATH", 0, 10, None))
        self.assertEqual(result, ())

    def test_allocator_metrics_reset_when_core_full_bypasses_allocation(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [1, 0], "unit_type": "WORKER", "cargo": 0}
        resource = {"kind": "RESOURCE", "positions": [[2, 0]]}
        economy_plan(snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker, resource], "events": []}), mem)
        self.assertEqual(mem.allocation_count, 1)
        worker["cargo"] = 1
        full = {"event_id": "full", "event_type": "DEPOSIT_FAILED", "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]}
        economy_plan(snapshot_from_state(2, {"status": "ACTIVE", "resources": 10, "population": 1,
            "objects": [core, worker], "events": [full]}), mem)
        self.assertEqual((mem.allocation_count, mem.allocation_total_cost), (0, 0))

    def test_resource_allocator_caps_assignment_edges(self):
        workers = tuple(
            snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 8,
                "objects": [{"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}] + [
                    {"kind": "UNIT", "id": f"worker-{i}", "controlled": True,
                     "position": [i, 0], "unit_type": "WORKER", "cargo": 0}
                    for i in range(8)
                ], "events": []}).workers
        )
        resources = tuple((i, j) for i in range(16) for j in range(2))
        result = allocate_visible_resources(workers, resources,
            lambda worker, resource: PathResult("RIGHT", "FOUND", 1, 1, resource))
        self.assertLessEqual(len(result), 8)

    def test_official_upkeep_and_overflow_events_are_idempotently_accounted(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True,
                "position": [0, 0], "hp": 5, "shield": 5, "state": "NORMAL"}
        events = [
            {"event_id": "upkeep", "event_type": "UPKEEP_PAID", "actor_id": "core",
             "position": [0, 0], "values": {"due": 4, "paid": 3, "deficit": 1}},
            {"event_id": "overflow", "event_type": "CORE_RESOURCE_OVERFLOW_DESTROYED", "actor_id": "core",
             "position": [0, 0], "values": {"amount": 6, "capacity": 10}},
        ]
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 4, "population": 1,
                                     "objects": [core], "events": events})
        economy_plan(s, mem)
        economy_plan(s, mem)
        self.assertEqual(list(mem.ledger.upkeep_events), [{"due": 4, "paid": 3, "deficit": 1}])
        self.assertEqual(mem.ledger.resource_overflow_amount, 6)

    def test_snapshot_parses_current_core_economy_fields(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 12, "population": 1,
            "upkeep_next_tick": 0,
            "objects": [{"kind": "CORE", "id": "core", "controlled": True,
                         "position": [0, 0], "hp": 4, "shield": 3, "state": "NORMAL"}],
            "events": []})
        self.assertEqual((s.core_hp, s.core_shield, s.core_state, s.upkeep_next_tick), (4, 3, "NORMAL", 0))

    def test_core_defense_heals_then_repairs_shield(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0],
                "hp": 4, "shield": 3, "state": "NORMAL"}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [4, 0], "unit_type": "WORKER", "cargo": 0}
        p = economy_plan(snapshot_from_state(1, {"status": "ACTIVE", "resources": 12, "population": 1,
            "objects": [core, worker], "events": []}), ExplorationMemory())
        self.assertEqual(p.core_action, {"type": "HEAL"})
        core["hp"] = 5
        p = economy_plan(snapshot_from_state(2, {"status": "ACTIVE", "resources": 12, "population": 1,
            "objects": [core, worker], "events": []}), ExplorationMemory())
        self.assertEqual(p.core_action, {"type": "REPAIR_SHIELD"})

    def test_core_defense_yields_to_full_core_or_recent_damage(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0],
                "hp": 4, "shield": 3, "state": "NORMAL"}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [0, 0], "unit_type": "WORKER", "cargo": 1}
        full = {"event_id": "full", "event_type": "DEPOSIT_FAILED", "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]}
        p = economy_plan(snapshot_from_state(1, {"status": "ACTIVE", "resources": 10, "population": 1,
            "objects": [core, worker], "events": [full]}), ExplorationMemory())
        self.assertEqual(p.core_action, {"type": "SPAWN", "unit_type": "WORKER"})
        damaged = {"event_id": "damage", "event_type": "CORE_DAMAGED", "reason_code": "ATTACK", "target_id": "core", "position": [0, 0]}
        p = economy_plan(snapshot_from_state(2, {"status": "ACTIVE", "resources": 12, "population": 1,
            "objects": [core], "events": [damaged]}), ExplorationMemory())
        self.assertIsNone(p.core_action)

    def test_core_ingress_holds_only_nearby_nonleader_carriers(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        near = {"kind": "UNIT", "id": "near", "controlled": True,
                "position": [2, 0], "unit_type": "WORKER", "cargo": 1}
        far = {"kind": "UNIT", "id": "far", "controlled": True,
               "position": [9, 0], "unit_type": "WORKER", "cargo": 1}
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 2,
                                     "objects": [core, near, far], "events": []})
        p = economy_plan(s, ExplorationMemory())
        self.assertEqual(p.unit_actions["near"], {"type": "MOVE", "direction": "LEFT"})
        self.assertNotEqual(p.unit_actions["far"], {"type": "WAIT"})

    def test_dynamic_move_failure_avoids_same_edge_next_tick(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [0, 0], "unit_type": "WORKER", "cargo": 0}
        resource = {"kind": "RESOURCE", "positions": [[2, 0]]}
        first = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 1,
                                        "objects": [core, worker, resource], "events": []})
        p = economy_plan(first, mem)
        self.assertEqual(p.unit_actions["worker"], {"type": "MOVE", "direction": "RIGHT"})
        failed = {"event_id": "blocked-right", "event_type": "UNIT_MOVE_FAILED",
                  "reason_code": "MOVE_DESTINATION_OCCUPIED", "actor_id": "worker", "position": [0, 0]}
        second = snapshot_from_state(2, {"status": "ACTIVE", "resources": 5, "population": 1,
                                         "objects": [core, worker, resource], "events": [failed]})
        p = economy_plan(second, mem)
        self.assertNotEqual(p.unit_actions["worker"], {"type": "MOVE", "direction": "RIGHT"})
        self.assertIn(p.unit_actions["worker"]["type"], {"MOVE", "WAIT"})

    def test_traffic_reserves_shared_destination(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker-a", "controlled": True,
                 "position": [0, 1], "unit_type": "WORKER", "cargo": 0},
                {"kind": "UNIT", "id": "worker-b", "controlled": True,
                 "position": [2, 1], "unit_type": "WORKER", "cargo": 0},
                {"kind": "RESOURCE", "positions": [[1, 1]]},
            ], "events": []})
        p = economy_plan(s, ExplorationMemory())
        moves = [action for action in p.unit_actions.values() if action == {"type": "MOVE", "direction": "RIGHT"}]
        self.assertLessEqual(len(moves), 1)

    def test_snapshot_parses_current_unit_hp(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0, "hp": 1},
            ], "events": []})
        self.assertEqual(s.workers[0].hp, 1)

    def test_injured_empty_worker_returns_to_core_and_heals(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        away = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [core, {"kind": "UNIT", "id": "worker", "controlled": True,
                "position": [2, 0], "unit_type": "WORKER", "cargo": 0, "hp": 1},
                {"kind": "RESOURCE", "positions": [[2, 1]]}], "events": []})
        p = economy_plan(away, ExplorationMemory())
        self.assertEqual(p.policy_state, "RETURN_HEAL")
        self.assertEqual(p.unit_actions["worker"], {"type": "MOVE", "direction": "LEFT"})
        at_core = snapshot_from_state(2, {"status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [core, {"kind": "UNIT", "id": "worker", "controlled": True,
                "position": [0, 0], "unit_type": "WORKER", "cargo": 0, "hp": 1}], "events": []})
        p = economy_plan(at_core, ExplorationMemory())
        self.assertEqual(p.policy_state, "HEAL_WORKER")
        self.assertEqual(p.unit_actions["worker"], {"type": "HEAL"})

    def test_injured_carrying_worker_still_returns_to_deposit(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [2, 0], "unit_type": "WORKER", "cargo": 1, "hp": 1},
            ], "events": []})
        p = economy_plan(s, ExplorationMemory())
        self.assertEqual(p.policy_state, "RETURN_CORE")
        self.assertEqual(p.unit_actions["worker"], {"type": "MOVE", "direction": "LEFT"})

    def test_worker_frontier_radius_is_bounded_and_rescans_own_ring(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [0, 0], "unit_type": "WORKER", "cargo": 0}
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 1,
                                     "objects": [core, worker], "events": []})
        mem = ExplorationMemory(route_core=(0, 0), route_core_id="core", band_radius=75)
        mem.frontier_candidates.extend([(75, 0)])
        target, path = mem.next_frontier(s, s.workers[0], frozenset(), set(), max_radius=21)
        self.assertIsNotNone(target)
        self.assertLessEqual(max(abs(target[0]), abs(target[1])), 21)
        self.assertEqual(path.status, "FOUND")

    def test_event_ledger_records_harvest_to_deposit_latency(self):
        mem = ExplorationMemory()
        harvest = {"event_id": "h", "event_type": "HARVEST_SUCCEEDED", "actor_id": "worker",
                   "position": [1, 0], "values": {"amount": 1}}
        deposit = {"event_id": "d", "event_type": "DEPOSIT_SUCCEEDED", "actor_id": "worker",
                   "position": [0, 0], "values": {"amount": 1}}
        for tick, event, cargo, pos in [(10, harvest, 1, [1, 0]), (17, deposit, 0, [0, 0])]:
            s = snapshot_from_state(tick, {"status": "ACTIVE", "resources": 6, "population": 1,
                "objects": [
                    {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                    {"kind": "UNIT", "id": "worker", "controlled": True,
                     "position": pos, "unit_type": "WORKER", "cargo": cargo},
                ], "events": [event]})
            economy_plan(s, mem)
        self.assertEqual(list(mem.ledger.deposit_latencies), [7])

    def test_snapshot_replaces_visible_objects(self):
        s = self.state()
        self.assertEqual(s.core_position, (0,0))
        self.assertEqual(s.resource_cells, frozenset({(2,2)}))
        self.assertEqual(s.workers[0].cargo, 0)
    def test_path_avoids_obstacle(self):
        self.assertEqual(first_step((2,0), {(2,2)}, frozenset({(1,0)}), {(2,0)}), "DOWN")
    def test_worker_moves_to_resource(self):
        p = economy_plan(self.state(), ExplorationMemory())
        self.assertEqual(p.unit_actions["worker"], {"type":"MOVE","direction":"DOWN"})

    def test_worker_explores_when_no_resource_is_visible(self):
        s = snapshot_from_state(1, {
            "status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 0},
            ], "events": []})
        p = economy_plan(s, ExplorationMemory())
        self.assertEqual(p.policy_state, "EXPLORE")
        self.assertEqual(p.unit_actions["worker"]["type"], "MOVE")

    def test_worker_harvests_visible_resource(self):
        s = snapshot_from_state(1, {
            "status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0},
                {"kind": "RESOURCE", "positions": [[1, 0]]},
            ], "events": []})
        p = economy_plan(s, ExplorationMemory())
        self.assertEqual(p.policy_state, "HARVEST")
        self.assertEqual(p.unit_actions["worker"], {"type": "HARVEST"})

    def test_worker_returns_and_deposits_cargo(self):
        s = snapshot_from_state(1, {
            "status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
            ], "events": []})
        p = economy_plan(s, ExplorationMemory())
        self.assertEqual(p.policy_state, "DEPOSIT")
        self.assertEqual(p.unit_actions["worker"], {"type": "DEPOSIT"})

    def test_exploration_starts_at_medium_range(self):
        s = snapshot_from_state(1, {
            "status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [10, 10]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [10, 10], "unit_type": "WORKER", "cargo": 0},
            ], "events": []})
        mem = ExplorationMemory()
        p = economy_plan(s, mem)
        self.assertEqual(p.policy_state, "EXPLORE")
        self.assertGreaterEqual(abs(p.waypoint[0] - 10) + abs(p.waypoint[1] - 10), 9)

    def test_return_home_uses_permanent_obstacle_memory(self):
        mem = ExplorationMemory(permanent_obstacles={(1, 0)})
        # The obstacle is intentionally absent from this current frame: return
        # planning must retain the previously observed obstacle and go around.
        s = snapshot_from_state(2, {
            "status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [2, 0], "unit_type": "WORKER", "cargo": 1},
            ], "events": []})
        p = economy_plan(s, mem)
        self.assertEqual(p.policy_state, "RETURN_CORE")
        self.assertIn(p.unit_actions["worker"]["direction"], {"UP", "DOWN"})

    def test_frontier_expands_without_terminal_exhaustion(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [0, 0], "unit_type": "WORKER", "cargo": 0}
        mem = ExplorationMemory()
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 1,
                                     "objects": [core, worker], "events": []})
        # Exhausting a band must refill the next band, then keep reseeding the
        # capped band rather than returning the former EXPLORATION_EXHAUSTED.
        for _ in range(20):
            mem.frontier_candidates.clear()
            target, result = mem.next_frontier(s, s.workers[0], frozenset(), set())
            self.assertIsNotNone(target)
            self.assertEqual(result.status, "FOUND")
            mem.active_target = None
        self.assertGreaterEqual(mem.band_radius, MAX_BAND_RADIUS)
        p = economy_plan(s, mem)
        self.assertNotEqual(p.policy_state, "EXPLORATION_EXHAUSTED")
        self.assertIn(p.policy_state, {"EXPLORE", "NO_FRONTIER"})

    def test_frontier_rebuilds_after_stale_candidate_batch(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [0, 0], "unit_type": "WORKER", "cargo": 0}
        mem = ExplorationMemory()
        s = snapshot_from_state(10, {"status": "ACTIVE", "resources": 8, "population": 1,
                                      "objects": [core, worker], "events": []})
        mem.route_core = (0, 0)
        mem.band_radius = 15
        mem.frontier_candidates.extend([(9, 0), (0, 9)])
        mem.failed_targets[(9, 0)] = (1, 99)
        mem.failed_targets[(0, 9)] = (1, 99)
        p = economy_plan(s, mem)
        self.assertEqual(p.policy_state, "EXPLORE")
        self.assertEqual(p.unit_actions["worker"]["type"], "MOVE")
        self.assertGreater(mem.band_radius, 15)

    def test_visible_resource_preempts_frontier(self):
        mem = ExplorationMemory()
        s = snapshot_from_state(3, {"status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 0},
                {"kind": "RESOURCE", "positions": [[1, 0]]},
            ], "events": []})
        p = economy_plan(s, mem)
        self.assertEqual(p.policy_state, "TO_RESOURCE")
        self.assertEqual(p.active_target, (1, 0))
        self.assertEqual(p.unit_actions["worker"], {"type": "MOVE", "direction": "RIGHT"})

    def test_core_full_pauses_deposit_without_dropping_cargo(self):
        mem = ExplorationMemory()
        full_event = {
            "event_id": "full-1", "event_type": "DEPOSIT_FAILED",
            "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0],
            "values": {"capacity": 10},
        }
        s = snapshot_from_state(2, {"status": "ACTIVE", "resources": 4, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
            ], "events": [full_event]})
        p = economy_plan(s, mem)
        self.assertTrue(mem.core_full)
        self.assertEqual(p.policy_state, "CORE_FULL")
        self.assertEqual(p.unit_actions["worker"], {"type": "WAIT"})

    def test_authoritative_spare_capacity_clears_core_full_pause(self):
        mem = ExplorationMemory(core_full=True)
        s = snapshot_from_state(4, {"status": "ACTIVE", "resources": 5, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "respawned-core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
            ], "events": [{"event_id": "respawn-1", "event_type": "CORE_RESPAWNED"}]})
        p = economy_plan(s, mem)
        self.assertFalse(mem.core_full)
        self.assertEqual(p.policy_state, "DEPOSIT")
        self.assertEqual(p.unit_actions["worker"], {"type": "DEPOSIT"})

    def test_deposit_success_clears_core_full_pause(self):
        mem = ExplorationMemory(core_full=True)
        success_event = {
            "event_id": "deposit-1", "event_type": "DEPOSIT_SUCCEEDED",
            "position": [0, 0], "values": {"amount": 1, "remaining": 0},
        }
        s = snapshot_from_state(3, {"status": "ACTIVE", "resources": 9, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
            ], "events": [success_event]})
        p = economy_plan(s, mem)
        self.assertFalse(mem.core_full)
        self.assertEqual(p.policy_state, "DEPOSIT")

    def test_multi_worker_claims_distinct_resources(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 8, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker-a", "controlled": True,
                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0},
                {"kind": "UNIT", "id": "worker-b", "controlled": True,
                 "position": [5, 0], "unit_type": "WORKER", "cargo": 0},
                {"kind": "RESOURCE", "positions": [[1, 1], [5, 1]]},
            ], "events": []})
        p = economy_plan(s, ExplorationMemory())
        self.assertEqual(p.unit_actions["worker-a"], {"type": "MOVE", "direction": "DOWN"})
        self.assertEqual(p.unit_actions["worker-b"], {"type": "MOVE", "direction": "DOWN"})

    def test_multi_worker_does_not_compete_for_same_resource(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 8, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker-a", "controlled": True,
                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0},
                {"kind": "UNIT", "id": "worker-b", "controlled": True,
                 "position": [3, 0], "unit_type": "WORKER", "cargo": 0},
                {"kind": "RESOURCE", "positions": [[1, 1]]},
            ], "events": []})
        p = economy_plan(s, ExplorationMemory())
        resource_moves = [action for action in (p.unit_actions["worker-a"], p.unit_actions["worker-b"])
                          if action == {"type": "MOVE", "direction": "DOWN"}]
        self.assertEqual(len(resource_moves), 1)
        self.assertIn(p.unit_actions["worker-b"]["type"], {"MOVE", "WAIT"})

    def test_non_worker_explicitly_waits_while_worker_explores(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 8, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 0},
                {"kind": "UNIT", "id": "vanguard", "controlled": True,
                 "position": [1, 0], "unit_type": "VANGUARD", "cargo": 0},
            ], "events": []})
        p = economy_plan(s, ExplorationMemory())
        self.assertEqual(p.unit_actions["vanguard"], {"type": "WAIT"})
        self.assertEqual(p.unit_actions["worker"]["type"], "MOVE")

    def test_event_ledger_deduplicates_deposit_event(self):
        event = {"event_id": "deposit-once", "event_type": "DEPOSIT_SUCCEEDED",
                 "actor_id": "worker", "position": [0, 0], "values": {"amount": 1}}
        mem = ExplorationMemory()
        for tick in (1, 2):
            s = snapshot_from_state(tick, {"status": "ACTIVE", "resources": 6, "population": 1,
                "objects": [
                    {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                    {"kind": "UNIT", "id": "worker", "controlled": True,
                     "position": [0, 0], "unit_type": "WORKER", "cargo": 0},
                ], "events": [event]})
            economy_plan(s, mem)
        self.assertEqual(list(mem.ledger.deposits), [1])

    def test_spawn_worker_requires_economy_and_safety_gates(self):
        mem = ExplorationMemory()
        mem.ledger.deposits.extend([1, 2, 3])
        s = snapshot_from_state(4, {"status": "ACTIVE", "resources": 12, "population": 1,
            "objects": [{"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}],
            "events": []})
        p = economy_plan(s, mem)
        self.assertEqual(p.core_action, {"type": "SPAWN", "unit_type": "WORKER"})
        mem.ledger.core_damage_ticks.append(4)
        self.assertIsNone(economy_plan(s, mem).core_action)

    def test_spawn_worker_is_blocked_by_core_occupancy_or_worker_cap(self):
        mem = ExplorationMemory()
        mem.ledger.deposits.extend([1, 2, 3])
        occupied = snapshot_from_state(4, {"status": "ACTIVE", "resources": 12, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 0},
            ], "events": []})
        self.assertIsNone(economy_plan(occupied, mem).core_action)
        two_workers = snapshot_from_state(5, {"status": "ACTIVE", "resources": 12, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker-a", "controlled": True,
                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0},
                {"kind": "UNIT", "id": "worker-b", "controlled": True,
                 "position": [2, 0], "unit_type": "WORKER", "cargo": 0},
            ], "events": []})
        self.assertEqual(economy_plan(two_workers, mem).core_action,
                         {"type": "SPAWN", "unit_type": "WORKER"})
        eight_workers = snapshot_from_state(6, {"status": "ACTIVE", "resources": 40, "population": 8,
            "objects": [{"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}] + [
                {"kind": "UNIT", "id": f"worker-{i}", "controlled": True,
                 "position": [i + 1, 0], "unit_type": "WORKER", "cargo": 0}
                for i in range(MAX_ECONOMY_WORKERS)
            ], "events": []})
        self.assertIsNone(economy_plan(eight_workers, mem).core_action)

    def test_core_full_recovery_moves_carrier_and_spawns_worker(self):
        mem = ExplorationMemory()
        full_event = {"event_id": "full-recovery", "event_type": "DEPOSIT_FAILED",
                      "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]}
        s = snapshot_from_state(8, {"status": "ACTIVE", "resources": 10, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
            ], "events": [full_event]})
        p = economy_plan(s, mem)
        self.assertEqual(p.policy_state, "CORE_FULL_RECOVERY")
        self.assertEqual(p.unit_actions["worker"], {"type": "MOVE", "direction": "UP"})
        self.assertEqual(p.core_action, {"type": "SPAWN", "unit_type": "WORKER"})

    def test_two_worker_core_full_recovery_spawns_third_worker(self):
        mem = ExplorationMemory()
        full_event = {"event_id": "full-two-worker", "event_type": "DEPOSIT_FAILED",
                      "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]}
        s = snapshot_from_state(9, {"status": "ACTIVE", "resources": 10, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "carrier", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
                {"kind": "UNIT", "id": "scout", "controlled": True,
                 "position": [2, 0], "unit_type": "WORKER", "cargo": 0},
            ], "events": [full_event]})
        p = economy_plan(s, mem)
        self.assertEqual(p.policy_state, "CORE_FULL_RECOVERY")
        self.assertEqual(p.unit_actions["carrier"], {"type": "MOVE", "direction": "UP"})
        self.assertEqual(p.core_action, {"type": "SPAWN", "unit_type": "WORKER"})

    def test_core_full_recovery_requires_empty_core_slot_and_worker_cap(self):
        full_event = {"event_id": "full-guard", "event_type": "DEPOSIT_FAILED",
                      "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]}
        occupied = snapshot_from_state(9, {"status": "ACTIVE", "resources": 10, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "carrier", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
                {"kind": "UNIT", "id": "blocker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 0},
            ], "events": [full_event]})
        self.assertIsNone(economy_plan(occupied, ExplorationMemory()).core_action)
        capped = snapshot_from_state(10, {"status": "ACTIVE", "resources": 40, "population": 8,
            "objects": [{"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}] + [
                {"kind": "UNIT", "id": f"worker-{i}", "controlled": True,
                 "position": [i + 1, 0], "unit_type": "WORKER", "cargo": 1 if i == 0 else 0}
                for i in range(MAX_ECONOMY_WORKERS)
            ], "events": [full_event]})
        self.assertIsNone(economy_plan(capped, ExplorationMemory()).core_action)

    def test_capacity_recovery_scales_from_three_to_five_workers(self):
        full_event = {"event_id": "full-3", "event_type": "DEPOSIT_FAILED",
                      "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]}
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        for population in (3, 4):
            workers = [
                {"kind": "UNIT", "id": "leader", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
            ] + [
                {"kind": "UNIT", "id": f"worker-{i}", "controlled": True,
                 "position": [i + 1, 0], "unit_type": "WORKER", "cargo": 0}
                for i in range(population - 1)
            ]
            s = snapshot_from_state(population, {"status": "ACTIVE", "resources": population * 5,
                "population": population, "objects": [core, *workers], "events": [full_event]})
            p = economy_plan(s, ExplorationMemory())
            self.assertEqual(p.policy_state, "CORE_FULL_RECOVERY")
            self.assertEqual(p.core_action, {"type": "SPAWN", "unit_type": "WORKER"})
            self.assertEqual(p.unit_actions["leader"], {"type": "MOVE", "direction": "UP"})
            self.assertEqual(s.resource_capacity, population * 5)

    def test_capacity_recovery_sequence_from_three_to_four_then_next_full(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        leader = {"kind": "UNIT", "id": "leader", "controlled": True,
                  "position": [0, 0], "unit_type": "WORKER", "cargo": 1}
        others = [
            {"kind": "UNIT", "id": "worker-a", "controlled": True,
             "position": [2, 0], "unit_type": "WORKER", "cargo": 1},
            {"kind": "UNIT", "id": "worker-b", "controlled": True,
             "position": [3, 0], "unit_type": "WORKER", "cargo": 1},
        ]
        full = {"event_id": "full-seq-1", "event_type": "DEPOSIT_FAILED",
                "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]}
        first = snapshot_from_state(10, {"status": "ACTIVE", "resources": 15, "population": 3,
            "objects": [core, leader, *others], "events": [full]})
        p = economy_plan(first, mem)
        self.assertEqual(p.core_action, {"type": "SPAWN", "unit_type": "WORKER"})
        self.assertEqual(p.unit_actions["leader"], {"type": "MOVE", "direction": "UP"})
        self.assertEqual(p.unit_actions["worker-a"], {"type": "WAIT"})

        spawned = {"event_id": "spawn-seq-1", "event_type": "CORE_SPAWN_SUCCEEDED",
                   "target_id": "worker-c", "position": [0, 0],
                   "values": {"unit_type": "WORKER", "cost": 5}}
        delivered = {"event_id": "deposit-seq-1", "event_type": "DEPOSIT_SUCCEEDED",
                     "actor_id": "leader", "position": [0, 0],
                     "values": {"amount": 1, "capacity": 20, "remaining": 0}}
        after = snapshot_from_state(11, {"status": "ACTIVE", "resources": 16, "population": 4,
            "objects": [core,
                {"kind": "UNIT", "id": "leader", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 0},
                *others,
                {"kind": "UNIT", "id": "worker-c", "controlled": True,
                 "position": [0, 1], "unit_type": "WORKER", "cargo": 0},
            ], "events": [spawned, delivered]})
        p = economy_plan(after, mem)
        self.assertEqual(after.resource_capacity, 20)
        self.assertFalse(mem.core_full)
        self.assertIsNone(p.core_action)

        next_full = {"event_id": "full-seq-2", "event_type": "DEPOSIT_FAILED",
                     "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]}
        fourth = snapshot_from_state(12, {"status": "ACTIVE", "resources": 20, "population": 4,
            "objects": [core,
                {"kind": "UNIT", "id": "leader", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
                *others,
                {"kind": "UNIT", "id": "worker-c", "controlled": True,
                 "position": [1, 1], "unit_type": "WORKER", "cargo": 0},
            ], "events": [next_full]})
        p = economy_plan(fourth, mem)
        self.assertEqual(p.core_action, {"type": "SPAWN", "unit_type": "WORKER"})
        self.assertEqual(p.unit_actions["leader"], {"type": "MOVE", "direction": "UP"})

    def test_vanguard_sweeps_only_visible_adjacent_enemy(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [1, 1], "unit_type": "VANGUARD", "cargo": 0},
                {"kind": "UNIT", "id": "enemy-adjacent", "controlled": False,
                 "position": [1, 0], "unit_type": "WORKER"},
                {"kind": "UNIT", "id": "enemy-far", "controlled": False,
                 "position": [4, 1], "unit_type": "WORKER"},
            ], "events": []})
        p = economy_plan(s, ExplorationMemory())
        self.assertEqual(p.unit_actions["guard"], {"type": "SWEEP", "direction": "UP"})

    def test_vanguard_waits_without_visible_adjacent_enemy(self):
        s = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [1, 1], "unit_type": "VANGUARD", "cargo": 0},
                {"kind": "UNIT", "id": "enemy-far", "controlled": False,
                 "position": [4, 1], "unit_type": "WORKER"},
            ], "events": []})
        self.assertEqual(economy_plan(s, ExplorationMemory()).unit_actions["guard"], {"type": "WAIT"})

    def test_core_full_holds_carrier_outside_reserved_core(self):
        mem = ExplorationMemory()
        full_event = {"event_id": "full-hold", "event_type": "DEPOSIT_FAILED",
                      "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]}
        s = snapshot_from_state(21, {"status": "ACTIVE", "resources": 10, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "leader", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
                {"kind": "UNIT", "id": "outside", "controlled": True,
                 "position": [0, 1], "unit_type": "WORKER", "cargo": 1},
            ], "events": [full_event]})
        p = economy_plan(s, mem)
        self.assertEqual(p.policy_state, "CORE_FULL_RECOVERY")
        self.assertEqual(p.unit_actions["leader"], {"type": "MOVE", "direction": "UP"})
        self.assertEqual(p.unit_actions["outside"], {"type": "WAIT"})
        self.assertEqual(p.core_action, {"type": "SPAWN", "unit_type": "WORKER"})

    def test_two_carriers_evict_before_core_full_recovery_spawn(self):
        mem = ExplorationMemory()
        full_event = {"event_id": "double-full", "event_type": "DEPOSIT_FAILED",
                      "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]}
        s = snapshot_from_state(20, {"status": "ACTIVE", "resources": 10, "population": 2,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "carrier-a", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
                {"kind": "UNIT", "id": "carrier-b", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
            ], "events": [full_event]})
        p = economy_plan(s, mem)
        self.assertEqual(p.policy_state, "CORE_FULL_EVICT")
        self.assertEqual(p.unit_actions["carrier-a"], {"type": "WAIT"})
        self.assertEqual(p.unit_actions["carrier-b"], {"type": "MOVE", "direction": "UP"})
        self.assertIsNone(p.core_action)

    def test_core_spawn_failure_applies_recovery_cooldown(self):
        mem = ExplorationMemory()
        events = [
            {"event_id": "full-cooldown", "event_type": "DEPOSIT_FAILED",
             "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]},
            {"event_id": "spawn-cooldown", "event_type": "CORE_SPAWN_FAILED",
             "reason_code": "CELL_UNIT_LIMIT", "position": [0, 0]},
        ]
        s = snapshot_from_state(20, {"status": "ACTIVE", "resources": 10, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "carrier", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
            ], "events": events})
        p = economy_plan(s, mem)
        self.assertGreater(mem.recovery_cooldown_until, 20)
        self.assertEqual(p.policy_state, "CORE_FULL")
        self.assertIsNone(p.core_action)

    def test_core_full_recovery_is_disabled_after_core_damage(self):
        mem = ExplorationMemory()
        events = [
            {"event_id": "damage", "event_type": "CORE_DAMAGED", "position": [0, 0]},
            {"event_id": "full-after-damage", "event_type": "DEPOSIT_FAILED",
             "reason_code": "CORE_RESOURCE_FULL", "position": [0, 0]},
        ]
        s = snapshot_from_state(8, {"status": "ACTIVE", "resources": 10, "population": 1,
            "objects": [
                {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]},
                {"kind": "UNIT", "id": "worker", "controlled": True,
                 "position": [0, 0], "unit_type": "WORKER", "cargo": 1},
            ], "events": events})
        p = economy_plan(s, mem)
        self.assertEqual(p.policy_state, "CORE_FULL")
        self.assertEqual(p.unit_actions["worker"], {"type": "WAIT"})
        self.assertIsNone(p.core_action)

    def test_path_result_reports_no_path_and_node_cap(self):
        no_path = plan_path((0, 0), {(2, 0)}, frozenset({(1, 0), (1, 1), (1, -1)}), set())
        self.assertIn(no_path.status, {"FOUND", "NO_PATH", "NODE_CAP"})
        capped = plan_path((0, 0), {(10_000, 0)}, frozenset(), set())
        self.assertIn(capped.status, {"FOUND", "NODE_CAP", "NO_PATH"})
        self.assertLessEqual(capped.explored_nodes, PATH_NODE_CAP + 4)

    def test_live_deploy_template_is_token_only(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "deploy/supervisor-arena-hero.conf").read_text()
        deploy = (root / "deploy/pxed-deploy.sh").read_text()
        self.assertIn("export ARENA_HERO_TOKEN", template)
        self.assertNotIn("ARENA_HERO_COOKIE", template)
        self.assertNotIn("ARENA_HERO_CSRF", template)
        self.assertIn("__ARENA_PYTHON_BIN__", template)
        self.assertIn("__ARENA_PYTHON_BIN__", deploy)
        self.assertIn("import websockets", deploy)

    def test_unreachable_target_returns_without_unbounded_search(self):
        step = first_step((0, 0), {(2, 0)}, frozenset({(1, 0), (1, 1), (1, -1)}), {(0, 0)})
        self.assertIn(step, {"UP", "DOWN", None})
    def test_worker_deposits_at_core(self):
        data = self.state()
        s = snapshot_from_state(1, {"status":"ACTIVE","resources":5,"population":1,"objects":[
            {"kind":"CORE","id":"core","controlled":True,"position":[0,0]},
            {"kind":"UNIT","id":"worker","controlled":True,"position":[0,0],"unit_type":"WORKER","cargo":1}],"events":[]})
        self.assertEqual(economy_plan(s).unit_actions["worker"], {"type":"DEPOSIT"})
    def test_respawning_waits(self):
        s = self.state(status="RESPAWNING")
        self.assertEqual(economy_plan(s).unit_actions, {})

    def test_journal_does_not_log_full_state_to_python_log(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            j = Journal(f"{d}/journal.jsonl")
            j.write("plan", session="s", tick=1,
                    state={"status": "ACTIVE", "objects": {"total": 1}},
                    plan={"unit_actions": {}}, result={"status": 202})
            from pathlib import Path
            self.assertIn('"objects":{"total":1}', Path(f"{d}/journal.jsonl").read_text())

    def test_tick_mismatch_is_marked_stale_without_hiding_other_conflicts(self):
        async def run(status: str, body: str):
            with patch("arena_agent.__main__.subprocess.run") as mocked:
                mocked.return_value = type("Completed", (), {
                    "returncode": 0, "stdout": status, "stderr": ""
                })()
                with patch("builtins.open", mock_open(read_data=body)):
                    return await post_plan("token", 9, {"unit_actions": {}}, False)
        stale = asyncio.run(run("409", '{"accepted":false,"error":"TICK_MISMATCH"}'))
        conflict = asyncio.run(run("409", '{"accepted":false,"error":"IDEMPOTENCY_CONFLICT"}'))
        self.assertTrue(stale["stale_tick"])
        self.assertNotIn("stale_tick", conflict)

    def test_command_auth_failure_is_permanent(self):
        async def run():
            with patch("arena_agent.__main__.subprocess.run") as mocked:
                mocked.return_value = type("Completed", (), {
                    "returncode": 0, "stdout": "401", "stderr": ""
                })()
                with self.assertRaises(PermanentAuthError):
                    await post_plan("token", 9, {"unit_actions": {}}, False)
        asyncio.run(run())

if __name__ == "__main__": unittest.main()
