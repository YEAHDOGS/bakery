"""Tests for `bake retry`: re-run only the agents that did not succeed.

End-to-end through the fixture backend (no live AI calls, no network):
a run with one clean agent, one nonzero-exit agent, and one timed-out
agent retries exactly the two failures as a fresh run, leaving the
original run's evidence untouched.
"""

import os
import tempfile
import unittest
from pathlib import Path

from bakery import recipe as recipe_mod
from bakery import report, runner


RECIPE = """\
[bakery]
name = "retry-e2e"
backend = "fixture"
max_parallel = 2

[[agents]]
name = "good"
report = "all fine"
exit_code = 0

[[agents]]
name = "bad"
report = "blew up"
exit_code = 3

[[agents]]
name = "slow"
report = "too slow"
delay = 3
timeout = 1
"""

ALL_GOOD_RECIPE = """\
[bakery]
name = "retry-clean"
backend = "fixture"
max_parallel = 2

[[agents]]
name = "good"
report = "all fine"
exit_code = 0
"""


def _bake_in_tmp(recipe_text: str):
    """Start a run inside a fresh temp cwd; restore cwd on cleanup.

    The detached supervisor inherits this cwd, so everything (run dirs,
    wait_for_run, retry) must happen while we sit in it.
    """
    tmp = Path(tempfile.mkdtemp())
    cwd = os.getcwd()
    os.chdir(tmp)
    recipe_p = tmp / "r.toml"
    recipe_p.write_text(recipe_text)
    run_id = runner.start_run(str(recipe_p))
    return run_id, tmp, cwd


class RetryTest(unittest.TestCase):
    def test_retry_reruns_only_failed_agents(self):
        run_id, tmp, cwd = _bake_in_tmp(RECIPE)
        self.addCleanup(os.chdir, cwd)
        try:
            meta = report.wait_for_run(run_id, timeout=60)
            agents = meta["agents"]
            self.assertEqual(agents["good"]["state"], "done")
            self.assertEqual(agents["good"]["exit_code"], 0)
            self.assertEqual(agents["bad"]["state"], "done")
            self.assertEqual(agents["bad"]["exit_code"], 3)
            self.assertEqual(agents["slow"]["state"], "timeout")

            new_id = runner.retry(run_id)
            self.assertIsNotNone(new_id)
            new_meta = runner._load_meta(new_id)
            self.assertEqual(set(new_meta["agents"]), {"bad", "slow"})
            self.assertEqual(new_meta["retry_of"], run_id)
            self.assertEqual(new_meta["retried_agents"], ["bad", "slow"])

            # The retry run's frozen recipe holds exactly the failed agents.
            retry_recipe = recipe_mod.load_recipe(
                str(tmp / ".bakery" / "runs" / new_id / "recipe.toml")
            )
            self.assertEqual([a.name for a in retry_recipe.agents], ["bad", "slow"])

            new_meta = report.wait_for_run(new_id, timeout=60)
            self.assertEqual(new_meta["agents"]["bad"]["state"], "done")
            self.assertEqual(new_meta["agents"]["bad"]["exit_code"], 3)
            self.assertEqual(new_meta["agents"]["slow"]["state"], "timeout")
            self.assertEqual(new_meta["agents"]["bad"]["attempts"], 1)

            # Original run's evidence is untouched.
            good_out = tmp / ".bakery" / "runs" / run_id / "agents" / "good.out"
            self.assertIn("all fine", good_out.read_text())
        finally:
            os.chdir(cwd)

    def test_retry_nothing_to_retry_when_all_succeed(self):
        run_id, tmp, cwd = _bake_in_tmp(ALL_GOOD_RECIPE)
        self.addCleanup(os.chdir, cwd)
        try:
            report.wait_for_run(run_id, timeout=60)
            result = runner.retry(run_id)
            self.assertIsNone(result)
            runs = list((tmp / ".bakery" / "runs").iterdir())
            self.assertEqual([d.name for d in runs], [run_id])  # no retry run created
        finally:
            os.chdir(cwd)

    def test_retry_refuses_running_run(self):
        run_id, tmp, cwd = _bake_in_tmp(RECIPE)
        self.addCleanup(os.chdir, cwd)
        try:
            with self.assertRaises(SystemExit):
                runner.retry(run_id)
        finally:
            runner.kill(run_id)  # leave nothing running
            os.chdir(cwd)

    def test_retry_unknown_run_errors_cleanly(self):
        tmp = Path(tempfile.mkdtemp())
        cwd = os.getcwd()
        os.chdir(tmp)
        self.addCleanup(os.chdir, cwd)
        with self.assertRaises(SystemExit):
            runner.retry("no-such-run")


if __name__ == "__main__":
    unittest.main()
