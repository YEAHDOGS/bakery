"""Regression tests for two-phase timeouts: SIGTERM warning, then SIGKILL.

ROADMAP quick win: instead of killing an over-budget agent with SIGKILL
out of the blue, the supervisor sends SIGTERM first and gives the agent
`timeout_grace` seconds (per-agent recipe field, default 5) to shut down
cleanly. Only agents that ignore SIGTERM get SIGKILLed.

Contract under test:
- recipe: `timeout_grace` parses, validates (>= 0, integer), defaults to 5;
- runner: over-budget agents finish with state="timeout", exit_code=-1
  either way; the audit trail records `agent.timeout_warn` and
  `agent.finished` with terminated_by="sigterm"|"sigkill";
- `bake retry` preserves `timeout_grace` in the frozen recipe it re-bakes.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from bakery import audit
from bakery import runner
from bakery.recipe import Recipe, AgentSpec, load_recipe


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


HEAD = '[bakery]\nname = "grace"\nbackend = "shell"\nmax_parallel = 1\n\n'
AGENT = '[[agents]]\nname = "{name}"\ncmd = ["true"]\ntimeout = 30\n'


class TimeoutGraceRecipeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _load(self, agent_block: str) -> Recipe:
        return load_recipe(str(_write(Path(self.tmp.name) / "r.toml", HEAD + agent_block)))

    def test_default_is_five_seconds(self):
        r = self._load(AGENT.format(name="a"))
        self.assertEqual(r.agents[0].timeout_grace, 5)

    def test_explicit_value_is_honored(self):
        r = self._load(AGENT.format(name="a") + "timeout_grace = 12\n")
        self.assertEqual(r.agents[0].timeout_grace, 12)

    def test_zero_disables_the_grace_window(self):
        r = self._load(AGENT.format(name="a") + "timeout_grace = 0\n")
        self.assertEqual(r.agents[0].timeout_grace, 0)

    def test_negative_is_rejected(self):
        with self.assertRaises(ValueError):
            self._load(AGENT.format(name="a") + "timeout_grace = -1\n")

    def test_non_integer_is_rejected(self):
        with self.assertRaises(ValueError):
            self._load(AGENT.format(name="a") + 'timeout_grace = "soon"\n')

    def test_retry_dump_preserves_grace(self):
        recipe = Recipe(
            "g", "shell", 1, [AgentSpec("a", ["true"], timeout=30, timeout_grace=9)]
        )
        text = runner._dump_recipe_toml(recipe, recipe.agents)
        reloaded = load_recipe(str(_write(Path(self.tmp.name) / "frozen.toml", text)))
        self.assertEqual(reloaded.agents[0].timeout_grace, 9)


class _RunFixture(unittest.TestCase):
    """Self-contained e2e harness: tmp CWD + PYTHONPATH so the detached
    supervisor can re-exec `python -m bakery _supervise <run-id>`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.tmp.name)
        self.old_environ = dict(os.environ)
        self.addCleanup(os.environ.update, self.old_environ)
        self.addCleanup(os.environ.clear)
        repo_root = str(Path(__file__).resolve().parent.parent)
        old_pp = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = repo_root + (os.pathsep + old_pp if old_pp else "")

    def _wait_for_run(self, run_id: str, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            meta = json.loads((runner.runs_root() / run_id / "meta.json").read_text())
            if meta["status"] not in ("starting", "running"):
                return
            time.sleep(0.2)
        raise AssertionError(f"run {run_id} did not finish within {timeout}s")


class TimeoutGraceE2ETest(_RunFixture):
    TRAP_EXIT = (
        "import signal, time, pathlib;"
        "signal.signal(signal.SIGTERM,"
        " lambda s, f: (pathlib.Path('marker-done').touch(), exit(0)));"
        "time.sleep(60)"
    )
    IGNORE_TERM = (
        "import signal, time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(60)"
    )

    def _recipe(self, name: str, code: str, timeout: int, grace: int | None) -> str:
        cmd = f'cmd = ["python3", "-c", {json.dumps(code)}]\n'
        block = (
            f'[[agents]]\nname = "{name}"\n' + cmd + f"timeout = {timeout}\n"
        )
        if grace is not None:
            block += f"timeout_grace = {grace}\n"
        p = Path(self.tmp.name) / f"{name}.toml"
        p.write_text(HEAD + block)
        return str(p)

    def test_sigterm_shutdown_is_a_timeout_not_a_done(self):
        run_id = "grace-sigterm-1"
        runner.start_run(
            self._recipe("cooperative", self.TRAP_EXIT, timeout=2, grace=None), run_id
        )
        self._wait_for_run(run_id)
        meta = json.loads((runner.runs_root() / run_id / "meta.json").read_text())
        st = meta["agents"]["cooperative"]
        self.assertEqual(st["state"], "timeout")
        self.assertEqual(st["exit_code"], -1)
        # the agent DID get the SIGTERM warning: it ran its trap before exiting
        self.assertTrue(
            Path(self.tmp.name, "marker-done").exists(),
            "agent never saw SIGTERM — the warning phase did not fire",
        )
        events = audit.read_events(run_id)
        warns = [e for e in events if e["event"] == "agent.timeout_warn"]
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["agent"], "cooperative")
        self.assertEqual(warns[0]["grace"], 5)
        finished = [e for e in events if e["event"] == "agent.finished"][0]
        self.assertEqual(finished["terminated_by"], "sigterm")
        self.assertIn("SIGTERM", audit.render_audit(run_id, event="agent.timeout_warn"))
        self.assertIn("sigterm", audit.render_audit(run_id))

    def test_sigterm_ignorer_gets_sigkill_after_grace(self):
        run_id = "grace-sigkill-1"
        t0 = time.time()
        runner.start_run(
            self._recipe("stubborn", self.IGNORE_TERM, timeout=1, grace=2), run_id
        )
        self._wait_for_run(run_id)
        elapsed = time.time() - t0
        meta = json.loads((runner.runs_root() / run_id / "meta.json").read_text())
        st = meta["agents"]["stubborn"]
        self.assertEqual(st["state"], "timeout")
        self.assertEqual(st["exit_code"], -1)
        self.assertGreaterEqual(
            elapsed, 3.0, "SIGKILL fired before the grace window expired"
        )
        events = audit.read_events(run_id)
        self.assertEqual(
            [e for e in events if e["event"] == "agent.timeout_warn"][0]["grace"], 2
        )
        finished = [e for e in events if e["event"] == "agent.finished"][0]
        self.assertEqual(finished["terminated_by"], "sigkill")
        self.assertIn("sigkill", audit.render_audit(run_id))


if __name__ == "__main__":
    unittest.main()
