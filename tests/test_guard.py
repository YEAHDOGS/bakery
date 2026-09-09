"""Regression tests for the pre-flight secret guard (bakery.guard).

The guard enforces VISION.md's non-negotiable rule: a worker never gets the
founder's credentials. `runner.start_run` refuses a recipe that hands a
secret-looking value to an agent, before any agent spawns.
"""

import tempfile
import unittest
from pathlib import Path

from bakery.guard import find_secrets, guard_recipe
from bakery.recipe import load_recipe
from bakery import runner


def _recipe_toml(env=None, cmd_extra=""):
    lines = [
        '[bakery]',
        'name = "guard-test"',
        'backend = "shell"',
        'max_parallel = 1',
        '',
        '[[agents]]',
        'name = "worker"',
        f'cmd = ["bash", "-lc", "echo ok{cmd_extra}"]',
        'timeout = 30',
    ]
    if env:
        lines.append("env = { " + ", ".join(f'{k} = "{v}"' for k, v in env.items()) + " }")
    return "\n".join(lines) + "\n"


def _load(tmpdir, **kwargs):
    p = Path(tmpdir) / "r.toml"
    p.write_text(_recipe_toml(**kwargs))
    return load_recipe(str(p))


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_clean_recipe_passes(self):
        recipe = _load(self.tmp.name, env={"ORG": "YEAHDOGS", "MODE": "fast"})
        self.assertEqual(find_secrets(recipe), [])
        guard_recipe(recipe)  # must not raise

    def test_github_token_in_env_refused(self):
        recipe = _load(self.tmp.name, env={"SOME_VAR": "ghp_" + "a" * 30})
        self.assertTrue(find_secrets(recipe))
        with self.assertRaises(SystemExit):
            guard_recipe(recipe)

    def test_aws_key_in_env_refused(self):
        recipe = _load(self.tmp.name, env={"KEY": "AKIA" + "B" * 16})
        with self.assertRaises(SystemExit):
            guard_recipe(recipe)

    def test_pem_block_in_env_refused(self):
        recipe = _load(self.tmp.name, env={"CERT": "-----BEGIN RSA PRIVATE KEY-----\\nfake\\n-----END RSA PRIVATE KEY-----"})
        with self.assertRaises(SystemExit):
            guard_recipe(recipe)

    def test_openai_key_in_cmd_refused(self):
        recipe = _load(self.tmp.name, cmd_extra=" sk-" + "z" * 40)
        with self.assertRaises(SystemExit):
            guard_recipe(recipe)

    def test_secret_bearing_env_name_with_real_value_refused(self):
        recipe = _load(self.tmp.name, env={"API_KEY": "s3cr3t-value-01234"})
        with self.assertRaises(SystemExit):
            guard_recipe(recipe)

    def test_placeholders_pass(self):
        for val in ("changeme", "example", "${API_KEY}", "test", "xxx"):
            recipe = _load(self.tmp.name, env={"API_KEY": val})
            self.assertEqual(find_secrets(recipe), [], f"placeholder {val!r} tripped the guard")

    def test_short_values_pass(self):
        recipe = _load(self.tmp.name, env={"PASSWORD": "hunter2"})
        self.assertEqual(find_secrets(recipe), [])

    def test_nonsecret_env_name_with_tokenish_value_passes(self):
        # name-based rule only fires on secret-bearing names; a plain long
        # value without a token shape is not a secret
        recipe = _load(self.tmp.name, env={"BUILD_TAG": "release-candidate-2026-09-09"})
        self.assertEqual(find_secrets(recipe), [])

    def test_finding_names_agent_and_source(self):
        recipe = _load(self.tmp.name, env={"TOKEN": "ghp_" + "b" * 30})
        (f,) = find_secrets(recipe)
        self.assertEqual(f.agent, "worker")
        self.assertEqual(f.source, "env:TOKEN")

    def test_start_run_refuses_before_creating_run_dir(self):
        p = Path(self.tmp.name) / "bad.toml"
        p.write_text(_recipe_toml(env={"SECRET": "ghp_" + "c" * 30}))
        cwd = Path.cwd()
        self.addCleanup(lambda: __import__("os").chdir(cwd))
        import os
        os.chdir(self.tmp.name)
        with self.assertRaises(SystemExit):
            runner.start_run(str(p))
        self.assertFalse((Path(".bakery") / "runs").exists(), "refused run must not create a run dir")


if __name__ == "__main__":
    unittest.main()
