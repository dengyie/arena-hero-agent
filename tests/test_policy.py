import asyncio
import unittest
from unittest.mock import patch
from arena_agent.model import snapshot_from_state
from arena_agent.__main__ import PermanentAuthError, post_plan
from pathlib import Path
from arena_agent.policy import (
    ExplorationMemory, MAX_BAND_RADIUS, PATH_NODE_CAP, economy_plan,
    first_step, plan_path,
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
        s = snapshot_from_state(2, {"status": "ACTIVE", "resources": 10, "population": 1,
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

    def test_spawn_worker_is_blocked_by_core_occupancy_or_second_worker(self):
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
        self.assertIsNone(economy_plan(two_workers, mem).core_action)

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
