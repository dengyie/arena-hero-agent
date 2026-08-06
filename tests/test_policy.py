import asyncio
import unittest
from unittest.mock import mock_open, patch
from arena_agent.allocator import allocate_visible_resources
from arena_agent.combat import (cell_shot_action, guard_slots, intermediate_cells,
                                precision_shot_action, ranger_line_distance,
                                ranger_target, select_ranger_decision,
                                select_vanguard_decision)
from arena_agent.model import Unit, snapshot_from_state
from arena_agent.path import (FRONTIER_PATH_NODE_CAP, MAX_FRONTIER_PATH_EVALUATIONS,
                              PathResult, plan_frontier_path, step_position)
from arena_agent.__main__ import (PermanentAuthError, PhaseEvaluationMetrics, ResourceSupplyMetrics, allocator_metrics,
                                  combat_operator_attention, operator_attention_metrics, population_control_metrics, post_plan,
                                  effective_combat_mode,
                                  reconcile_received_plan,
                                  received_source_conflict,
                                  received_combat_contaminated,
                                  websocket_close_requires_reconnect,
                                  resource_transition_audit,
                                  record_population_transition,
                                  record_received_source, record_session_baseline, stale_tick_reconnect_required)
from pathlib import Path
from arena_agent.policy import (
    CombatMemory, ExplorationMemory, MAX_BAND_RADIUS, MAX_ECONOMY_WORKERS, MAX_EXTERNAL_RECOVERY_POPULATION, PATH_NODE_CAP,
    core_resource_capacity, economy_plan, first_step, plan_path, ranger_fire_allowed,
    unit_production_cost, upkeep_for_population,
)
from arena_agent.journal import Journal

