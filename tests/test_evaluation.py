import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from arena_agent.evaluation import evaluate_combat_session
from arena_agent.journal import Journal


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "evaluate-combat-v2.py"


class CombatEvaluationTests(unittest.TestCase):
    def test_journal_rotates_and_evaluator_reads_receipt_from_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "arena.jsonl"
            journal = Journal(path, max_bytes=260, backups=2)
            journal.write("plan", session="rotated", tick=1, result={"status": 202},
                          state={"metric_window_eligible": True, "events": [],
                                 "phase_evaluation": {"clean_combat_episodes": []}})
            journal.write("received", session="rotated", data={"tick": 1})
            journal.write("padding", session="other", value="x" * 240)
            self.assertTrue(path.with_name("arena.jsonl.1").exists())
            result = evaluate_combat_session(path, "rotated")
            self.assertEqual(result["resolved_ticks"], 1)

    def test_oversized_legacy_backup_is_compressed_and_scanned(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "arena.jsonl"
            backup = path.with_name("arena.jsonl.1")
            rows = [
                {"event": "plan", "session": "legacy", "tick": 1,
                 "result": {"status": 202}, "state": {
                     "metric_window_eligible": True, "events": [],
                     "phase_evaluation": {"clean_combat_episodes": []}}},
                {"event": "received", "session": "legacy", "data": {"tick": 1}},
            ]
            backup.write_text("".join(json.dumps(row) + "\n" for row in rows) + "x" * 300)
            journal = Journal(path, max_bytes=100, backups=2)
            self.assertTrue(path.with_name("arena.jsonl.legacy-1.gz").exists())
            result = evaluate_combat_session(path, "legacy")
            self.assertEqual(result["resolved_ticks"], 1)

    def test_evaluator_deduplicates_repeated_event_ids(self):
        event = {"event_id": "same", "event_type": "SHOT_HIT"}
        rows = []
        for tick in (1, 2):
            rows.extend([
                {"event": "plan", "session": "dedupe", "tick": tick,
                 "result": {"status": 202},
                 "state": {"metric_window_eligible": False, "events": [event],
                           "phase_evaluation": {"clean_combat_episodes": []}}},
                {"event": "received", "session": "dedupe", "data": {"tick": tick}},
            ])
        result = evaluate_combat_session(self.write_rows(rows), "dedupe")
        self.assertEqual(result["event_types"].get("SHOT_HIT"), 1)

    def write_rows(self, rows):
        temp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        with temp:
            for row in rows:
                temp.write(json.dumps(row) + "\n")
        self.addCleanup(Path(temp.name).unlink, missing_ok=True)
        return Path(temp.name)

    def test_clean_session_requires_50_ticks_three_episodes_and_attack_coverage(self):
        episodes = [
            {"outcome": "CLEAN_COMPLETE", "friendly_deaths": 0,
             "friendly_cargo_lost": 0, "sweeps": 1, "precision_shots": 0,
             "cell_intercept_shots": 0},
            {"outcome": "CLEAN_COMPLETE", "friendly_deaths": 0,
             "friendly_cargo_lost": 0, "sweeps": 0, "precision_shots": 1,
             "cell_intercept_shots": 0},
            {"outcome": "CLEAN_COMPLETE", "friendly_deaths": 0,
             "friendly_cargo_lost": 0, "sweeps": 0, "precision_shots": 0,
             "cell_intercept_shots": 1},
        ]
        rows = []
        for tick in range(1, 51):
            event_type = {1: "SWEEP_RESOLVED", 2: "SHOT_HIT", 3: "SHOT_MISSED"}.get(tick)
            rows.append({
                "event": "plan", "session": "clean", "tick": tick,
                "result": {"status": 202},
                "state": {
                    "metric_window_eligible": True,
                    "events": ([{"event_type": event_type}] if event_type else []),
                    "phase_evaluation": {"clean_combat_episodes": episodes},
                },
            })
            rows.append({"event": "received", "session": "clean", "data": {"tick": tick}})
        result = evaluate_combat_session(self.write_rows(rows), "clean")
        self.assertTrue(result["strategy_quality_ready"])
        self.assertEqual(result["eligible_resolved_ticks"], 50)
        self.assertEqual(result["clean_complete_episodes"], 3)

    def test_contaminated_session_is_not_ready_even_with_episodes(self):
        episode = {"outcome": "CLEAN_COMPLETE", "friendly_deaths": 0,
                   "friendly_cargo_lost": 0, "sweeps": 1,
                   "precision_shots": 1, "cell_intercept_shots": 1}
        rows = [{
            "event": "plan", "session": "dirty", "tick": 1,
            "result": {"status": 202},
            "state": {"metric_window_eligible": False, "events": [],
                      "phase_evaluation": {"clean_combat_episodes": [episode, episode, episode]}},
        }, {"event": "received", "session": "dirty", "data": {"tick": 1}}]
        result = evaluate_combat_session(self.write_rows(rows), "dirty")
        self.assertFalse(result["strategy_quality_ready"])
        self.assertIn("eligible_resolved_ticks<50", result["blocking_reasons"])

    def test_cli_runs_directly_from_checkout_and_returns_two_when_blocked(self):
        path = self.write_rows([{
            "event": "plan", "session": "blocked", "tick": 1,
            "result": {"status": 202},
            "state": {"metric_window_eligible": False, "events": [],
                      "phase_evaluation": {"clean_combat_episodes": []}},
        }, {"event": "received", "session": "blocked", "data": {"tick": 1}}])
        result = subprocess.run(
            [sys.executable, str(CLI), str(path), "blocked"],
            text=True, capture_output=True, cwd="/",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('"strategy_quality_ready": false', result.stdout)


if __name__ == "__main__":
    unittest.main()
