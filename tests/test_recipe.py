"""Regression tests for recipe loading (bakery.recipe).

Master had no test coverage at all. These pin the existing contract so
future changes can't silently alter validation, defaults, or env coercion.
"""

import tempfile
import unittest
from pathlib import Path

from bakery.recipe import load_recipe


def _write(tmpdir, body):
    p = Path(tmpdir) / "r.toml"
    p.write_text(body)
    return str(p)


AGENT = """\
[[agents]]
name = "{name}"
cmd = ["bash", "-lc", "echo hi"]
"""

HEAD = """\
[bakery]
name = "regress"
backend = "shell"
{extra}
"""


class RecipeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_defaults(self):
        path = _write(self.tmp.name, HEAD.format(extra="") + AGENT.format(name="a"))
        r = load_recipe(path)
        self.assertEqual(r.name, "regress")
        self.assertEqual(r.backend, "shell")
        self.assertEqual(r.max_parallel, 1)  # defaults to agent count
        a = r.agents[0]
        self.assertEqual(a.timeout, 3600)
        self.assertEqual(a.workdir, ".")
        self.assertEqual(a.env, {})

    def test_explicit_max_parallel_and_timeout(self):
        path = _write(
            self.tmp.name,
            HEAD.format(extra="max_parallel = 2")
            + AGENT.format(name="a")
            + AGENT.format(name="b").replace('cmd = ["bash", "-lc", "echo hi"]', 'cmd = ["bash", "-lc", "echo hi"]\ntimeout = 42'),
        )
        r = load_recipe(path)
        self.assertEqual(r.max_parallel, 2)
        self.assertEqual([a.timeout for a in r.agents], [3600, 42])

    def test_env_passthrough(self):
        path = _write(
            self.tmp.name,
            HEAD.format(extra="")
            + AGENT.format(name="a").replace('timeout = 30', '').rstrip()
            + '\nenv = { ORG = "YEAHDOGS", N = 3 }\n',
        )
        r = load_recipe(path)
        self.assertEqual(r.agents[0].env, {"ORG": "YEAHDOGS", "N": 3})

    def test_duplicate_agent_name_rejected(self):
        path = _write(self.tmp.name, HEAD.format(extra="") + AGENT.format(name="a") + AGENT.format(name="a"))
        with self.assertRaises(ValueError):
            load_recipe(path)

    def test_missing_name_rejected(self):
        path = _write(self.tmp.name, HEAD.format(extra="") + '[[agents]]\ncmd = ["bash", "-lc", "echo hi"]\n')
        with self.assertRaises(ValueError):
            load_recipe(path)

    def test_shell_agent_without_cmd_rejected(self):
        path = _write(self.tmp.name, HEAD.format(extra="") + '[[agents]]\nname = "a"\n')
        with self.assertRaises(ValueError):
            load_recipe(path)

    def test_no_agents_rejected(self):
        path = _write(self.tmp.name, HEAD.format(extra=""))
        with self.assertRaises(ValueError):
            load_recipe(path)

    def test_missing_bakery_section_rejected(self):
        path = _write(self.tmp.name, AGENT.format(name="a"))
        with self.assertRaises(ValueError):
            load_recipe(path)

    def test_invalid_toml_rejected(self):
        path = _write(self.tmp.name, "this is [ not valid toml {{{")
        with self.assertRaises(Exception):
            load_recipe(path)


if __name__ == "__main__":
    unittest.main()
