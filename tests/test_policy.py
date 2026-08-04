import asyncio
import unittest
from unittest.mock import mock_open, patch
from arena_agent.allocator import allocate_visible_resources
from arena_agent.combat import intermediate_cells, ranger_line_distance, ranger_target
from arena_agent.model import Unit, snapshot_from_state
from arena_agent.path import (FRONTIER_PATH_NODE_CAP, MAX_FRONTIER_PATH_EVALUATIONS,
                              PathResult, plan_frontier_path)
from arena_agent.__main__ import (PermanentAuthError, allocator_metrics, operator_attention_metrics,
                                  population_control_metrics, post_plan, record_population_transition,
                                  record_received_source, record_session_baseline, stale_tick_reconnect_required)
from pathlib import Path
from arena_agent.policy import (
    ExplorationMemory, MAX_BAND_RADIUS, MAX_ECONOMY_WORKERS, MAX_EXTERNAL_RECOVERY_POPULATION, PATH_NODE_CAP,
    economy_plan, first_step, plan_path, ranger_fire_allowed,
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
    def test_frontier_astar_bounds_distant_search_and_routes_around_obstacle(self):
        direct = plan_frontier_path((0, 0), (500, 0), frozenset(), set(), node_cap=2_000)
        self.assertEqual((direct.status, direct.path_length, direct.direction), ("FOUND", 500, "RIGHT"))
        self.assertLess(direct.explored_nodes, 1_100)
        detour = plan_frontier_path((0, 0), (4, 0), frozenset({(1, 0), (2, 0), (3, 0)}), set())
        self.assertEqual(detour.status, "FOUND")
        self.assertIn(detour.direction, {"UP", "DOWN"})
        capped = plan_frontier_path((0, 0), (500, 0), frozenset(), set(), node_cap=4)
        self.assertEqual(capped.status, "NODE_CAP")

    def test_frontier_node_cap_uses_longer_bounded_retry_than_no_path(self):
        mem = ExplorationMemory()
        self.assertEqual(mem.frontier_retry_after("NODE_CAP", 0, 10), 18)
        self.assertEqual(mem.frontier_retry_after("NODE_CAP", 99, 10), 70)
        self.assertEqual(mem.frontier_retry_after("NO_PATH", 0, 10), 12)
        self.assertEqual(mem.frontier_retry_after("NO_PATH", 99, 10), 22)

    def test_stale_tick_circuit_breaker_rejoins_after_three_and_resets_on_accepted(self):
        streak = 0
        for expected in (1, 2, 3):
            streak, reconnect = stale_tick_reconnect_required(streak, {"stale_tick": True})
            self.assertEqual(streak, expected)
        self.assertTrue(reconnect)
        streak, reconnect = stale_tick_reconnect_required(streak, {"status": 202})
        self.assertEqual((streak, reconnect), (0, False))

    def test_frontier_budget_bounds_search_without_marking_budget_as_failure(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [1, 0], "unit_type": "WORKER", "cargo": 0}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": []})
        mem.begin_frontier_budget()
        mem.frontier_candidates.clear()
        mem.frontier_candidates.extend((1000 + offset, 0) for offset in range(20))
        target, result = mem.next_frontier(state, state.workers[0], frozenset(), set(), max_radius=2000)
        self.assertLessEqual(mem.frontier_path_evaluations, MAX_FRONTIER_PATH_EVALUATIONS)
        self.assertLessEqual(mem.frontier_path_nodes,
                             MAX_FRONTIER_PATH_EVALUATIONS * (FRONTIER_PATH_NODE_CAP + 4))
        self.assertNotIn("FRONTIER_BUDGET", mem.frontier_failure_reasons)
        self.assertIn(result.status, {"FOUND", "FRONTIER_BUDGET", "NO_PATH"})

    def test_frontier_completion_and_path_failure_reason_are_distinct_from_traffic(self):
        mem = ExplorationMemory()
        worker = Unit("worker", (3, 0), "WORKER", 0, 2)
        mem.active_targets["worker"] = (3, 0)
        mem.complete_frontier_if_reached(worker, 7)
        self.assertEqual(mem.completed_targets, {(3, 0): 7})
        self.assertNotIn("worker", mem.active_targets)
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        state = snapshot_from_state(8, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, {"kind": "UNIT", "id": "worker", "controlled": True,
                                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0}], "events": []})
        mem.frontier_candidates.clear()
        mem.frontier_candidates.append((3, 0))
        # Block every cardinal exit from (1, 0), so bounded BFS is truly NO_PATH.
        mem.next_frontier(state, state.workers[0],
                          frozenset({(2, 0), (1, 1), (1, -1), (0, 0)}), set())
        self.assertGreater(mem.frontier_failure_reasons.get("NO_PATH", 0), 0)
        before = dict(mem.frontier_failure_reasons)
        event = {"event_id": "traffic-only", "event_type": "UNIT_MOVE_FAILED",
                 "reason_code": "MOVE_CONTESTED", "actor_id": "worker", "position": [1, 0]}
        mem.apply_events(snapshot_from_state(9, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, {"kind": "UNIT", "id": "worker", "controlled": True,
                                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0}], "events": [event]}))
        self.assertEqual(mem.frontier_failure_reasons, before)

    def test_dynamic_move_failure_preserves_active_frontier_target(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [1, 0], "unit_type": "WORKER", "cargo": 0}
        initial = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": []})
        mem.observe(initial)
        mem.active_targets["worker"] = (10, 0)
        mem.traffic.mark_planned_move("worker", (1, 0), "RIGHT")
        failed = {"event_id": "move-failed", "event_type": "UNIT_MOVE_FAILED",
                  "reason_code": "MOVE_DESTINATION_OCCUPIED", "actor_id": "worker", "position": [1, 0]}
        mem.apply_events(snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": [failed]}))
        self.assertEqual(mem.active_targets["worker"], (10, 0))
        self.assertNotIn((10, 0), mem.failed_targets)
        self.assertTrue(mem.traffic.is_edge_blocked("worker", (1, 0), "RIGHT", 2))

    def test_allocator_metrics_distinguish_resource_starvation_from_unmatched_work(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        workers = [
            {"kind": "UNIT", "id": f"worker-{i}", "controlled": True,
             "position": [i + 1, 0], "unit_type": "WORKER", "cargo": 0}
            for i in range(3)
        ]
        empty = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 3,
            "objects": [core, *workers], "events": []})
        self.assertEqual(allocator_metrics(empty, 0, 0), {
            "eligible": 3, "visible_resources": 0, "matched": 0,
            "unmatched_eligible": 3, "resource_starved": True, "total_cost": 0,
        })
        visible = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 3,
            "objects": [core, *workers, {"kind": "RESOURCE", "positions": [[3, 0], [4, 0], [5, 0]]}], "events": []})
        self.assertEqual(allocator_metrics(visible, 2, 900), {
            "eligible": 3, "visible_resources": 3, "matched": 2,
            "unmatched_eligible": 1, "resource_starved": False, "total_cost": 900,
        })

    def test_unattributed_population_growth_marks_window_but_agent_spawn_does_not(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        def snapshot(population):
            return snapshot_from_state(population, {"status": "ACTIVE", "resources": 0,
                "population": population, "objects": [core], "events": []})
        audit = {"last_population": None, "unattributed_population_increases": 0,
                 "window_contaminated": False}
        record_population_transition(audit, snapshot(10), None)
        record_population_transition(audit, snapshot(11), None)
        self.assertEqual(audit["unattributed_population_increases"], 1)
        self.assertTrue(audit["window_contaminated"])
        audit = {"last_population": 11, "unattributed_population_increases": 0,
                 "window_contaminated": False}
        record_population_transition(audit, snapshot(12), "SPAWN")
        self.assertEqual(audit["unattributed_population_increases"], 0)
        self.assertFalse(audit["window_contaminated"])

    def test_operator_attention_marks_population_ceiling_hold_only(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        workers = [
            {"kind": "UNIT", "id": f"worker-{index}", "controlled": True,
             "position": [index, 0], "unit_type": "WORKER", "cargo": index % 2}
            for index in range(4)
        ]
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0,
            "population": 19, "objects": [core, *workers], "events": []})
        self.assertEqual(operator_attention_metrics(state, "RETURN_CORE"), {
            "required": False, "reason": None, "blocked_cargo_workers": 2,
        })
        self.assertEqual(operator_attention_metrics(state, "CORE_FULL_EXTERNAL_CAP_HOLD"), {
            "required": True, "reason": "external_recovery_population_ceiling",
            "blocked_cargo_workers": 2,
        })

    def test_population_control_metrics_marks_external_recovery_population_ceiling(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        workers = [
            {"kind": "UNIT", "id": f"worker-{index}", "controlled": True,
             "position": [index + 1, 0], "unit_type": "WORKER", "cargo": 0}
            for index in range(MAX_EXTERNAL_RECOVERY_POPULATION)
        ]
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0,
            "population": MAX_EXTERNAL_RECOVERY_POPULATION, "objects": [core, *workers], "events": []})
        self.assertEqual(population_control_metrics(state), {
            "worker_count": MAX_EXTERNAL_RECOVERY_POPULATION,
            "normal_worker_cap": MAX_ECONOMY_WORKERS,
            "external_recovery_population_ceiling": MAX_EXTERNAL_RECOVERY_POPULATION,
            "external_recovery_ceiling_reached": True,
        })

    def test_session_baseline_marks_external_over_cap_contamination(self):
        audit = {"manual_interventions": 0, "external_core_actions": 0,
                 "window_contaminated": False, "last_received": None,
                 "baseline_recorded": False, "baseline_contaminated": False,
                 "baseline_worker_count": None, "baseline_over_worker_cap": False}
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        workers = [
            {"kind": "UNIT", "id": f"worker-{index}", "controlled": True,
             "position": [index + 1, 0], "unit_type": "WORKER", "cargo": 0}
            for index in range(MAX_ECONOMY_WORKERS + 1)
        ]
        snapshot = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0,
            "population": 10, "objects": [core, *workers], "events": []})
        record_session_baseline(audit, snapshot)
        self.assertTrue(audit["window_contaminated"])
        self.assertEqual(audit["baseline_worker_count"], MAX_ECONOMY_WORKERS + 1)
        self.assertTrue(audit["baseline_over_worker_cap"])
        record_session_baseline(audit, snapshot)
        self.assertEqual(audit["baseline_worker_count"], MAX_ECONOMY_WORKERS + 1)

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
            mem.begin_frontier_budget()
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

    def test_friendly_combat_damage_and_cargo_drop_disable_ranger_fire(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 0], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [1, 0], "unit_type": "WORKER", "cargo": 1, "hp": 2}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [3, 0], "unit_type": "WORKER"}
        hit = {"event_id": "friendly-hit", "event_type": "UNIT_DAMAGED", "reason_code": "ATTACK",
               "target_id": "worker", "position": [1, 0], "values": {"damage": 1, "hp": 1}}
        state = snapshot_from_state(10, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, ranger, worker, enemy], "events": [hit]})
        plan = economy_plan(state, mem)
        self.assertFalse(ranger_fire_allowed(state, mem))
        self.assertEqual(plan.unit_actions["ranger"], {"type": "WAIT"})
        self.assertGreater(mem.ledger.combat_cooldown_until, 10)
        death = {"event_id": "friendly-death", "event_type": "UNIT_DAMAGED", "reason_code": "ATTACK",
                 "target_id": "worker", "position": [1, 0], "values": {"damage": 1, "hp": 0}}
        dropped = {"event_id": "cargo", "event_type": "WORKER_CARGO_DROPPED", "actor_id": "worker",
                   "position": [1, 0], "values": {"amount": 1}}
        after = snapshot_from_state(11, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, ranger, enemy], "events": [death, dropped]})
        mem.apply_events(after)
        self.assertGreaterEqual(mem.ledger.combat_cooldown_until, 11 + 40)
        self.assertEqual(list(mem.ledger.combat_cargo_drops)[-1]["amount"], 1)

    def test_enemy_combat_death_does_not_disable_ranger_fire(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 0], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [3, 0], "unit_type": "WORKER"}
        death = {"event_id": "enemy-death", "event_type": "UNIT_DAMAGED", "reason_code": "ATTACK",
                 "target_id": "enemy", "position": [3, 0], "values": {"damage": 1, "hp": 0}}
        state = snapshot_from_state(10, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, ranger, enemy], "events": [death]})
        plan = economy_plan(state, mem)
        self.assertTrue(ranger_fire_allowed(state, mem))
        self.assertEqual(plan.unit_actions["ranger"]["type"], "SHOOT")

    def test_ranger_shoots_only_current_visible_clear_legal_target(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [1, 1], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        enemy_unit = {"kind": "UNIT", "id": "enemy-unit", "controlled": False,
                      "position": [4, 1], "unit_type": "WORKER"}
        enemy_core = {"kind": "CORE", "id": "enemy-core", "controlled": False,
                      "position": [1, 3]}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, ranger, enemy_unit, enemy_core], "events": []})
        plan = economy_plan(state, ExplorationMemory())
        self.assertEqual(plan.unit_actions["ranger"], {
            "type": "SHOOT", "target_id": "enemy-unit", "expected_cell": [4, 1],
        })
        blocked = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, ranger, enemy_unit,
                        {"kind": "OBSTACLE", "positions": [[2, 1]]}], "events": []})
        self.assertEqual(economy_plan(blocked, ExplorationMemory()).unit_actions["ranger"], {"type": "WAIT"})
        diagonal = snapshot_from_state(3, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, ranger, {"kind": "UNIT", "id": "bad", "controlled": False,
                                        "position": [3, 2], "unit_type": "WORKER"}], "events": []})
        self.assertEqual(economy_plan(diagonal, ExplorationMemory()).unit_actions["ranger"], {"type": "WAIT"})

    def test_combat_episode_confirms_only_friendly_death_on_next_owned_state(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [1, 0], "unit_type": "WORKER", "cargo": 1, "hp": 2}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 0], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [3, 0], "unit_type": "WORKER"}
        economy_plan(snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, worker, ranger, enemy], "events": []}), mem)
        events = [
            {"event_id": "friendly-zero", "event_type": "UNIT_DAMAGED", "reason_code": "ATTACK",
             "target_id": "worker", "values": {"damage": 1, "hp": 0}},
            {"event_id": "enemy-zero", "event_type": "UNIT_DAMAGED", "reason_code": "ATTACK",
             "target_id": "enemy", "values": {"damage": 1, "hp": 0}},
            {"event_id": "enemy-participation", "event_type": "DESTRUCTION_PARTICIPATION",
             "target_id": "enemy", "values": {}},
            {"event_id": "drop", "event_type": "WORKER_CARGO_DROPPED", "actor_id": "worker",
             "values": {"amount": 1}},
        ]
        lethal = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, worker, ranger, enemy], "events": events})
        economy_plan(lethal, mem)
        self.assertEqual(set(mem.ledger.pending_friendly_deaths), {"worker"})
        self.assertEqual(mem.ledger.combat_episode["enemy_destruction_participations"], 1)
        self.assertEqual(mem.ledger.combat_episode["friendly_deaths"], 0)
        confirmed = snapshot_from_state(3, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, ranger], "events": []})
        economy_plan(confirmed, mem)
        self.assertEqual(mem.ledger.combat_episode["friendly_deaths"], 1)
        self.assertEqual(mem.ledger.combat_episode["enemy_destruction_participations"], 1)
        self.assertFalse(mem.ledger.pending_friendly_deaths)

    def test_combat_episode_closes_after_idle_window(self):
        mem = ExplorationMemory()
        episode = mem.ledger.combat_touch(10)
        episode["shots_hit"] = 1
        mem.ledger.combat_close_if_idle(17)
        self.assertIsNotNone(mem.ledger.combat_episode)
        mem.ledger.combat_close_if_idle(18)
        self.assertIsNone(mem.ledger.combat_episode)
        self.assertEqual(mem.ledger.completed_combat_episodes[-1]["shots_hit"], 1)
        self.assertEqual(mem.ledger.completed_combat_episodes[-1]["end_tick"], 10)

    def test_combat_event_ledger_is_idempotent_and_tracks_damage_candidate(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [1, 0], "unit_type": "WORKER", "cargo": 0}
        events = [
            {"event_id": "sweep", "event_type": "SWEEP_RESOLVED", "actor_id": "guard",
             "position": [2, 0], "values": {"targets_hit": 1}},
            {"event_id": "hit", "event_type": "SHOT_HIT", "actor_id": "ranger",
             "target_id": "enemy", "position": [3, 0], "values": {"damage": 1}},
            {"event_id": "damage", "event_type": "UNIT_DAMAGED", "reason_code": "ATTACK",
             "target_id": "enemy", "position": [3, 0], "values": {"damage": 1, "hp": 0}},
        ]
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": events})
        mem.apply_events(state)
        mem.apply_events(state)
        self.assertEqual([event["type"] for event in mem.ledger.combat_events],
                         ["SWEEP_RESOLVED", "SHOT_HIT", "UNIT_DAMAGED"])
        self.assertEqual(list(mem.ledger.combat_deaths), [{"target_id": "enemy", "tick": 1}])

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

    def test_external_over_cap_core_full_recovery_remains_live(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        workers = [
            {"kind": "UNIT", "id": f"worker-{index}", "controlled": True,
             "position": [0, 0] if index == 0 else [index, 0],
             "unit_type": "WORKER", "cargo": 1 if index == 0 else 0}
            for index in range(MAX_ECONOMY_WORKERS + 1)
        ]
        full = {"event_id": "full-over-cap", "event_type": "DEPOSIT_FAILED",
                "reason_code": "CORE_RESOURCE_FULL", "actor_id": "worker-0", "position": [0, 0]}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 50,
            "population": 10, "objects": [core, *workers], "events": [full]})
        plan = economy_plan(state, mem)
        self.assertEqual(plan.policy_state, "CORE_FULL_EXTERNAL_CAP_RECOVERY")
        self.assertEqual(plan.core_action, {"type": "SPAWN", "unit_type": "WORKER"})
        self.assertEqual(plan.unit_actions["worker-0"], {"type": "MOVE", "direction": "UP"})

    def test_external_recovery_population_ceiling_holds_without_spawn(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        workers = [
            {"kind": "UNIT", "id": f"worker-{index}", "controlled": True,
             "position": [0, 0] if index == 0 else [index, 0],
             "unit_type": "WORKER", "cargo": 1 if index == 0 else 0}
            for index in range(MAX_EXTERNAL_RECOVERY_POPULATION - 1)
        ]
        full = {"event_id": "full-at-ceiling", "event_type": "DEPOSIT_FAILED",
                "reason_code": "CORE_RESOURCE_FULL", "actor_id": "worker-0", "position": [0, 0]}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 95,
            "population": MAX_EXTERNAL_RECOVERY_POPULATION, "objects": [core, *workers], "events": [full]})
        plan = economy_plan(state, mem)
        self.assertEqual(plan.policy_state, "CORE_FULL_EXTERNAL_CAP_HOLD")
        self.assertIsNone(plan.core_action)
        self.assertEqual(plan.unit_actions["worker-0"], {"type": "WAIT"})

    def test_external_recovery_below_population_ceiling_can_spawn(self):
        mem = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        workers = [
            {"kind": "UNIT", "id": f"worker-{index}", "controlled": True,
             "position": [0, 0] if index == 0 else [index, 0],
             "unit_type": "WORKER", "cargo": 1 if index == 0 else 0}
            for index in range(MAX_EXTERNAL_RECOVERY_POPULATION - 2)
        ]
        full = {"event_id": "full-below-ceiling", "event_type": "DEPOSIT_FAILED",
                "reason_code": "CORE_RESOURCE_FULL", "actor_id": "worker-0", "position": [0, 0]}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 90,
            "population": MAX_EXTERNAL_RECOVERY_POPULATION - 1,
            "objects": [core, *workers], "events": [full]})
        plan = economy_plan(state, mem)
        self.assertEqual(plan.policy_state, "CORE_FULL_EXTERNAL_CAP_RECOVERY")
        self.assertEqual(plan.core_action, {"type": "SPAWN", "unit_type": "WORKER"})

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

    def test_curl_argv_does_not_contain_authentication_material(self):
        token, cookie, csrf = "top-secret-token", "session=top-secret-cookie", "top-secret-csrf"
        with patch("arena_agent.__main__.subprocess.run") as mocked:
            mocked.return_value = type("Completed", (), {
                "returncode": 0, "stdout": "202", "stderr": ""
            })()
            with patch("builtins.open", mock_open(read_data='{}')):
                result = asyncio.run(post_plan(token, 9, {"unit_actions": {}}, False, cookie, csrf))
        self.assertEqual(result["status"], 202)
        argv = mocked.call_args.args[0]
        self.assertIn("--config", argv)
        self.assertNotIn(token, argv)
        self.assertNotIn(cookie, argv)
        self.assertNotIn(csrf, argv)
        self.assertNotIn("Authorization:", " ".join(argv))

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
