"""Regression tests for the audit trail (bakery.audit + runner wiring).

VISION.md's non-negotiable rule: every agent action is logged, so `bake
logs` (now `bake audit`) can answer "who did what, when" for any run. The
audit log is evidence — it must never become a secret leak of its own:
env VALUES are never recorded (names only), and the supervisor's exported
secrets must not appear anywhere in audit.jsonl.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from bakery import audit
from bakery import runner


def _wait_for_run(run_id: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = json.loads((runner.runs_root() / run_id / "meta.json").read_text())
        if meta["status"] not in ("starting", "running"):
            return
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


class AuditFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.tmp.name)
        self.old_environ = dict(os.environ)
        # LIFO order: clear runs first, then restore — the reverse order
        # wipes os.environ entirely and breaks later tests in the suite.
        self.addCleanup(os.environ.update, self.old_environ)
        self.addCleanup(os.environ.clear)
        # The detached supervisor re-execs `python -m bakery` with this CWD.
        repo_root = str(Path(__file__).resolve().parent.parent)
        old_pp = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = repo_root + (os.pathsep + old_pp if old_pp else "")


class AuditUnitTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir_prev = None  # noqa — no cwd change needed
            # append_event writes into .bakery/runs/<run_id> relative to CWD
            with _cd(tmp):
                Path(".bakery/runs/r1").mkdir(parents=True)
                audit.append_event("r1", "run.started", recipe="x", agents=2)
                audit.append_event("r1", "agent.finished", agent="a", state="done")
            events = _read_in(tmp, "r1")
        self.assertEqual([e["event"] for e in events], ["run.started", "agent.finished"])
        self.assertTrue(all(e["run_id"] == "r1" and "ts" in e for e in events))

    def test_corrupt_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _cd(tmp):
                Path(".bakery/runs/r2").mkdir(parents=True)
                p = Path(".bakery/runs/r2/audit.jsonl")
                p.write_text('{"event": "ok"}\nnot json at all\n{"event": "ok2"}\n')
            events = _read_in(tmp, "r2")
        self.assertEqual([e["event"] for e in events], ["ok", "ok2"])

    def test_render_audit_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _cd(tmp):
                Path(".bakery/runs/r3").mkdir(parents=True)
                audit.append_event("r3", "run.started", recipe="x", agents=1)
                audit.append_event("r3", "agent.launched", agent="a1")
                audit.append_event("r3", "agent.launched", agent="a2")
            out = _render_in(tmp, "r3")
            self.assertIn("run.started", out)
            self.assertIn("agent.launched", out)
            self.assertIn("a1", _render_in(tmp, "r3", agent="a1"))
            self.assertNotIn("a2", _render_in(tmp, "r3", agent="a1"))
            self.assertNotIn("agent.launched", _render_in(tmp, "r3", event="run.started"))
        # unknown run: no crash, honest message
        self.assertIn("no audit events", audit.render_audit("does-not-exist"))


class _cd:
    def __init__(self, path):
        self.path = path
        self.prev = None

    def __enter__(self):
        self.prev = Path.cwd()
        os.chdir(self.path)
        return self

    def __exit__(self, *a):
        os.chdir(self.prev)


def _read_in(tmp, run_id):
    prev = Path.cwd()
    os.chdir(tmp)
    try:
        return audit.read_events(run_id)
    finally:
        os.chdir(prev)


def _render_in(tmp, run_id, **kw):
    prev = Path.cwd()
    os.chdir(tmp)
    try:
        return audit.render_audit(run_id, **kw)
    finally:
        os.chdir(prev)


class AuditE2ETest(AuditFixture):
    FAKE_TOKEN = "ghp_" + "audit-test-token-" + "y" * 20

    def setUp(self):
        super().setUp()
        os.environ["GITHUB_TOKEN"] = self.FAKE_TOKEN
        os.environ["SOME_EXPORTED_SECRET"] = "sk-" + "s" * 30

    def test_full_lifecycle_events_and_no_secrets(self):
        recipe = Path(self.tmp.name) / "r.toml"
        recipe.write_text(
            '[bakery]\nname = "audit-e2e"\nbackend = "shell"\nmax_parallel = 2\n\n'
            '[[agents]]\nname = "quick"\ncmd = ["bash", "-c", "echo hi"]\ntimeout = 30\n'
            'env = { MARKER = "from-recipe" }\n\n'
            '[[agents]]\nname = "quicker"\ncmd = ["bash", "-c", "exit 3"]\ntimeout = 30\n'
        )
        run_id = "audit-e2e-1"
        runner.start_run(str(recipe), run_id)
        _wait_for_run(run_id)
        events = audit.read_events(run_id)
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[0], "run.started")
        self.assertIn("agent.launched", kinds)
        self.assertIn("agent.finished", kinds)
        self.assertIn("run.finished", kinds)
        # launched events carry env NAMES only — no values anywhere in the file
        audit_text = (Path(".bakery") / "runs" / run_id / "audit.jsonl").read_text()
        self.assertNotIn(self.FAKE_TOKEN, audit_text)
        self.assertNotIn("from-recipe", audit_text, "env value leaked into audit log")
        self.assertIn("MARKER", audit_text, "env name should be auditable")
        # the failing agent's outcome is recorded honestly
        finished = {e["agent"]: e for e in events if e["event"] == "agent.finished"}
        self.assertEqual(finished["quick"]["state"], "done")
        self.assertEqual(finished["quick"]["exit_code"], 0)
        self.assertEqual(finished["quicker"]["exit_code"], 3)

    def test_kill_is_audited(self):
        recipe = Path(self.tmp.name) / "k.toml"
        recipe.write_text(
            '[bakery]\nname = "audit-kill"\nbackend = "shell"\nmax_parallel = 1\n\n'
            '[[agents]]\nname = "sleeper"\ncmd = ["bash", "-c", "sleep 60"]\ntimeout = 120\n'
        )
        run_id = "audit-kill-1"
        runner.start_run(str(recipe), run_id)
        # wait until the agent is actually running, then kill
        deadline = time.time() + 15
        while time.time() < deadline:
            meta = json.loads((runner.runs_root() / run_id / "meta.json").read_text())
            if meta["agents"]["sleeper"]["state"] == "running":
                break
            time.sleep(0.2)
        else:
            self.fail("agent never reached running state")
        runner.kill(run_id)
        time.sleep(2)  # let the supervisor notice the kill
        events = audit.read_events(run_id)
        kinds = [e["event"] for e in events]
        self.assertIn("agent.launched", kinds)
        self.assertIn("agent.killed", kinds)
        self.assertIn("run.killed", kinds)
        self.assertNotIn("agent.finished", kinds, "killed agent must not also get a finished event")
        self.assertNotIn("run.finished", kinds, "killed run must not get a finished event")


if __name__ == "__main__":
    unittest.main()
