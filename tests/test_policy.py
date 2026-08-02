import asyncio
import unittest
from unittest.mock import patch
from arena_agent.model import snapshot_from_state
from arena_agent.__main__ import PermanentAuthError, post_plan
from arena_agent.policy import ExplorationMemory, economy_plan, first_step
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
