"""Tests for `bake run --watch` / `bake watch` — stream status until a run finishes.

Covers runner.watch() against fabricated run dirs (fast, no subprocesses)
plus one end-to-end CLI run through `python -m bakery run --watch`.
"""

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bakery import __main__ as cli
from bakery import runner


def _write_meta(run_dir: Path, run_id: str, status: str = "running",
                sup_pid: int | None = None, states: dict | None = None) -> dict:
    """Write a synthetic meta.json; return the meta dict."""
    states = states or {"a": "running", "b": "pending"}
    meta = {
        "run_id": run_id,
        "recipe": "watch-test",
        "status": status,
        "started_at": "2026-09-09T00:00:00",
        "finished_at": None,
        "agents": {
            name: {
                "pid": None, "pgid": None, "state": st, "exit_code": None,
                "started_at": None, "finished_at": None, "duration_s": None,
                "attempts": 0,
            }
            for name, st in states.items()
        },
    }
    if sup_pid is not None:
        meta["supervisor_pid"] = sup_pid
    (run_dir / "meta.json").write_text(json.dumps(meta))
    return meta


class WatchRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        (self.tmp / ".bakery" / "runs").mkdir(parents=True)

    def tearDown(self):
        os.chdir(self.cwd)

    def _run_dir(self, run_id: str) -> Path:
        d = self.tmp / ".bakery" / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_returns_meta_and_prints_finished(self):
        """watch() blocks until the run turns done, then returns the meta."""
        run_dir = self._run_dir("w1")
        _write_meta(run_dir, "w1")

        def flip():
            time.sleep(0.4)
            _write_meta(run_dir, "w1", status="done",
                        states={"a": "done", "b": "done"})

        threading.Thread(target=flip, daemon=True).start()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            meta = runner.watch("w1", poll=0.05, timeout=10)
        out = buf.getvalue()
        self.assertEqual(meta["status"], "done")
        self.assertIn("run w1 finished:", out)
        self.assertIn("done 2/2 done", out)

    def test_prints_snapshots_only_on_change(self):
        """A static swarm state prints once, not once per tick."""
        run_dir = self._run_dir("w2")
        _write_meta(run_dir, "w2")

        def flip():
            time.sleep(0.45)
            _write_meta(run_dir, "w2", status="done",
                        states={"a": "done", "b": "done"})

        threading.Thread(target=flip, daemon=True).start()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.watch("w2", poll=0.05, timeout=10)
        progress = [l for l in buf.getvalue().splitlines() if "finished:" not in l]
        # ~9 ticks while running but the state never changes -> one line
        self.assertEqual(len(progress), 1)
        self.assertIn("running 0/2 done", progress[0])

    def test_killed_is_terminal(self):
        """watch() returns immediately for an already-killed run."""
        run_dir = self._run_dir("w3")
        _write_meta(run_dir, "w3", status="killed",
                    states={"a": "killed", "b": "done"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            meta = runner.watch("w3", poll=0.05, timeout=10)
        self.assertEqual(meta["status"], "killed")
        self.assertIn("run w3 finished:", buf.getvalue())

    def test_supervisor_death_raises(self):
        """A dead supervisor with no terminal state raises RuntimeError."""
        run_dir = self._run_dir("w4")
        _write_meta(run_dir, "w4", sup_pid=2**31 - 1)  # can't be a live pid
        with self.assertRaisesRegex(RuntimeError, "supervisor.*died"):
            runner.watch("w4", poll=0.05, timeout=10)

    def test_timeout_raises(self):
        """watch() gives up at the deadline instead of blocking forever."""
        run_dir = self._run_dir("w5")
        _write_meta(run_dir, "w5", sup_pid=os.getpid())  # alive; no dead-check trip
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaisesRegex(TimeoutError, "did not finish"):
                runner.watch("w5", poll=0.05, timeout=0.5)

    def test_e2e_run_watch_cli(self):
        """`bake run --watch` streams a real two-agent swarm to completion."""
        recipe = (
            "[bakery]\nname = \"watch-e2e\"\nbackend = \"shell\"\nmax_parallel = 2\n"
            "[[agents]]\nname = \"fast\"\n"
            "cmd = [\"bash\", \"-lc\", \"echo hi; sleep 1\"]\ntimeout = 30\n"
            "[[agents]]\nname = \"slow\"\n"
            "cmd = [\"bash\", \"-lc\", \"echo yo; sleep 2\"]\ntimeout = 30\n"
        )
        recipe_p = self.tmp / "r.toml"
        recipe_p.write_text(recipe)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["run", str(recipe_p), "--watch", "--timeout", "60"])
        out = buf.getvalue()
        self.assertIn("baked run", out)
        self.assertIn("finished: done 2/2 done", out)

    def test_e2e_watch_command_on_unknown_run(self):
        """`bake watch` on a bogus id fails loudly (SystemExit)."""
        with self.assertRaises(SystemExit):
            cli.main(["watch", "no-such-run"])


if __name__ == "__main__":
    unittest.main()
