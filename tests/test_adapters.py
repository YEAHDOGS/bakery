"""Tests for the agent adapter contract (bakery/adapters.py)."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from bakery import adapters
from bakery.adapters import Adapter, FixtureAdapter, ShellAdapter, get, register
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


class FixtureAdapterTest(unittest.TestCase):
    def _agent(self, **kw):
        defaults = dict(name="kite", report="# demo report\nall good")
        defaults.update(kw)
        return AgentSpec(name=defaults.pop("name"), cmd=[], **defaults)

    def test_validate_rejects_no_report(self):
        with self.assertRaises(ValueError):
            FixtureAdapter().validate(AgentSpec(name="x", cmd=[]))

    def test_validate_rejects_missing_report_file(self):
        with self.assertRaises(ValueError):
            FixtureAdapter().validate(AgentSpec(name="x", cmd=[], report_file="/nope.md"))

    def test_spawn_plays_canned_report(self):
        from bakery.adapters import FixtureAdapter, get
        self.assertIsInstance(get("fixture"), FixtureAdapter)
        tmp = Path(tempfile.mkdtemp())
        out_p = tmp / "f.out"
        with open(out_p, "w") as out, open(tmp / "f.err", "w") as err:
            proc = FixtureAdapter().spawn(
                self._agent(report="# hello\nline2", delay=0, exit_code=0),
                stdout=out,
                stderr=err,
                env={"PATH": "/usr/bin:/bin"},
            )
            proc.wait()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("# hello", out_p.read_text())
        self.assertIn("line2", out_p.read_text())

    def test_spawn_honors_exit_code(self):
        from bakery.adapters import FixtureAdapter
        tmp = Path(tempfile.mkdtemp())
        out_p = tmp / "f.out"
        with open(out_p, "w") as out, open(tmp / "f.err", "w") as err:
            proc = FixtureAdapter().spawn(
                self._agent(report="boom", exit_code=3),
                stdout=out,
                stderr=err,
                env={"PATH": "/usr/bin:/bin"},
            )
            proc.wait()
        self.assertEqual(proc.returncode, 3)

    def test_recipe_fixture_fields_load(self):
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "r.toml"
        p.write_text(
            '[bakery]\nname = "t"\nbackend = "fixture"\n'
            '[[agents]]\nname = "kite"\nreport = "# r"\ndelay = 2\nexit_code = 1\n'
        )
        r = load_recipe(str(p))
        self.assertEqual(r.backend, "fixture")
        self.assertEqual(r.agents[0].report, "# r")
        self.assertEqual(r.agents[0].delay, 2)
        self.assertEqual(r.agents[0].exit_code, 1)