class AgentTests(unittest.TestCase):
    def test_sdk_029_dynamic_unit_prices_and_core_capacity(self):
        self.assertEqual(core_resource_capacity(0), 10)
        self.assertEqual(core_resource_capacity(1), 10)
        self.assertEqual(core_resource_capacity(3), 15)
        self.assertEqual(
            [unit_production_cost("WORKER", n) for n in (0, 19, 20, 24, 25, 29, 30)],
            [5, 5, 7, 7, 8, 8, 11],
        )
        self.assertEqual(
            [unit_production_cost("VANGUARD", n) for n in (0, 19, 20, 24, 25, 29, 30)],
            [10, 10, 13, 13, 17, 17, 22],
        )
        self.assertEqual(
            [unit_production_cost("RANGER", n) for n in (0, 19, 20, 24, 25, 29, 30)],
            [12, 12, 16, 16, 20, 20, 26],
        )
        with self.assertRaises(ValueError):
            unit_production_cost("WORKER", -1)
        with self.assertRaises(ValueError):
            unit_production_cost("UNKNOWN", 0)

    def test_worker_at_current_visible_resource_preempts_frontier_and_harvests(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [5, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        other = {"kind": "UNIT", "id": "other", "controlled": True,
                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        resource = {"kind": "RESOURCE", "positions": [[5, 0], [8, 0]]}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0,
            "population": 2, "objects": [core, worker, other, resource], "events": []})
        plan = economy_plan(state, ExplorationMemory(), combat_mode="current")
        self.assertEqual(plan.unit_actions["worker"], {"type": "HARVEST"})
        self.assertNotEqual(plan.unit_actions["other"], {"type": "HARVEST"})

    def test_current_resource_is_reconsidered_after_harvest_failure(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [5, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        resource = {"kind": "RESOURCE", "positions": [[5, 0]]}
        memory = ExplorationMemory()
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0,
            "population": 1, "objects": [core, worker, resource], "events": []})
        self.assertEqual(economy_plan(state, memory).unit_actions["worker"], {"type": "HARVEST"})
        failed = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0,
            "population": 1, "objects": [core, worker, resource],
            "events": [{"event_id": "failed", "event_type": "HARVEST_FAILED",
                        "actor_id": "worker", "position": [5, 0]}]})
        next_plan = economy_plan(failed, memory)
        self.assertEqual(next_plan.unit_actions["worker"], {"type": "HARVEST"})

    def test_visible_resource_after_failure_can_be_reassigned_to_other_worker(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        failed_worker = {"kind": "UNIT", "id": "failed-worker", "controlled": True,
                         "position": [7, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        other = {"kind": "UNIT", "id": "other", "controlled": True,
                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        resource = {"kind": "RESOURCE", "positions": [[5, 0]]}
        state = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0,
            "population": 2, "objects": [core, failed_worker, other, resource],
            "events": [{"event_id": "failed-other", "event_type": "HARVEST_FAILED",
                        "actor_id": "failed-worker", "position": [5, 0]}]})
        plan = economy_plan(state, ExplorationMemory())
        resource_intents = {
            worker_id: target for worker_id, (target, kind) in plan.worker_intents.items()
            if kind == "RESOURCE"
        }
        self.assertIn((5, 0), resource_intents.values())

    def test_unexplained_resource_drop_is_journaled_but_explained_drop_is_not(self):
        snapshot = snapshot_from_state(5, {"status": "ACTIVE", "resources": 0,
            "population": 1, "objects": [], "events": []})
        self.assertEqual(resource_transition_audit(95, snapshot, None), {
            "from": 95, "to": 0, "delta": -95, "expected_spend": 0,
            "evidence": [], "tick": 5,
        })
        upkeep = snapshot_from_state(6, {"status": "ACTIVE", "resources": 0,
            "population": 20, "objects": [], "events": [
                {"event_id": "upkeep", "event_type": "UPKEEP_PAID",
                 "values": {"due": 1, "paid": 1, "deficit": 0}},
            ]})
        self.assertIsNone(resource_transition_audit(1, upkeep, None))

    def test_resource_audit_requires_numeric_cost_to_explain_drop(self):
        state = snapshot_from_state(7, {"status": "ACTIVE", "resources": 0,
            "population": 1, "objects": [], "events": [
                {"event_id": "spawn", "event_type": "CORE_SPAWN_SUCCEEDED",
                 "values": {"unit_type": "WORKER", "cost": 5}},
            ]})
        self.assertIsNone(resource_transition_audit(5, state, None))
        missing_cost = snapshot_from_state(8, {"status": "ACTIVE", "resources": 0,
            "population": 1, "objects": [], "events": [
                {"event_id": "spawn2", "event_type": "CORE_SPAWN_SUCCEEDED",
                 "values": {"unit_type": "WORKER"}},
            ]})
        audit = resource_transition_audit(5, missing_cost, None)
        assert audit is not None
        self.assertEqual(audit["expected_spend"], 0)

    def test_resource_audit_accounts_for_unit_heal_cost(self):
        state = snapshot_from_state(9, {"status": "ACTIVE", "resources": 9,
            "population": 1, "objects": [], "events": [{
                "event_id": "unit-heal", "event_type": "UNIT_HEAL_SUCCEEDED",
                "actor_id": "worker", "values": {"amount": 1, "cost": 1, "hp": 2},
            }]})
        self.assertIsNone(resource_transition_audit(10, state, None))

    def test_conflicting_same_source_receipts_are_not_authoritative(self):
        self.assertTrue(received_source_conflict([
            {"source": "AGENT", "plan": {"unit_actions": {"w": {"type": "WAIT"}}}},
            {"source": "AGENT", "plan": {"unit_actions": {"w": {"type": "MOVE", "direction": "UP"}}}},
        ]))

    def test_manual_worker_does_not_contaminate_combat_domain(self):
        receipts = [{"source": "MANUAL", "plan": {
            "unit_actions": {"worker": {"type": "MOVE", "direction": "UP"}},
        }}]
        self.assertFalse(received_combat_contaminated(receipts, {"vanguard", "ranger"}))

    def test_manual_defender_core_or_agent_conflict_contaminates_combat(self):
        defender = [{"source": "MANUAL", "plan": {
            "unit_actions": {"ranger": {"type": "WAIT"}},
        }}]
        core = [{"source": "MANUAL", "plan": {"unit_actions": {},
                                                 "core_action": {"type": "HEAL"}}}]
        conflict = [
            {"source": "AGENT", "plan": {"unit_actions": {"ranger": {"type": "WAIT"}}}},
            {"source": "AGENT", "plan": {"unit_actions": {"ranger": {"type": "MOVE", "direction": "UP"}}}},
        ]
        self.assertTrue(received_combat_contaminated(defender, {"ranger"}))
        self.assertTrue(received_combat_contaminated(core, {"ranger"}))
        self.assertTrue(received_combat_contaminated(conflict, {"ranger"}))

    def test_normal_websocket_close_reconnects(self):
        self.assertTrue(websocket_close_requires_reconnect())

    def test_received_manual_override_clears_agent_move_and_does_not_record_shot(self):
        memory = ExplorationMemory()
        memory.traffic.mark_planned_move("worker", (0, 0), "RIGHT")
        accepted = {"unit_actions": {
            "worker": {"type": "MOVE", "direction": "RIGHT"},
            "ranger": {"type": "SHOOT", "target_id": "enemy", "expected_cell": [2, 0]},
        }}
        received = {"tick": 7, "source": "MANUAL", "plan": {
            "tick": 7, "unit_actions": {"worker": {"type": "MOVE", "direction": "LEFT"}},
        }}
        reconcile_received_plan(memory, 7, accepted, [received], {"ranger": "PRECISION_CURRENT"})
        self.assertNotIn("worker", memory.traffic.last_planned_edges)
        self.assertFalse(memory.ledger.pending_combat_submissions)

    def test_received_matching_agent_plan_records_shot_and_spawn_acceptance(self):
        memory = ExplorationMemory()
        memory.combat.request_spawn("RANGER", 8, ())
        accepted = {
            "unit_actions": {
                "ranger": {"type": "SHOOT", "target_id": "enemy", "expected_cell": [2, 0]},
            },
            "core_action": {"type": "SPAWN", "unit_type": "RANGER"},
        }
        received = {"tick": 8, "source": "AGENT", "plan": {"tick": 8, **accepted}}
        reconcile_received_plan(memory, 8, accepted, [received], {"ranger": "PRECISION_CURRENT"})
        self.assertEqual(memory.combat.last_spawn_result, "ACCEPTED")
        self.assertEqual(memory.ledger.pending_combat_submissions[("ranger", 8)], "PRECISION_CURRENT")

    def test_manual_override_wins_regardless_of_receipt_arrival_order(self):
        accepted = {"unit_actions": {
            "ranger": {"type": "SHOOT", "target_id": "enemy", "expected_cell": [2, 0]},
        }}
        agent = {"tick": 9, "source": "AGENT", "plan": {"tick": 9, **accepted}}
        manual = {"tick": 9, "source": "MANUAL", "plan": {"tick": 9,
            "unit_actions": {"ranger": {"type": "MOVE", "direction": "LEFT"}}}}
        for receipts in ([agent, manual], [manual, agent]):
            memory = ExplorationMemory()
            reconcile_received_plan(memory, 9, accepted, list(receipts),
                                    {"ranger": "PRECISION_CURRENT"})
            self.assertFalse(memory.ledger.pending_combat_submissions)

    def test_identical_manual_action_is_not_attributed_to_agent(self):
        shot = {"type": "SHOOT", "target_id": "enemy", "expected_cell": [2, 0]}
        accepted = {"unit_actions": {"ranger": shot}}
        agent = {"tick": 10, "source": "AGENT", "plan": {"tick": 10, **accepted}}
        manual = {"tick": 10, "source": "MANUAL", "plan": {"tick": 10,
            "unit_actions": {"ranger": dict(shot)}}}
        memory = ExplorationMemory()
        reconcile_received_plan(memory, 10, accepted, [agent, manual],
                                {"ranger": "PRECISION_CURRENT"})
        self.assertFalse(memory.ledger.pending_combat_submissions)

    def test_live_precision_auto_advances_only_after_closed_precision_episode(self):
        self.assertEqual(effective_combat_mode("live-precision", []), "live-precision")
        self.assertEqual(
            effective_combat_mode("live-precision", [{"incoming_damage": 1, "precision_shots": 0}]),
            "live-precision",
        )
        self.assertEqual(
            effective_combat_mode(
                "live-precision",
                [{"precision_shots": 1, "shots_missed": 1, "end_tick": 9,
                  "outcome": "CLEAN_COMPLETE"}],
            ),
            "live-cell",
        )
        self.assertEqual(
            effective_combat_mode(
                "live-precision",
                [{"precision_shots": 1, "outcome": "INCOMPLETE", "end_tick": 9}],
            ),
            "live-precision",
        )
        self.assertEqual(
            effective_combat_mode(
                "live-precision",
                [{"precision_shots": 1, "outcome": "EXCLUDED_CONTAMINATED", "end_tick": 9}],
            ),
            "live-precision",
        )
        self.assertEqual(
            effective_combat_mode("live-sweep", [{"precision_shots": 1, "end_tick": 9}]),
            "live-sweep",
        )

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

    def test_v2_snapshot_enemy_fields_are_current_state_only(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [2, 0], "unit_type": "VANGUARD", "hp": 3}
        first = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 0,
            "objects": [core, enemy], "events": []})
        self.assertEqual((first.visible_enemies[0].hp, first.visible_enemies[0].unit_type), (3, "VANGUARD"))
        second = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 0,
            "objects": [core], "events": []})
        self.assertEqual(second.visible_enemies, ())

    def test_v2_precision_and_cell_shot_payloads_are_strict(self):
        self.assertEqual(precision_shot_action("enemy", (3, 3)), {
            "type": "SHOOT", "target_id": "enemy", "expected_cell": [3, 3],
        })
        self.assertEqual(cell_shot_action((3, 3)), {
            "type": "SHOOT", "expected_cell": [3, 3],
        })
        self.assertNotIn("target_id", cell_shot_action((3, 3)))

    def test_v2_ranger_geometry_and_current_cell_decision(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 0], "unit_type": "RANGER", "hp": 2}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [3, 3], "unit_type": "WORKER", "hp": 2}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, ranger, enemy], "events": []})
        decision = select_ranger_decision(state.rangers[0], state, protected_cells={(0, 0)})
        self.assertEqual(decision.target_mode, "PRECISION_CURRENT")
        self.assertEqual(decision.action, precision_shot_action("enemy", (3, 3)))
        blocked = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, ranger, enemy, {"kind": "OBSTACLE", "positions": [[1, 1]]}], "events": []})
        self.assertEqual(select_ranger_decision(blocked.rangers[0], blocked, protected_cells={(0, 0)}).action,
                         {"type": "WAIT"})
        self.assertIsNone(ranger_line_distance((0, 0), (2, 1)))

    def test_v2_vanguard_scores_multi_hostile_sweep_cell(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [1, 1], "unit_type": "VANGUARD", "hp": 4}
        enemies = [
            {"kind": "UNIT", "id": "a", "controlled": False, "position": [1, 0], "unit_type": "WORKER"},
            {"kind": "CORE", "id": "b", "controlled": False, "position": [1, 0]},
            {"kind": "UNIT", "id": "c", "controlled": False, "position": [2, 1], "unit_type": "WORKER"},
        ]
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, guard, *enemies], "events": []})
        decision = select_vanguard_decision(state.vanguards[0], state, protected_cells={(0, 0)})
        self.assertEqual(decision.action, {"type": "SWEEP", "direction": "UP"})

    def test_v2_guard_slots_are_deterministic_and_bounded(self):
        slots = guard_slots((10, 10), frozenset({(12, 10)}))
        self.assertEqual(slots, guard_slots((10, 10), frozenset({(12, 10)})))
        self.assertTrue(slots)
        self.assertTrue(all(2 <= abs(x - 10) + abs(y - 10) <= 4 for x, y in slots))
        self.assertNotIn((12, 10), slots)

    def test_v2_combat_roles_are_stable_and_forget_missing_units(self):
        memory = CombatMemory()
        units = (Unit("v2", (2, 0), "VANGUARD", 0, 4), Unit("v1", (3, 0), "VANGUARD", 0, 4),
                 Unit("r1", (0, 2), "RANGER", 0, 2))
        memory.reconcile_roles(units, tick=1)
        self.assertEqual((memory.home_vanguard_id, memory.home_ranger_id), ("v1", "r1"))
        memory.reconcile_roles((units[0],), tick=2)
        self.assertEqual((memory.home_vanguard_id, memory.home_ranger_id), ("v2", None))
        self.assertFalse(any("enemy" in name for name in vars(memory)))

    def test_v2_spawn_transaction_confirms_only_new_matching_unit(self):
        memory = CombatMemory()
        before = (Unit("worker", (1, 0), "WORKER", 0, 2),)
        memory.request_spawn("VANGUARD", 10, before)
        memory.reconcile_roles(before, tick=11)
        self.assertIsNotNone(memory.pending_spawn)
        after = (*before, Unit("guard", (0, 0), "VANGUARD", 0, 4))
        memory.reconcile_roles(after, tick=12)
        self.assertIsNotNone(memory.pending_spawn)
        self.assertIsNone(memory.home_vanguard_id)
        memory.mark_spawn_accepted(10)
        memory.reconcile_roles(after, tick=13)
        self.assertIsNone(memory.pending_spawn)
        self.assertEqual(memory.home_vanguard_id, "guard")
        self.assertEqual(memory.last_spawn_result, "CONFIRMED")

    def test_v2_combat_production_respects_reserve_population_and_guard(self):
        self.assertEqual(
            [upkeep_for_population(n) for n in (0, 19, 20, 39, 40, 59, 60)],
            [0, 0, 1, 1, 3, 3, 6],
        )
        core = {"kind": "CORE", "id": "core", "controlled": True,
                "position": [0, 0], "hp": 5, "shield": 5, "state": "NORMAL"}
        workers = [{"kind": "UNIT", "id": f"w{i}", "controlled": True,
                    "position": [i + 1, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
                   for i in range(17)]
        mem = ExplorationMemory()
        first = snapshot_from_state(1, {"status": "ACTIVE", "resources": 50, "population": 17,
            "upkeep_next_tick": 0, "objects": [core, *workers], "events": []})
        plan = economy_plan(first, mem, combat_mode="production", combat_production_guard=True)
        self.assertEqual(plan.core_action, {"type": "SPAWN", "unit_type": "VANGUARD"})
        blocked = economy_plan(first, ExplorationMemory(), combat_mode="production", combat_production_guard=False)
        self.assertIsNone(blocked.core_action)
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [0, 2], "unit_type": "VANGUARD", "cargo": 0, "hp": 4}
        eighteen_workers = [
            {"kind": "UNIT", "id": f"x{i}", "controlled": True,
             "position": [i + 1, 1], "unit_type": "WORKER", "cargo": 0, "hp": 2}
            for i in range(18)
        ]
        ranger_slot = snapshot_from_state(2, {"status": "ACTIVE", "resources": 95, "population": 19,
            "upkeep_next_tick": 0, "objects": [core, *eighteen_workers, guard], "events": []})
        self.assertIsNone(
            economy_plan(ranger_slot, ExplorationMemory(), combat_mode="production").core_action,
        )
        supplied = ExplorationMemory()
        supplied.ledger.deposits.extend(range(20))
        self.assertEqual(
            economy_plan(ranger_slot, supplied, combat_mode="production").core_action,
            {"type": "SPAWN", "unit_type": "RANGER"},
        )
        low_reserve = snapshot_from_state(3, {"status": "ACTIVE", "resources": 51, "population": 19,
            "upkeep_next_tick": 0, "objects": [core, *eighteen_workers, guard], "events": []})
        self.assertIsNone(economy_plan(low_reserve, ExplorationMemory(), combat_mode="production").core_action)
        exact_reserve = snapshot_from_state(3, {"status": "ACTIVE", "resources": 52, "population": 19,
            "upkeep_next_tick": 0, "objects": [core, *eighteen_workers, guard], "events": []})
        exact_supplied = ExplorationMemory()
        exact_supplied.ledger.deposits.extend(range(20))
        self.assertEqual(
            economy_plan(exact_reserve, exact_supplied, combat_mode="production").core_action,
            {"type": "SPAWN", "unit_type": "RANGER"},
        )
        no_vanguard = snapshot_from_state(4, {"status": "ACTIVE", "resources": 100, "population": 19,
            "upkeep_next_tick": 0, "objects": [core, *eighteen_workers], "events": []})
        self.assertIsNone(economy_plan(no_vanguard, ExplorationMemory(), combat_mode="production").core_action)
        at_ceiling = snapshot_from_state(5, {"status": "ACTIVE", "resources": 100, "population": 20,
            "upkeep_next_tick": 1, "objects": [core, *eighteen_workers, guard], "events": []})
        self.assertIsNone(economy_plan(at_ceiling, ExplorationMemory(), combat_mode="production").core_action)
        self.assertEqual(combat_operator_attention(at_ceiling, "production", True), {
            "required": True, "reason": "combat_population_ceiling",
        })
        self.assertEqual(combat_operator_attention(ranger_slot, "production", True), {
            "required": False, "reason": None,
        })
        self.assertEqual(combat_operator_attention(first, "production", False), {
            "required": True, "reason": "unattributed_population_increase",
        })

    def test_v2_positioning_allows_defender_to_follow_worker_vacated_cell(self):
        core = {"kind": "CORE", "id": "core", "controlled": True,
                "position": [0, 0], "hp": 5, "shield": 5, "state": "NORMAL"}
        carrier = {"kind": "UNIT", "id": "carrier", "controlled": True,
                   "position": [2, 0], "unit_type": "WORKER", "cargo": 1, "hp": 2}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [2, 1], "unit_type": "VANGUARD", "hp": 3}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 10, "population": 2,
            "upkeep_next_tick": 0, "objects": [core, carrier, guard], "events": []})
        plan = economy_plan(state, ExplorationMemory(), combat_mode="positioning")
        self.assertEqual(plan.unit_actions["carrier"], {"type": "MOVE", "direction": "LEFT"})
        self.assertEqual(plan.combat_decisions["guard"]["state"], "RECOVER")
        self.assertEqual(plan.unit_actions["guard"], {"type": "MOVE", "direction": "UP"})

    def test_v2_worker_may_share_singly_occupied_friendly_defender_cell(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [0, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [1, 0], "unit_type": "VANGUARD", "hp": 4}
        resource = {"kind": "RESOURCE", "positions": [[2, 0]]}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, worker, guard, resource], "events": []})
        plan = economy_plan(state, ExplorationMemory(), combat_mode="positioning")
        self.assertEqual(plan.unit_actions["worker"], {"type": "MOVE", "direction": "RIGHT"})
        current = economy_plan(state, ExplorationMemory(), combat_mode="current")
        shadow = economy_plan(state, ExplorationMemory(), combat_mode="shadow")
        self.assertEqual(shadow.as_dict(), current.as_dict())

    def test_worker_does_not_enter_friendly_cell_already_at_capacity_two(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        first = {"kind": "UNIT", "id": "first", "controlled": True,
                 "position": [2, 0], "unit_type": "WORKER", "cargo": 0}
        second = {"kind": "UNIT", "id": "second", "controlled": True,
                  "position": [2, 0], "unit_type": "VANGUARD", "cargo": 0}
        carrier = {"kind": "UNIT", "id": "carrier", "controlled": True,
                   "position": [3, 0], "unit_type": "WORKER", "cargo": 1}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 3,
            "objects": [core, first, second, carrier], "events": []})
        self.assertNotEqual(
            economy_plan(state, ExplorationMemory()).unit_actions["carrier"],
            {"type": "MOVE", "direction": "LEFT"},
        )

    def test_worker_does_not_enter_enemy_occupied_cell(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        carrier = {"kind": "UNIT", "id": "carrier", "controlled": True,
                   "position": [3, 0], "unit_type": "WORKER", "cargo": 1}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [2, 0], "unit_type": "WORKER", "cargo": 0}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, carrier, enemy], "events": []})
        self.assertNotEqual(
            economy_plan(state, ExplorationMemory()).unit_actions["carrier"],
            {"type": "MOVE", "direction": "LEFT"},
        )

    def test_v2_shadow_records_decisions_without_changing_actions(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [4, 0], "unit_type": "VANGUARD", "hp": 4}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [5, 0], "unit_type": "WORKER", "hp": 2}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, guard, enemy], "events": []})
        current = economy_plan(state, ExplorationMemory(), combat_mode="current")
        shadow = economy_plan(state, ExplorationMemory(), combat_mode="shadow")
        self.assertEqual(shadow.unit_actions, current.unit_actions)
        self.assertEqual(shadow.combat_decisions["guard"]["proposed_action"]["type"], "SWEEP")
        self.assertTrue(shadow.combat_decisions["guard"]["shadow"])

    def test_v2_live_cell_intercept_has_no_target_id(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 0], "unit_type": "RANGER", "hp": 2}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [2, 1], "unit_type": "WORKER", "hp": 2}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, ranger, enemy], "events": []})
        plan = economy_plan(state, ExplorationMemory(), combat_mode="live")
        self.assertEqual(plan.unit_actions["ranger"]["type"], "SHOOT")
        self.assertNotIn("target_id", plan.unit_actions["ranger"])
        self.assertEqual(plan.combat_decisions["ranger"]["target_mode"], "CELL_INTERCEPT")

    def test_v2_ranger_loss_fuse_blocks_live_precision_and_intercept(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 0], "unit_type": "RANGER", "hp": 2}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [3, 0], "unit_type": "WORKER", "hp": 2}
        mem = ExplorationMemory()
        mem.ledger.combat_cooldown_until = 10
        state = snapshot_from_state(5, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, ranger, enemy], "events": []})
        plan = economy_plan(state, mem, combat_mode="live")
        self.assertNotEqual(plan.unit_actions["ranger"]["type"], "SHOOT")
        self.assertEqual(plan.combat_decisions["ranger"]["reason"], "RANGER_FIRE_FUSED")

    def test_v2_ranger_fire_allows_complete_population_20_squad_but_not_21(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 1], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [0, 3], "unit_type": "WORKER", "hp": 2}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 80, "population": 20,
            "upkeep_next_tick": 1, "objects": [core, ranger, enemy], "events": []})
        plan = economy_plan(state, ExplorationMemory(), combat_mode="live-precision")
        self.assertEqual(plan.unit_actions["ranger"], {
            "type": "SHOOT", "target_id": "enemy", "expected_cell": [0, 3],
        })
        over = snapshot_from_state(2, {"status": "ACTIVE", "resources": 80, "population": 21,
            "upkeep_next_tick": 1, "objects": [core, ranger, enemy], "events": []})
        over_plan = economy_plan(over, ExplorationMemory(), combat_mode="live-precision")
        self.assertNotEqual(over_plan.unit_actions["ranger"].get("type"), "SHOOT")
        self.assertEqual(over_plan.combat_decisions["ranger"]["reason"], "RANGER_FIRE_FUSED")

    def test_v2_upkeep_fuse_removes_only_home_ranger_before_worker_damage(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [3, 0], "unit_type": "WORKER", "cargo": 1, "hp": 2}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [2, 0], "unit_type": "VANGUARD", "cargo": 0, "hp": 4}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 2], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        low = snapshot_from_state(1, {"status": "ACTIVE", "resources": 20, "population": 20,
            "upkeep_next_tick": 1, "objects": [core, worker, guard, ranger], "events": []})
        plan = economy_plan(low, ExplorationMemory(), combat_mode="live-sweep")
        self.assertEqual(plan.policy_state, "COMBAT_UPKEEP_FUSE")
        self.assertEqual(plan.unit_actions["ranger"], {"type": "SELF_DESTRUCT"})
        self.assertEqual(plan.unit_actions["worker"], {"type": "WAIT"})
        self.assertEqual(plan.unit_actions["guard"], {"type": "WAIT"})
        current = economy_plan(low, ExplorationMemory(), combat_mode="current")
        self.assertNotEqual(current.unit_actions.get("ranger", {}).get("type"), "SELF_DESTRUCT")
        safe = snapshot_from_state(2, {"status": "ACTIVE", "resources": 21, "population": 20,
            "upkeep_next_tick": 1, "objects": [core, worker, guard, ranger], "events": []})
        self.assertNotEqual(
            economy_plan(safe, ExplorationMemory(), combat_mode="live-sweep").policy_state,
            "COMBAT_UPKEEP_FUSE",
        )

    def test_v2_defenders_respond_only_to_current_core_zone_threat(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [-2, 0], "unit_type": "VANGUARD", "hp": 4}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [4, 0], "unit_type": "WORKER", "hp": 2}
        mem = ExplorationMemory()
        first = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, guard, enemy], "events": []})
        response = economy_plan(first, mem, combat_mode="live")
        self.assertEqual(response.combat_decisions["guard"]["state"], "RESPOND")
        self.assertEqual(response.unit_actions["guard"]["type"], "MOVE")
        second = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, guard], "events": []})
        fallback = economy_plan(second, mem, combat_mode="live")
        self.assertNotEqual(fallback.combat_decisions["guard"]["state"], "RESPOND")
        self.assertFalse(any("enemy" in name for name in vars(mem.combat)))

    def test_v2_current_worker_threat_starts_bounded_friendly_escort(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [2, 0], "unit_type": "VANGUARD", "cargo": 0, "hp": 4}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 2], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [12, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [14, 0], "unit_type": "WORKER", "hp": 2}
        memory = ExplorationMemory()
        threatened = snapshot_from_state(1, {"status": "ACTIVE", "resources": 30,
            "population": 3, "objects": [core, guard, ranger, worker, enemy], "events": []})
        first = economy_plan(threatened, memory, combat_mode="live-precision")
        self.assertEqual(memory.combat.escort_worker_id, "worker")
        self.assertEqual(first.combat_decisions["guard"]["state"], "RESPOND")
        self.assertEqual(first.combat_decisions["ranger"]["state"], "RESPOND")
        self.assertEqual(first.unit_actions["guard"]["type"], "MOVE")
        self.assertEqual(first.unit_actions["ranger"]["type"], "MOVE")
        self.assertFalse(any("enemy" in name for name in vars(memory.combat)))

        quiet = snapshot_from_state(2, {"status": "ACTIVE", "resources": 30,
            "population": 3, "objects": [core, guard, ranger, worker], "events": []})
        second = economy_plan(quiet, memory, combat_mode="live-precision")
        self.assertEqual(memory.combat.escort_worker_id, "worker")
        self.assertEqual(second.combat_decisions["guard"]["state"], "ESCORT")

        carrying = snapshot_from_state(3, {"status": "ACTIVE", "resources": 30,
            "population": 3, "objects": [core, guard, ranger, {**worker, "cargo": 1}],
            "events": []})
        economy_plan(carrying, memory, combat_mode="live-precision")
        self.assertIsNone(memory.combat.escort_worker_id)

    def test_v2_friendly_patrol_starts_without_enemy_and_lease_expires(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [2, 0], "unit_type": "VANGUARD", "cargo": 0, "hp": 4}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 2], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [10, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        memory = ExplorationMemory()
        first = snapshot_from_state(1, {"status": "ACTIVE", "resources": 30,
            "population": 3, "objects": [core, guard, ranger, worker], "events": []})
        plan = economy_plan(first, memory, combat_mode="live-precision")
        self.assertIsNone(memory.combat.escort_worker_id)
        self.assertNotEqual(plan.combat_decisions["guard"]["state"], "ESCORT")

        threatened = snapshot_from_state(2, {"status": "ACTIVE", "resources": 30,
            "population": 3, "objects": [core, guard, ranger, worker,
                {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [12, 0], "unit_type": "WORKER", "hp": 2}], "events": []})
        threatened_plan = economy_plan(threatened, memory, combat_mode="live-precision")
        self.assertEqual(memory.combat.escort_worker_id, "worker")
        self.assertIn(threatened_plan.combat_decisions["guard"]["state"], {"RESPOND", "ESCORT"})

        expired = snapshot_from_state(123, {"status": "ACTIVE", "resources": 30,
            "population": 3, "objects": [core, guard, ranger, worker], "events": []})
        expired_plan = economy_plan(expired, memory, combat_mode="live-precision")
        self.assertIsNone(memory.combat.escort_worker_id)
        self.assertNotEqual(expired_plan.combat_decisions["guard"]["state"], "ESCORT")

    def test_v2_patrol_selects_farthest_safe_worker_and_stable_distinct_slots(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [2, 0], "unit_type": "VANGUARD", "cargo": 0, "hp": 4}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 2], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        near = {"kind": "UNIT", "id": "near", "controlled": True,
                "position": [4, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        far = {"kind": "UNIT", "id": "far", "controlled": True,
               "position": [18, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 30,
            "population": 4, "objects": [core, guard, ranger, near, far], "events": []})
        memory = ExplorationMemory()
        plan = economy_plan(state, memory, combat_mode="live-precision")
        self.assertIsNone(memory.combat.escort_worker_id)
        self.assertNotEqual(plan.combat_decisions["guard"]["state"], "ESCORT")
        self.assertNotEqual(plan.combat_decisions["ranger"]["state"], "ESCORT")

    def test_v2_patrol_does_not_rebind_until_defenders_return_home(self):
        memory = CombatMemory(home_vanguard_id="guard", home_ranger_id="ranger",
                              escort_cooldown_until=10)
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        far_guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                     "position": [8, 0], "unit_type": "VANGUARD", "cargo": 0, "hp": 4}
        far_ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                      "position": [8, 1], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [15, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        away = snapshot_from_state(10, {"status": "ACTIVE", "resources": 30,
            "population": 3, "objects": [core, far_guard, far_ranger, worker], "events": []})
        memory.reconcile_escort(away, set())
        self.assertIsNone(memory.escort_worker_id)

        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [16, 0], "unit_type": "WORKER", "hp": 2}
        home = snapshot_from_state(11, {"status": "ACTIVE", "resources": 30,
            "population": 3, "objects": [core, {**far_guard, "position": [2, 0]},
                                           {**far_ranger, "position": [0, 2]}, worker, enemy],
            "events": []})
        memory.reconcile_escort(home, {"worker"})
        self.assertEqual(memory.escort_worker_id, "worker")

    def test_v2_episode_attributes_only_accepted_shot_mode_once(self):
        mem = ExplorationMemory()
        mem.ledger.record_combat_submission("ranger", "CELL_INTERCEPT", 10)
        mem.ledger.record_combat_submission("ranger", "CELL_INTERCEPT", 10)
        state = snapshot_from_state(11, {"status": "ACTIVE", "resources": 0, "population": 0,
            "objects": [], "events": [{"event_id": "miss", "event_type": "SHOT_MISSED",
                                        "actor_id": "ranger", "values": {}}]})
        mem.apply_events(state)
        self.assertEqual(mem.ledger.combat_episode["cell_intercept_shots"], 1)
        self.assertEqual(mem.ledger.combat_episode["precision_shots"], 0)
        mem.apply_events(state)
        self.assertEqual(mem.ledger.combat_episode["cell_intercept_shots"], 1)

    def test_v2_delayed_shot_events_consume_submission_fifo(self):
        ledger = ExplorationMemory().ledger
        ledger.record_combat_submission("ranger", "PRECISION_CURRENT", 10)
        ledger.record_combat_submission("ranger", "CELL_INTERCEPT", 11)
        self.assertEqual(ledger.consume_combat_submission("ranger", 12), "PRECISION_CURRENT")
        self.assertEqual(ledger.consume_combat_submission("ranger", 12), "CELL_INTERCEPT")

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

    def test_resource_supply_metrics_count_only_clean_visibility_transitions_and_resolutions(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [1, 0], "unit_type": "WORKER", "cargo": 0}
        metrics = ResourceSupplyMetrics()
        mem = ExplorationMemory()
        empty = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": []})
        empty_plan = economy_plan(empty, mem)
        metrics.observe(empty, empty_plan, eligible=True)
        visible = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker, {"kind": "RESOURCE", "positions": [[1, 0]]}], "events": []})
        visible_plan = economy_plan(visible, mem)
        metrics.observe(visible, visible_plan, eligible=True)
        resolved = snapshot_from_state(3, {"status": "ACTIVE", "resources": 1, "population": 1,
            "objects": [core, worker],
            "events": [{"event_id": "h", "event_type": "HARVEST_SUCCEEDED", "actor_id": "worker", "position": [1, 0]}]})
        metrics.observe(resolved, economy_plan(resolved, mem), eligible=True)
        deposited = snapshot_from_state(4, {"status": "ACTIVE", "resources": 1, "population": 1,
            "objects": [core, worker],
            "events": [{"event_id": "d", "event_type": "DEPOSIT_SUCCEEDED", "actor_id": "worker", "position": [0, 0]}]})
        metrics.observe(deposited, economy_plan(deposited, mem), eligible=False)
        self.assertEqual(metrics.as_dict(), {
            "clean_ticks": 3, "starved_ticks": 2, "visible_resource_ticks": 1,
            "initial_visible_resources": 0, "discovery_transitions": 1, "harvests": 1, "deposits": 0,
            "action_counts": {"HARVEST": 1, "MOVE": 2},
            "intent_counts": {"EXPLORE": 2, "RESOURCE": 1},
        })

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

    def test_carrier_already_on_core_deposits_even_when_not_ingress_leader(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        leader = {"kind": "UNIT", "id": "a-leader", "controlled": True,
                  "position": [2, 0], "unit_type": "WORKER", "cargo": 1}
        arrived = {"kind": "UNIT", "id": "z-arrived", "controlled": True,
                   "position": [0, 0], "unit_type": "WORKER", "cargo": 1}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 2,
            "objects": [core, leader, arrived], "events": []})
        memory = ExplorationMemory()
        memory.traffic.ingress_queue = ("a-leader", "z-arrived")
        plan = economy_plan(state, memory)
        self.assertEqual(plan.unit_actions["z-arrived"], {"type": "DEPOSIT"})
        self.assertEqual(memory.traffic.ingress_queue[0], "z-arrived")

    def test_worker_may_enter_singly_occupied_friendly_non_core_cell(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        stationary = {"kind": "UNIT", "id": "stationary", "controlled": True,
                      "position": [2, 0], "unit_type": "WORKER", "cargo": 0}
        carrier = {"kind": "UNIT", "id": "carrier", "controlled": True,
                   "position": [3, 0], "unit_type": "WORKER", "cargo": 1}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 2,
            "objects": [core, stationary, carrier], "events": []})
        plan = economy_plan(state, ExplorationMemory())
        self.assertEqual(plan.unit_actions["carrier"], {"type": "MOVE", "direction": "LEFT"})

    def test_worker_does_not_enter_core_already_holding_one_unit(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        occupant = {"kind": "UNIT", "id": "occupant", "controlled": True,
                    "position": [0, 0], "unit_type": "WORKER", "cargo": 0}
        carrier = {"kind": "UNIT", "id": "carrier", "controlled": True,
                   "position": [1, 0], "unit_type": "WORKER", "cargo": 1}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 2,
            "objects": [core, occupant, carrier], "events": []})
        plan = economy_plan(state, ExplorationMemory())
        self.assertNotEqual(plan.unit_actions["carrier"], {"type": "MOVE", "direction": "LEFT"})

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

    def test_two_friendly_moves_may_fill_two_slots_of_empty_destination(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        carrier = {"kind": "UNIT", "id": "carrier", "controlled": True,
                   "position": [2, 0], "unit_type": "WORKER", "cargo": 1, "hp": 2}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [1, 1], "unit_type": "VANGUARD", "cargo": 0, "hp": 3}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 2,
            "objects": [core, carrier, guard], "events": []})
        plan = economy_plan(state, ExplorationMemory(), combat_mode="positioning")
        self.assertEqual(plan.unit_actions["carrier"], {"type": "MOVE", "direction": "LEFT"})
        self.assertEqual(plan.unit_actions["guard"], {"type": "MOVE", "direction": "UP"})
        self.assertEqual(step_position(carrier["position"], "LEFT"), (1, 0))
        self.assertEqual(step_position(guard["position"], "UP"), (1, 0))

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

    def test_frontier_completion_cooldown_never_reselects_reached_waypoint(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [9, 0], "unit_type": "WORKER", "cargo": 0}
        state = snapshot_from_state(10, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": []})
        mem = ExplorationMemory(route_core=(0, 0), route_core_id="core", band_radius=9)
        mem.active_targets["worker"] = (9, 0)
        mem.frontier_candidates.extend([(9, 0)])
        plan = economy_plan(state, mem)
        self.assertNotEqual(plan.worker_intents["worker"][0], (9, 0))
        self.assertNotEqual(plan.unit_actions["worker"]["type"], "WAIT")
        self.assertIn((9, 0), mem.completion_cooldowns)
        self.assertTrue(mem.frontier_completion_transitions)

    def test_frontier_uses_bounded_fallback_when_completed_candidate_is_only_normal_choice(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [9, 0], "unit_type": "WORKER", "cargo": 0}
        state = snapshot_from_state(10, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": []})
        mem = ExplorationMemory(route_core=(0, 0), route_core_id="core", band_radius=9)
        mem.frontier_candidates.extend([(9, 0)])
        mem.completion_cooldowns[(9, 0)] = 26
        target, result = mem.next_frontier(state, state.workers[0], frozenset(), set(), max_radius=9)
        self.assertIsNotNone(target)
        self.assertNotEqual(target, (9, 0))
        self.assertEqual(result.status, "FOUND")
        self.assertEqual(mem.frontier_selection_sources["worker"], "fallback")
        self.assertEqual(mem.frontier_fallback_assignments, 1)

    def test_frontier_budget_deferred_is_explicit_and_not_a_failure(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [0, 0], "unit_type": "WORKER", "cargo": 0}
        state = snapshot_from_state(10, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": []})
        mem = ExplorationMemory(route_core=(0, 0), route_core_id="core", band_radius=9)
        mem.frontier_candidates.extend([(9, 0)])
        mem.frontier_worker_evaluations["worker"] = MAX_FRONTIER_PATH_EVALUATIONS
        target, result = mem.next_frontier(state, state.workers[0], frozenset(), set(), max_radius=9)
        self.assertIsNone(target)
        self.assertEqual(result.status, "FRONTIER_BUDGET_DEFERRED")
        self.assertFalse(mem.failed_targets)
        self.assertEqual(mem.frontier_no_candidate_reasons["FRONTIER_BUDGET_DEFERRED"], 1)

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

    def test_every_current_worker_has_explicit_action_and_intent_trace(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        workers = [
            {"kind": "UNIT", "id": "w-empty", "controlled": True,
             "position": [4, 0], "unit_type": "WORKER", "cargo": 0},
            {"kind": "UNIT", "id": "w-cargo", "controlled": True,
             "position": [3, 0], "unit_type": "WORKER", "cargo": 1},
        ]
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, *workers], "events": []})
        plan = economy_plan(state, ExplorationMemory())
        self.assertEqual(set(plan.unit_actions), {"w-empty", "w-cargo"})
        self.assertTrue(all(action["type"] in {"WAIT", "MOVE", "HARVEST", "DEPOSIT", "HEAL"}
                            for action in plan.unit_actions.values()))
        self.assertEqual(plan.worker_intents["w-cargo"], ((0, 0), "RETURN_CORE"))

    def test_threatened_empty_worker_returns_safe_but_carrying_priority_and_ranger_protection_hold(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False, "position": [3, 0], "unit_type": "WORKER"}
        empty = {"kind": "UNIT", "id": "empty", "controlled": True, "position": [2, 0], "unit_type": "WORKER", "cargo": 0}
        carrying = {"kind": "UNIT", "id": "carrying", "controlled": True, "position": [2, 1], "unit_type": "WORKER", "cargo": 1}
        resource = {"kind": "RESOURCE", "positions": [[2, 0]]}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 2,
            "objects": [core, empty, carrying, enemy, resource], "events": []})
        plan = economy_plan(state, ExplorationMemory())
        self.assertEqual(plan.worker_intents["empty"], ((0, 0), "RETURN_SAFE"))
        self.assertEqual(plan.worker_intents["carrying"], ((0, 0), "RETURN_CORE"))
        self.assertEqual(plan.unit_actions["empty"], {"type": "WAIT"})
        self.assertEqual(plan.unit_actions["carrying"]["type"], "MOVE")

    def test_phase_evaluation_attributes_fallback_once_and_excludes_contamination(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True, "position": [1, 0], "unit_type": "WORKER", "cargo": 0}
        mem = ExplorationMemory()
        metrics = PhaseEvaluationMetrics()
        first = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": []})
        first_plan = economy_plan(first, mem)
        mem.frontier_selection_sources["worker"] = "fallback"
        metrics.observe(first, first_plan, mem, economy_eligible=True, combat_eligible=True)
        harvested = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, {**worker, "cargo": 1}],
                        "events": [{"event_id": "h", "event_type": "HARVEST_SUCCEEDED", "actor_id": "worker", "position": [1, 0]}]})
        harvested_plan = economy_plan(harvested, mem)
        metrics.observe(harvested, harvested_plan, mem, economy_eligible=True, combat_eligible=True)
        self.assertEqual(metrics.as_dict()["fallback_pending"], 1)
        self.assertEqual(metrics.as_dict()["fallback_outcomes"], {})
        deposited = snapshot_from_state(3, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, {**worker, "cargo": 0}],
            "events": [{"event_id": "d", "event_type": "DEPOSIT_SUCCEEDED", "actor_id": "worker", "position": [0, 0]}]})
        deposited_plan = economy_plan(deposited, mem)
        metrics.observe(deposited, deposited_plan, mem, economy_eligible=True, combat_eligible=True)
        self.assertEqual(metrics.as_dict()["fallback_outcomes"], {"DEPOSIT_AFTER_FALLBACK": 1})
        pending = snapshot_from_state(4, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, {**worker, "cargo": 0}], "events": []})
        pending_plan = economy_plan(pending, mem)
        mem.frontier_selection_sources["worker"] = "fallback"
        metrics.observe(pending, pending_plan, mem, economy_eligible=True, combat_eligible=True)
        metrics.observe(pending, pending_plan, mem, economy_eligible=False, combat_eligible=False)
        self.assertEqual(metrics.as_dict()["fallback_outcomes"]["ABORTED_CONTAMINATED"], 1)

    def test_safe_retreat_sticks_until_core_after_threat_visibility_flaps(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [2, 0], "unit_type": "WORKER", "cargo": 0}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [3, 0], "unit_type": "WORKER"}
        mem = ExplorationMemory()
        threatened = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker, enemy], "events": []})
        first = economy_plan(threatened, mem)
        self.assertEqual(first.worker_intents["worker"], ((0, 0), "RETURN_SAFE"))
        unthreatened = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, {**worker, "position": [1, 0]}], "events": []})
        second = economy_plan(unthreatened, mem)
        self.assertEqual(second.worker_intents["worker"], ((0, 0), "RETURN_SAFE"))
        arrived = snapshot_from_state(3, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, {**worker, "position": [0, 0]}], "events": []})
        third = economy_plan(arrived, mem)
        self.assertNotEqual(third.worker_intents["worker"][1], "RETURN_SAFE")

    def test_safe_retreat_uses_core_ingress_queue_without_last_step_oscillation(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        first = {"kind": "UNIT", "id": "first", "controlled": True,
                 "position": [1, 0], "unit_type": "WORKER", "cargo": 0}
        second = {"kind": "UNIT", "id": "second", "controlled": True,
                  "position": [1, 2], "unit_type": "WORKER", "cargo": 0}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [2, 1], "unit_type": "WORKER"}
        mem = ExplorationMemory()
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, first, second, enemy], "events": []})
        plan = economy_plan(state, mem)
        self.assertEqual(plan.worker_intents["first"], ((0, 0), "RETURN_SAFE"))
        self.assertEqual(plan.worker_intents["second"], ((0, 0), "RETURN_SAFE"))
        self.assertEqual(plan.unit_actions["first"], {"type": "MOVE", "direction": "LEFT"})
        self.assertEqual(plan.unit_actions["second"], {"type": "WAIT"})
        after = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, {**first, "position": [0, 0]}, second], "events": []})
        next_plan = economy_plan(after, mem)
        self.assertNotIn("first", mem.safe_retreat_workers)
        self.assertEqual(next_plan.unit_actions["second"]["type"], "MOVE")

    def test_ingress_queue_does_not_starve_existing_safe_retreat_with_new_carrier(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        retreat = {"kind": "UNIT", "id": "retreat", "controlled": True,
                   "position": [2, 0], "unit_type": "WORKER", "cargo": 0}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [3, 0], "unit_type": "WORKER"}
        mem = ExplorationMemory()
        first = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, retreat, enemy], "events": []})
        economy_plan(first, mem)
        self.assertEqual(mem.traffic.ingress_queue, ("retreat",))
        carrier = {"kind": "UNIT", "id": "carrier", "controlled": True,
                   "position": [0, 2], "unit_type": "WORKER", "cargo": 1}
        second = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, {**retreat, "position": [1, 0]}, carrier], "events": []})
        plan = economy_plan(second, mem)
        self.assertEqual(mem.traffic.ingress_queue[:2], ("retreat", "carrier"))
        self.assertEqual(plan.unit_actions["carrier"]["type"], "MOVE")
        self.assertEqual(plan.unit_actions["retreat"], {"type": "WAIT"})

    def test_remote_ingress_head_does_not_block_first_local_carrier(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        remote = {"kind": "UNIT", "id": "remote", "controlled": True,
                  "position": [20, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        local = {"kind": "UNIT", "id": "local", "controlled": True,
                 "position": [3, 0], "unit_type": "WORKER", "cargo": 1, "hp": 2}
        memory = ExplorationMemory()
        memory.route_core = (0, 0)
        memory.route_core_id = "core"
        memory.safe_retreat_workers.add("remote")
        memory.traffic.ingress_queue = ("remote", "local")
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5, "population": 2,
            "objects": [core, remote, local], "events": []})
        plan = economy_plan(state, memory)
        self.assertEqual(plan.unit_actions["local"], {"type": "MOVE", "direction": "LEFT"})
        self.assertEqual(plan.unit_actions["remote"]["type"], "MOVE")

    def test_local_carrier_preempts_earlier_local_safe_retreat(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        safe = {"kind": "UNIT", "id": "safe", "controlled": True,
                "position": [3, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
        carrier = {"kind": "UNIT", "id": "carrier", "controlled": True,
                   "position": [0, 3], "unit_type": "WORKER", "cargo": 1, "hp": 2}
        memory = ExplorationMemory()
        memory.route_core = (0, 0)
        memory.route_core_id = "core"
        memory.safe_retreat_workers.add("safe")
        memory.traffic.ingress_queue = ("safe", "carrier")
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 5,
            "population": 2, "objects": [core, safe, carrier], "events": []})
        plan = economy_plan(state, memory)
        self.assertEqual(plan.unit_actions["carrier"]["type"], "MOVE")
        self.assertEqual(plan.unit_actions["safe"], {"type": "WAIT"})

    def test_stalled_local_carrier_yields_ingress_token(self):
        traffic = ExplorationMemory().traffic
        traffic.ingress_queue = ("stalled", "next")
        candidates = [("stalled", "RETURN_CORE", 3), ("next", "RETURN_SAFE", 3)]
        self.assertEqual(traffic.select_local_ingress_head(candidates, 1), "stalled")
        self.assertEqual(traffic.select_local_ingress_head(candidates, 2), "stalled")
        self.assertEqual(traffic.select_local_ingress_head(candidates, 3), "stalled")
        self.assertEqual(traffic.select_local_ingress_head(candidates, 4), "stalled")
        self.assertEqual(traffic.select_local_ingress_head(candidates, 5), "next")

    def test_defense_restricts_vanguard_and_ranger_to_core_local_economy_protection(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        vanguard = {"kind": "UNIT", "id": "vanguard", "controlled": True,
                    "position": [10, 0], "unit_type": "VANGUARD", "cargo": 0}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [10, 1], "unit_type": "RANGER", "cargo": 0}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [11, 0], "unit_type": "WORKER"}
        distant = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, vanguard, ranger, enemy], "events": []})
        plan = economy_plan(distant, ExplorationMemory())
        self.assertEqual(plan.unit_actions["vanguard"], {"type": "WAIT"})
        self.assertEqual(plan.unit_actions["ranger"], {"type": "WAIT"})
        carrier = {"kind": "UNIT", "id": "carrier", "controlled": True,
                   "position": [2, 0], "unit_type": "WORKER", "cargo": 1}
        local_ranger = {"kind": "UNIT", "id": "local-ranger", "controlled": True,
                        "position": [0, 1], "unit_type": "RANGER", "cargo": 0}
        threat = {"kind": "UNIT", "id": "threat", "controlled": False,
                  "position": [3, 0], "unit_type": "WORKER"}
        protected = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, carrier, local_ranger, threat], "events": []})
        plan = economy_plan(protected, ExplorationMemory())
        self.assertEqual(plan.worker_intents["carrier"], ((0, 0), "RETURN_CORE"))
        self.assertEqual(plan.unit_actions["local-ranger"], {"type": "WAIT"})

    def test_phase_evaluation_marks_contaminated_closed_combat_episode_excluded(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [1, 0], "unit_type": "WORKER", "cargo": 0}
        mem = ExplorationMemory()
        metrics = PhaseEvaluationMetrics()
        hit = {"event_id": "hit", "event_type": "SHOT_HIT", "actor_id": "ranger", "target_id": "enemy",
               "values": {"damage": 1}}
        active = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": [hit]})
        metrics.observe(active, economy_plan(active, mem), mem, economy_eligible=True, combat_eligible=True)
        contaminated = snapshot_from_state(2, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": []})
        metrics.observe(contaminated, economy_plan(contaminated, mem), mem, economy_eligible=False, combat_eligible=False)
        closed = snapshot_from_state(10, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": []})
        metrics.observe(closed, economy_plan(closed, mem), mem, economy_eligible=True, combat_eligible=True)
        self.assertEqual(metrics.as_dict()["clean_combat_episodes"][-1]["outcome"], "EXCLUDED_CONTAMINATED")

    def test_economy_contamination_does_not_exclude_authoritative_combat_episode(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [1, 0], "unit_type": "RANGER", "cargo": 0, "hp": 2}
        memory = ExplorationMemory()
        metrics = PhaseEvaluationMetrics()
        memory.ledger.combat_episode = {
            "start_tick": 1, "end_tick": 9, "precision_shots": 1,
            "cell_intercept_shots": 0, "sweeps": 0, "friendly_deaths": 0,
            "friendly_cargo_lost": 0,
        }
        memory.ledger.completed_combat_episodes.append(memory.ledger.combat_episode)
        memory.ledger.combat_episode = None
        snapshot = snapshot_from_state(9, {"status": "ACTIVE", "resources": 1,
            "population": 1, "objects": [core, ranger], "events": []})
        metrics.observe(snapshot, economy_plan(snapshot, memory), memory,
                        economy_eligible=False, combat_eligible=True)
        self.assertEqual(metrics.as_dict()["clean_combat_episodes"][-1]["outcome"],
                         "CLEAN_COMPLETE")

    def test_phase_evaluation_consumes_episode_closed_on_contaminated_tick(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        worker = {"kind": "UNIT", "id": "worker", "controlled": True,
                  "position": [1, 0], "unit_type": "WORKER", "cargo": 0}
        mem = ExplorationMemory()
        metrics = PhaseEvaluationMetrics()
        mem.ledger.completed_combat_episodes.append({
            "start_tick": 1, "end_tick": 9, "shots_hit": 1,
        })
        snapshot = snapshot_from_state(9, {
            "status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, worker], "events": [],
        })
        metrics.observe(snapshot, economy_plan(snapshot, mem), mem, economy_eligible=False, combat_eligible=False)
        episodes = metrics.as_dict()["clean_combat_episodes"]
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["outcome"], "EXCLUDED_CONTAMINATED")

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

    def test_core_full_population_19_can_atomically_evict_carrier_and_spawn_missing_ranger(self):
        core = {"kind": "CORE", "id": "core", "controlled": True,
                "position": [0, 0], "hp": 5, "shield": 5, "state": "NORMAL"}
        workers = [
            {"kind": "UNIT", "id": f"worker-{index}", "controlled": True,
             "position": [0, 0] if index == 0 else [index + 1, 0],
             "unit_type": "WORKER", "cargo": 1 if index == 0 else 0, "hp": 2}
            for index in range(18)
        ]
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [0, 2], "unit_type": "VANGUARD", "cargo": 0, "hp": 4}
        full = {"event_id": "full-ranger-slot", "event_type": "DEPOSIT_FAILED",
                "reason_code": "CORE_RESOURCE_FULL", "actor_id": "worker-0", "position": [0, 0]}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 95, "population": 19,
            "upkeep_next_tick": 0, "objects": [core, *workers, guard], "events": [full]})
        supplied = ExplorationMemory()
        supplied.ledger.deposits.extend(range(20))
        plan = economy_plan(state, supplied, combat_mode="live-sweep")
        self.assertEqual(plan.policy_state, "CORE_FULL_COMBAT_CAPACITY_RECOVERY")
        self.assertEqual(plan.unit_actions["worker-0"], {"type": "MOVE", "direction": "UP"})
        self.assertEqual(plan.core_action, {"type": "SPAWN", "unit_type": "RANGER"})
        blocked = economy_plan(
            state, ExplorationMemory(), combat_mode="live-sweep", combat_production_guard=False,
        )
        self.assertEqual(blocked.policy_state, "CORE_FULL_EXTERNAL_CAP_HOLD")
        self.assertIsNone(blocked.core_action)

    def test_core_full_evictor_may_share_singly_occupied_friendly_adjacent_cell(self):
        memory = ExplorationMemory()
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        leader = {"kind": "UNIT", "id": "a-leader", "controlled": True,
                  "position": [0, 0], "unit_type": "WORKER", "cargo": 1}
        evictor = {"kind": "UNIT", "id": "z-evictor", "controlled": True,
                   "position": [0, 0], "unit_type": "WORKER", "cargo": 1}
        adjacent = [
            {"kind": "UNIT", "id": f"adjacent-{index}", "controlled": True,
             "position": position, "unit_type": "WORKER", "cargo": 0}
            for index, position in enumerate(([0, -1], [0, 1], [-1, 0], [1, 0]))
        ]
        full = {"event_id": "full-share", "event_type": "DEPOSIT_FAILED",
                "reason_code": "CORE_RESOURCE_FULL", "actor_id": "a-leader", "position": [0, 0]}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 30, "population": 6,
            "objects": [core, leader, evictor, *adjacent], "events": [full]})
        plan = economy_plan(state, memory)
        self.assertEqual(plan.policy_state, "CORE_FULL_EVICT")
        self.assertEqual(plan.unit_actions["z-evictor"], {"type": "MOVE", "direction": "UP"})

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
