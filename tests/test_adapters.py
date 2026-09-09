"""Tests for the agent adapter contract (bakery/adapters.py)."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from bakery import adapters
from bakery.adapters import Adapter, ShellAdapter, get, register
from bakery.recipe import AgentSpec, load_recipe


class RegistryTest(unittest.TestCase):
    def test_shell_registered(self):
        self.assertIsInstance(get("shell"), ShellAdapter)

    def test_unknown_backend_names_valid_ones(self):
        with self.assertRaises(ValueError) as ctx:
            get("telepathy")
        self.assertIn("shell", str(ctx.exception))
        self.assertIn("telepathy", str(ctx.exception))

    def test_register_custom_adapter(self):
        class NoopAdapter(Adapter):
            name = "noop-test"

            def spawn(self, spec, *, stdout, stderr, env):
                raise NotImplementedError

        register(NoopAdapter())
        self.assertIsInstance(get("noop-test"), NoopAdapter)

    def test_register_rejects_unnamed(self):
        with self.assertRaises(ValueError):
            register(Adapter())


class ShellAdapterTest(unittest.TestCase):
    def _agent(self, cmd):
        return AgentSpec(name="a1", cmd=cmd, timeout=30, workdir=".", env={"FOO": "bar"})

    def test_validate_rejects_missing_cmd(self):
        with self.assertRaises(ValueError):
            ShellAdapter().validate(AgentSpec(name="a1", cmd=[]))

    def test_spawn_runs_and_captures(self):
        tmp = Path(tempfile.mkdtemp())
        out_p, err_p = tmp / "a.out", tmp / "a.err"
        with open(out_p, "w") as out, open(err_p, "w") as err:
            proc = ShellAdapter().spawn(
                self._agent(["bash", "-lc", "echo hello-$FOO; echo oops >&2"]),
                stdout=out,
                stderr=err,
                env={"FOO": "bar"},
            )
            proc.wait()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("hello-bar", out_p.read_text())
        self.assertIn("oops", err_p.read_text())
        self.assertIsInstance(proc, subprocess.Popen)


class RecipeValidationTest(unittest.TestCase):
    def _recipe(self, backend, extra=""):
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "r.toml"
        p.write_text(
            f'[bakery]\nname = "t"\nbackend = "{backend}"\n'
            f'[[agents]]\nname = "a1"\n{extra}\n'
        )
        return str(p)

    def test_unknown_backend_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            load_recipe(self._recipe("telepathy"))
        self.assertIn("telepathy", str(ctx.exception))

    def test_shell_requires_cmd(self):
        with self.assertRaises(ValueError) as ctx:
            load_recipe(self._recipe("shell", 'timeout = 5'))
        self.assertIn("cmd", str(ctx.exception))

    def test_shell_valid_recipe_loads(self):
        r = load_recipe(self._recipe("shell", 'cmd = ["bash", "-lc", "true"]'))
        self.assertEqual(r.backend, "shell")
        self.assertEqual(len(r.agents), 1)


if __name__ == "__main__":
    unittest.main()
