import unittest

from arena_agent.combat import intermediate_cells, ranger_firing_cells, ranger_line_distance
from arena_agent.model import snapshot_from_state
from arena_agent.policy import ExplorationMemory, economy_plan


class CombatV2MatrixTests(unittest.TestCase):
    def test_ranger_geometry_matrix_and_firing_cells(self):
        self.assertEqual(intermediate_cells((0, 0), (3, 3)), ((1, 1), (2, 2)))
        self.assertEqual(ranger_line_distance((0, 0), (3, 0)), 3)
        self.assertIsNone(ranger_line_distance((0, 0), (2, 1)))
        self.assertIsNone(ranger_line_distance((0, 0), (4, 0)))
        cells = ranger_firing_cells((3, 0), frozenset({(2, 0)}))
        self.assertEqual(cells, ranger_firing_cells((3, 0), frozenset({(2, 0)})))
        self.assertNotIn((0, 0), cells)
        self.assertIn((3, 3), cells)
        self.assertTrue(all(ranger_line_distance(cell, (3, 0)) in {1, 2, 3} for cell in cells))

    def test_production_sequence_resolves_vanguard_then_ranger_without_retry(self):
        core = {"kind": "CORE", "id": "core", "controlled": True,
                "position": [0, 0], "hp": 5, "shield": 5, "state": "NORMAL"}
        workers = [{"kind": "UNIT", "id": f"w{i}", "controlled": True,
                    "position": [i + 1, 0], "unit_type": "WORKER", "cargo": 0, "hp": 2}
                   for i in range(17)]
        memory = ExplorationMemory()
        first = snapshot_from_state(10, {"status": "ACTIVE", "resources": 80, "population": 17,
            "upkeep_next_tick": 0, "objects": [core, *workers], "events": []})
        first_plan = economy_plan(first, memory, combat_mode="production")
        self.assertEqual(first_plan.core_action, {"type": "SPAWN", "unit_type": "VANGUARD"})
        memory.combat.mark_spawn_accepted(10)
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [0, 1], "unit_type": "VANGUARD", "hp": 4}
        succeeded = {"event_id": "spawn-v", "event_type": "CORE_SPAWN_SUCCEEDED",
                     "target_id": "guard", "values": {"unit_type": "VANGUARD"}}
        second = snapshot_from_state(11, {"status": "ACTIVE", "resources": 70, "population": 18,
            "upkeep_next_tick": 0, "objects": [core, *workers, guard], "events": [succeeded]})
        second_plan = economy_plan(second, memory, combat_mode="production")
        self.assertEqual(memory.combat.home_vanguard_id, "guard")
        self.assertEqual(second_plan.core_action, {"type": "SPAWN", "unit_type": "RANGER"})
        self.assertEqual(second.population + 1, 19)

    def test_ranger_moves_to_legal_firing_cell_for_current_threat(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [-2, 0], "unit_type": "RANGER", "hp": 2}
        enemy = {"kind": "UNIT", "id": "enemy", "controlled": False,
                 "position": [2, 1], "unit_type": "WORKER", "hp": 2}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, ranger, enemy], "events": []})
        plan = economy_plan(state, ExplorationMemory(), combat_mode="live")
        self.assertEqual(plan.combat_decisions["ranger"]["state"], "RESPOND")
        target = tuple(plan.combat_decisions["ranger"]["candidate_cell"])
        self.assertIn(target, ranger_firing_cells((2, 1), state.obstacle_cells))
        self.assertEqual(plan.unit_actions["ranger"]["type"], "MOVE")

    def test_attack_modes_open_in_strict_order(self):
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [1, 0], "unit_type": "VANGUARD", "hp": 4}
        ranger = {"kind": "UNIT", "id": "ranger", "controlled": True,
                  "position": [0, 0], "unit_type": "RANGER", "hp": 2}
        adjacent = {"kind": "UNIT", "id": "adjacent", "controlled": False,
                    "position": [2, 0], "unit_type": "WORKER", "hp": 2}
        precision = {"kind": "UNIT", "id": "precision", "controlled": False,
                     "position": [3, 0], "unit_type": "WORKER", "hp": 2}
        state = snapshot_from_state(1, {"status": "ACTIVE", "resources": 0, "population": 2,
            "objects": [core, guard, ranger, adjacent, precision], "events": []})
        positioning = economy_plan(state, ExplorationMemory(), combat_mode="positioning")
        sweep = economy_plan(state, ExplorationMemory(), combat_mode="live-sweep")
        precision_plan = economy_plan(state, ExplorationMemory(), combat_mode="live-precision")
        self.assertNotIn(positioning.unit_actions["guard"]["type"], {"SWEEP", "SHOOT"})
        self.assertEqual(sweep.unit_actions["guard"]["type"], "SWEEP")
        self.assertNotEqual(sweep.unit_actions["ranger"]["type"], "SHOOT")
        self.assertEqual(precision_plan.unit_actions["ranger"]["type"], "SHOOT")

    def test_episode_attributes_economy_events_during_combat(self):
        memory = ExplorationMemory()
        memory.ledger.combat_touch(10)
        events = [
            {"event_id": "combat-h", "event_type": "HARVEST_SUCCEEDED", "actor_id": "worker",
             "position": [1, 0], "values": {"amount": 1}},
            {"event_id": "combat-d", "event_type": "DEPOSIT_SUCCEEDED", "actor_id": "worker",
             "position": [0, 0], "values": {"amount": 1}},
        ]
        state = snapshot_from_state(11, {"status": "ACTIVE", "resources": 1, "population": 0,
            "objects": [], "events": events})
        memory.apply_events(state)
        self.assertEqual(memory.ledger.combat_episode["harvests"], 1)
        self.assertEqual(memory.ledger.combat_episode["deposits"], 1)

    def test_spawn_success_resolves_transaction_before_state_confirmation(self):
        memory = ExplorationMemory(route_core=(0, 0), route_core_id="core")
        memory.combat.request_spawn("VANGUARD", 10, ())
        memory.combat.mark_spawn_accepted(10)
        event = {"event_id": "spawn-resolved", "event_type": "CORE_SPAWN_SUCCEEDED",
                 "target_id": "guard", "values": {"unit_type": "VANGUARD"}}
        state = snapshot_from_state(11, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [], "events": [event]})
        memory.apply_events(state)
        self.assertEqual(memory.combat.last_spawn_result, "RESOLVED")
        self.assertIsNotNone(memory.combat.pending_spawn)

    def test_spawn_success_at_timeout_boundary_confirms_before_timeout(self):
        memory = ExplorationMemory(route_core=(0, 0), route_core_id="core")
        memory.combat.request_spawn("VANGUARD", 10, ())
        memory.combat.mark_spawn_accepted(10)
        core = {"kind": "CORE", "id": "core", "controlled": True, "position": [0, 0]}
        guard = {"kind": "UNIT", "id": "guard", "controlled": True,
                 "position": [0, 1], "unit_type": "VANGUARD", "hp": 4}
        event = {"event_id": "spawn-boundary", "event_type": "CORE_SPAWN_SUCCEEDED",
                 "target_id": "guard", "values": {"unit_type": "VANGUARD"}}
        state = snapshot_from_state(14, {"status": "ACTIVE", "resources": 0, "population": 1,
            "objects": [core, guard], "events": [event]})
        economy_plan(state, memory, combat_mode="production")
        self.assertEqual(memory.combat.last_spawn_result, "CONFIRMED")
        self.assertEqual(memory.combat.home_vanguard_id, "guard")

    def test_open_episode_finalizes_incomplete_idempotently(self):
        ledger = ExplorationMemory().ledger
        ledger.combat_touch(10)
        ledger.finalize_combat_episode("session_end")
        ledger.finalize_combat_episode("session_end")
        self.assertIsNone(ledger.combat_episode)
        self.assertEqual(len(ledger.completed_combat_episodes), 1)
        self.assertEqual(ledger.completed_combat_episodes[0]["outcome"], "INCOMPLETE")
        self.assertEqual(ledger.completed_combat_episodes[0]["close_reason"], "session_end")


if __name__ == "__main__":
    unittest.main()
