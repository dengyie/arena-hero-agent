import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "pxed-deploy.sh"


class DeployScriptTests(unittest.TestCase):
    def make_source(self, base: Path) -> Path:
        source = base / "source"
        shutil.copytree(ROOT / "arena_agent", source / "arena_agent")
        shutil.copytree(ROOT / "deploy", source / "deploy")
        (source / "scripts").mkdir()
        (source / "scripts" / "evaluate-combat-v2.py").write_text("print('ok')\n")
        (source / "tests").mkdir()
        (source / "tests" / "test_smoke.py").write_text(
            "import unittest\n\nclass SmokeTest(unittest.TestCase):\n"
            "    def test_source_tree(self):\n        self.assertTrue(True)\n"
        )
        (source / "README.md").write_text("test source\n")
        (source / "requirements.txt").write_text("websockets\n")
        return source

    def test_source_sync_preserves_runtime_and_secret_and_renders_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = self.make_source(base)
            app = base / "app"
            supervisor = base / "arena.conf"
            app.mkdir()
            (app / ".env.protected").write_text("ARENA_HERO_TOKEN=test-only\n")
            (app / "runtime").mkdir()
            (app / "runtime" / "journal.jsonl").write_text("keep\n")
            (app / "private-note.txt").write_text("keep private\n")
            (app / "arena_agent").mkdir()
            (app / "arena_agent" / "stale.py").write_text("remove\n")
            env = os.environ | {
                "ARENA_HERO_APP_DIR": str(app),
                "ARENA_HERO_ENV_FILE": str(app / ".env.protected"),
                "ARENA_HERO_SUPERVISOR_CONF": str(supervisor),
                "ARENA_HERO_PYTHON": subprocess.check_output(
                    ["command", "-v", "python3"], text=True, shell=True
                ).strip(),
            }
            result = subprocess.run([
                "bash", str(SCRIPT), "--source-dir", str(source), "--live",
                "--combat-mode", "production", "--prepare-only",
            ], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((app / ".env.protected").exists())
            self.assertEqual((app / "runtime" / "journal.jsonl").read_text(), "keep\n")
            self.assertEqual((app / "private-note.txt").read_text(), "keep private\n")
            self.assertFalse((app / "arena_agent" / "stale.py").exists())
            self.assertTrue((app / "arena_agent" / "policy.py").exists())
            self.assertTrue((app / "scripts" / "evaluate-combat-v2.py").exists())
            self.assertIn("--combat-mode production", supervisor.read_text())

    def test_invalid_combat_mode_is_rejected_before_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            source = self.make_source(Path(temp))
            result = subprocess.run([
                "bash", str(SCRIPT), "--source-dir", str(source), "--live",
                "--combat-mode", "everything", "--prepare-only",
            ], text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid combat mode", result.stderr)


if __name__ == "__main__":
    unittest.main()