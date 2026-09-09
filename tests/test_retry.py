"""Regression tests for `bake retry <run-id>`.

Roadmap quick win: re-run only agents that failed or timed out. A retry bakes
a NEW run (`<run-id>-retry<N>`) from the frozen recipe filtered to the
retryable agents — the original run is never rewritten, and agents that
succeeded keep their results.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from bakery import runner
from bakery.__main__ import main
from bakery.recipe import load_recipe


def _agent_record(state, exit_code):
    return {
        "pid": 100,
        "pgid": 100,
        "state": state,
        "exit_code": exit_code,
        "started_at": "2026-09-09T15:00:00+00:00",
        "finished_at": "2026-09-09T15:00:05+00:00",
        "duration_s": 5.0,
    }


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.tmp.name)
        self.run_id = "orig-1"
        self.recipe_toml = (
            '[bakery]\nname = "t"\nbackend = "shell"\nmax_parallel = 3\n\n'
            '[[agents]]\nname = "ok"\ncmd = ["true"]\ntimeout = 30\n\n'
            '[[agents]]\nname = "bad"\ncmd = ["false"]\ntimeout = 45\n'
            'workdir = "."\n'
            'env = { NOTE = "say \\"hi\\" \\\\ bye" }\n\n'
            '[[agents]]\nname = "slow"\ncmd = ["sleep", "10"]\ntimeout = 1\n'
        )
        self._write_fake_run(
            self.run_id,
            "done",
            {
                "ok": _agent_record("done", 0),
                "bad": _agent_record("done", 1),
                "slow": _agent_record("timeout", -1),
            },
        )

    def _write_fake_run(self, run_id, status, agents, recipe_toml=None):
        run_dir = Path(".bakery") / "runs" / run_id
        (run_dir / "agents").mkdir(parents=True)
        meta = {
            "run_id": run_id,
            "recipe": "t",
            "status": status,
            "started_at": "2026-09-09T15:00:00+00:00",
            "finished_at": "2026-09-09T15:00:05+00:00",
            "retried_from": None,
            "agents": agents,
        }
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        (run_dir / "recipe.toml").write_text(recipe_toml or self.recipe_toml)

    def _simple_recipe(self, names):
        body = '[bakery]\nname = "t"\nbackend = "shell"\nmax_parallel = 3\n'
        for n in names:
            body += f'\n[[agents]]\nname = "{n}"\ncmd = ["true"]\ntimeout = 30\n'
        return body

    def test_retry_runs_only_failed_and_timeout_agents(self):
        new_id = runner.retry_run(self.run_id)
        self.assertEqual(new_id, "orig-1-retry1")
        run_dir = Path(".bakery") / "runs" / new_id
        # the frozen recipe is filtered to exactly the retryable agents
        retry_recipe = load_recipe(str(run_dir / "recipe.toml"))
        self.assertEqual([a.name for a in retry_recipe.agents], ["bad", "slow"])
        # agent specs survive the TOML round trip (incl. tricky env values)
        bad = next(a for a in retry_recipe.agents if a.name == "bad")
        self.assertEqual(bad.cmd, ["false"])
        self.assertEqual(bad.timeout, 45)
        self.assertEqual(bad.env, {"NOTE": 'say "hi" \\ bye'})
        # meta is a fresh pending run that records its provenance
        meta = json.loads((run_dir / "meta.json").read_text())
        self.assertEqual(meta["retried_from"], self.run_id)
        self.assertEqual(sorted(meta["agents"]), ["bad", "slow"])
        self.assertTrue(all(s["state"] == "pending" for s in meta["agents"].values()))
        # the original run is untouched
        orig = json.loads(
            (Path(".bakery") / "runs" / self.run_id / "meta.json").read_text()
        )
        self.assertEqual(sorted(orig["agents"]), sorted(["ok", "bad", "slow"]))

    def test_retry_nothing_to_retry(self):
        agents = {"ok": _agent_record("done", 0), "fine": _agent_record("done", 0)}
        self._write_fake_run("clean-1", "done", agents, self._simple_recipe(["ok", "fine"]))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(runner.retry_run("clean-1"))
        self.assertIn("nothing to retry", out.getvalue())
        self.assertFalse((Path(".bakery") / "runs" / "clean-1-retry1").exists())

    def test_retry_killed_agents_are_not_retried(self):
        agents = {"ok": _agent_record("done", 0), "zap": _agent_record("killed", None)}
        self._write_fake_run("killed-1", "killed", agents, self._simple_recipe(["ok", "zap"]))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(runner.retry_run("killed-1"))
        self.assertIn("nothing to retry", out.getvalue())

    def test_retry_unknown_run_exits(self):
        with self.assertRaises(SystemExit):
            runner.retry_run("nope")

    def test_retry_refuses_active_run(self):
        self._write_fake_run("live-1", "running", {"ok": _agent_record("done", 0)})
        with self.assertRaises(SystemExit):
            runner.retry_run("live-1")

    def test_retry_suffix_increments(self):
        (Path(".bakery") / "runs" / "orig-1-retry1").mkdir(parents=True)
        self.assertEqual(runner.retry_run(self.run_id), "orig-1-retry2")

    def test_retry_cli(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            main(["retry", self.run_id])
        self.assertIn("orig-1-retry1", out.getvalue())
        self.assertTrue((Path(".bakery") / "runs" / "orig-1-retry1" / "meta.json").exists())


if __name__ == "__main__":
    unittest.main()
