"""Tests for graceful supervisor termination: SIGTERM grace before SIGKILL.

Regression coverage for the supervisor's timeout path in bakery/runner.py.
Before the change, over-time agents were SIGKILLed immediately: an agent
that could have flushed its final output and exited cleanly lost both.
Now the supervisor SIGTERMs, waits up to TERMINATE_GRACE_S, and only then
SIGKILLs — cooperative agents keep their output and real exit code, stuck
agents still die.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from bakery import report, runner


def _recipe_text(agent_cmd: str, timeout: int = 1) -> str:
    return (
        "[bakery]\nname = \"grace-e2e\"\nbackend = \"shell\"\nmax_parallel = 1\n"
        "[[agents]]\nname = \"worker\"\n"
        f"cmd = [\"bash\", \"-c\", {json.dumps(agent_cmd)}]\n"
        f"timeout = {timeout}\n"
    )


def _bake_and_meta(cmd: str, timeout: int = 1):
    """Start a one-agent run in a temp cwd; return (final meta, agent stdout)."""
    tmp = Path(tempfile.mkdtemp())
    recipe_p = tmp / "r.toml"
    recipe_p.write_text(_recipe_text(cmd, timeout))
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        run_id = runner.start_run(str(recipe_p))
        meta = report.wait_for_run(run_id, timeout=60)
        out = (tmp / ".bakery" / "runs" / run_id / "agents" / "worker.out").read_text()
    finally:
        os.chdir(cwd)
    return meta, out


class GracefulTerminationTest(unittest.TestCase):
    def test_cooperative_agent_exits_cleanly_on_sigterm(self):
        """A TERM-trapping agent flushes its final line and keeps its exit code."""
        cmd = "trap 'echo FLUSHED; exit 0' TERM; sleep 30"
        meta, out = _bake_and_meta(cmd)
        st = meta["agents"]["worker"]
        self.assertEqual(st["state"], "timeout")  # it still exceeded its timeout
        self.assertEqual(st["exit_code"], 0)      # ...but shut down cooperatively
        self.assertIn("FLUSHED", out)             # ...and flushed its final line

    def test_stubborn_agent_gets_sigkill_after_grace(self):
        """An agent ignoring SIGTERM is still killed; the run finishes."""
        cmd = "trap '' TERM; sleep 30"
        meta, out = _bake_and_meta(cmd)
        st = meta["agents"]["worker"]
        self.assertEqual(st["state"], "timeout")
        self.assertEqual(st["exit_code"], -9)  # SIGKILL


if __name__ == "__main__":
    unittest.main()
