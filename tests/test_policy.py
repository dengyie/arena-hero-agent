import asyncio
import unittest
from unittest.mock import patch
from arena_agent.model import snapshot_from_state
from arena_agent.__main__ import PermanentAuthError, post_plan
from arena_agent.policy import economy_plan, first_step

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
        p = economy_plan(self.state())
        self.assertEqual(p.unit_actions["worker"], {"type":"MOVE","direction":"DOWN"})

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
