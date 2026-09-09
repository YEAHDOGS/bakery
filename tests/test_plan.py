"""Tests for `bake plan` (bakery/plan.py) — dry-run launch planning."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from bakery import plan


RECIPE_TOML = """\
[bakery]
name = "plan-test"
backend = "shell"
max_parallel = 2

[[agents]]
name = "a1"
cmd = ["bash", "-lc", "echo hi"]
timeout = 60
env = { TEAM = "dogs" }

[[agents]]
name = "a2"
cmd = ["bash", "-lc", "echo yo"]

[[agents]]
name = "a3"
cmd = ["bash", "-lc", "echo hey"]
timeout = 120
"""


@contextlib.contextmanager
def isolated_tmp():
    """A cwd with no bake.yaml and no .bakery — plan must create nothing.

    Also pins BAKE_CONFIG/HOME so config discovery can't inherit leaked
    state from other tests or the operator's shell.
    """
    with tempfile.TemporaryDirectory() as tmp:
        old = os.getcwd()
        old_bake = os.environ.pop("BAKE_CONFIG", None)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp
        os.chdir(tmp)
        try:
            yield Path(tmp)
        finally:
            os.chdir(old)
            os.environ["HOME"] = old_home
            if old_bake is not None:
                os.environ["BAKE_CONFIG"] = old_bake
            else:
                os.environ.pop("BAKE_CONFIG", None)


@contextlib.contextmanager
def captured():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


class BakePlanTest(unittest.TestCase):
    def _recipe(self, tmp: Path) -> str:
        p = tmp / "r.toml"
        p.write_text(RECIPE_TOML)
        return str(p)

    def test_waves_batch_fifo(self):
        with isolated_tmp() as tmp:
            recipe = self._recipe(tmp)
            with captured():
                doc = plan.bake_plan(recipe)
        self.assertEqual(doc.waves, [["a1", "a2"], ["a3"]])
        self.assertEqual(doc.max_parallel, 2)

    def test_effective_params_resolved(self):
        with isolated_tmp() as tmp:
            recipe = self._recipe(tmp)
            with captured():
                doc = plan.bake_plan(recipe)
        by_name = {a.name: a for a in doc.agents}
        self.assertEqual(by_name["a1"].timeout, 60)
        self.assertEqual(by_name["a3"].timeout, 120)
        self.assertEqual(by_name["a2"].timeout, 3600)  # recipe default
        self.assertEqual(by_name["a1"].retries, 0)

    def test_spawns_nothing_and_writes_nothing(self):
        with isolated_tmp() as tmp:
            recipe = self._recipe(tmp)
            with captured():
                plan.bake_plan(recipe)
            self.assertFalse((tmp / ".bakery").exists())
            self.assertEqual([p.name for p in sorted(tmp.iterdir())], ["r.toml"])

    def test_only_filters_agents(self):
        with isolated_tmp() as tmp:
            recipe = self._recipe(tmp)
            with captured() as out:
                doc = plan.bake_plan(recipe, only_agents=["a1", "a3"])
        self.assertEqual(doc.waves, [["a1", "a3"]])
        self.assertNotIn("a2", out.getvalue())

    def test_only_unknown_agent_errors(self):
        with isolated_tmp() as tmp:
            recipe = self._recipe(tmp)
            with self.assertRaises(ValueError) as cm:
                plan.bake_plan(recipe, only_agents=["nope"])
        self.assertIn("unknown agents in recipe: nope", str(cm.exception))

    def test_text_hides_env_values(self):
        with isolated_tmp() as tmp:
            recipe = self._recipe(tmp)
            with captured() as out:
                plan.bake_plan(recipe)
        text = out.getvalue()
        self.assertIn("TEAM", text)
        self.assertNotIn("dogs", text)  # value never shown
        self.assertIn("nothing was spawned", text)

    def test_json_is_parseable_and_masks_env(self):
        with isolated_tmp() as tmp:
            recipe = self._recipe(tmp)
            with captured() as out:
                plan.bake_plan(recipe, fmt="json")
        doc = json.loads(out.getvalue())
        self.assertFalse(doc["spawned"])
        self.assertEqual(doc["backend"], "shell")
        self.assertEqual(doc["waves"], [["a1", "a2"], ["a3"]])
        a1 = next(a for a in doc["agents"] if a["name"] == "a1")
        self.assertEqual(a1["env_keys"], ["TEAM"])
        self.assertNotIn("dogs", out.getvalue())

    def test_task_stub_mode(self):
        with isolated_tmp() as tmp:
            with captured() as out:
                doc = plan.bake_plan(
                    task="audit the org", agent_names=["kite", "claude"], fmt="text"
                )
        self.assertEqual(doc.backend, "fixture")
        self.assertEqual(doc.waves, [["kite", "claude"]])
        self.assertIn("(fixture report)", out.getvalue())

    def test_recipe_and_task_both_errors(self):
        with isolated_tmp() as tmp:
            recipe = self._recipe(tmp)
            with self.assertRaises(ValueError):
                plan.bake_plan(recipe, task="x", agent_names=["y"])

    def test_bad_recipe_raises_cleanly(self):
        with isolated_tmp() as tmp:
            bad = tmp / "bad.toml"
            bad.write_text('[bakery]\nname = "bad"\n[[agents]]\nname = 42\n')
            with self.assertRaises(ValueError) as cm:
                plan.bake_plan(str(bad))
        msg = str(cm.exception)
        self.assertIn("bad.toml", msg)   # names the file
        self.assertIn("agents[0]", msg)  # names the agent index

    def test_secret_env_rejected_at_plan_time(self):
        with isolated_tmp() as tmp:
            bad = tmp / "bad2.toml"
            bad.write_text(
                '[bakery]\nname = "s"\n[[agents]]\nname = "a1"\n'
                'cmd = ["echo", "x"]\nenv = { SECRET_KEY = "hunter2" }\n'
            )
            with self.assertRaises(ValueError) as cm:
                plan.bake_plan(str(bad))
        self.assertIn("SECRET_KEY", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
