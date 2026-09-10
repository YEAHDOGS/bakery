"""Regression tests for agent sandboxing (bakery.sandbox).

VISION.md's non-negotiable rule: agents run with least privilege — a worker
never gets the founder's credentials. The shell backend used to hand every
agent `os.environ.copy()`; `sandboxed_env` now builds a minimal environment
instead (allowlisted operational vars + the guard-vetted recipe env, and
nothing else).
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from bakery.sandbox import SAFE_ENV_ALLOWLIST, sandboxed_env
from bakery import runner


class SandboxUnitTest(unittest.TestCase):
    def test_parent_secrets_are_dropped(self):
        parent = {
            "PATH": "/usr/bin",
            "GITHUB_TOKEN": "ghp_" + "a" * 30,
            "ANTHROPIC_API_KEY": "sk-ant-" + "b" * 40,
            "SOME_EXPORTED_THING": "mystuff",
            "AWS_SECRET_ACCESS_KEY": "c" * 40,
        }
        env = sandboxed_env(parent, {})
        self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_allowlisted_vars_pass_through(self):
        parent = {"PATH": "/usr/bin", "HOME": "/root", "LANG": "C.UTF-8", "TZ": "UTC"}
        env = sandboxed_env(parent, {})
        self.assertEqual(env, parent)

    def test_secret_shaped_value_under_allowlisted_name_is_dropped(self):
        parent = {"PATH": "/usr/bin", "HOME": "ghp_" + "x" * 30}
        env = sandboxed_env(parent, {})
        self.assertEqual(env, {"PATH": "/usr/bin"})

    def test_recipe_env_passes_and_wins_on_conflict(self):
        env = sandboxed_env({"PATH": "/usr/bin"}, {"ORG": "YEAHDOGS", "PATH": "/opt/bin"})
        self.assertEqual(env["ORG"], "YEAHDOGS")
        self.assertEqual(env["PATH"], "/opt/bin")

    def test_recipe_env_values_coerced_to_str(self):
        env = sandboxed_env({}, {"N": 3, "FLAG": True})
        self.assertEqual(env, {"N": "3", "FLAG": "True"})

    def test_allowlist_is_tight(self):
        # allowlist must be operational vars only — no credential-ish names
        banned = ("token", "secret", "key", "password", "credential", "auth")
        for name in SAFE_ENV_ALLOWLIST:
            self.assertFalse(
                any(b in name.lower() for b in banned),
                f"allowlisted {name!r} looks credential-bearing",
            )


class SandboxLaunchTest(unittest.TestCase):
    """End-to-end: a real agent must not see the supervisor's secrets."""

    FAKE_TOKEN = "ghp_" + "faketoken-for-sandbox-test-" + "z" * 10

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
        os.environ["GITHUB_TOKEN"] = self.FAKE_TOKEN
        os.environ["SOME_LEAKY_EXPORT"] = "should-not-reach-agent"
        # The detached supervisor re-execs `python -m bakery` with this CWD;
        # it needs the package importable from here (real installs have it).
        repo_root = str(Path(__file__).resolve().parent.parent)
        old_pp = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = repo_root + (os.pathsep + old_pp if old_pp else "")

    def _wait_for_agent(self, run_dir: Path, name: str, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (run_dir / "agents" / f"{name}.code").exists():
                return
            time.sleep(0.2)
        self.fail(f"agent {name} did not finish within {timeout}s")

    def test_agent_cannot_see_supervisor_secrets(self):
        recipe = Path(self.tmp.name) / "r.toml"
        recipe.write_text(
            '[bakery]\nname = "sandbox-e2e"\nbackend = "shell"\nmax_parallel = 1\n\n'
            '[[agents]]\nname = "prober"\ncmd = ["bash", "-lc", "env | sort"]\ntimeout = 30\n'
            'env = { RECIPE_VAR = "from-recipe" }\n'
        )
        run_id = "sandbox-e2e-1"
        runner.start_run(str(recipe), run_id)
        self._wait_for_agent(Path(".bakery") / "runs" / run_id, "prober")
        out = (Path(".bakery") / "runs" / run_id / "agents" / "prober.out").read_text()
        leaked = [line for line in out.splitlines() if self.FAKE_TOKEN in line or "SOME_LEAKY_EXPORT" in line]
        self.assertEqual(leaked, [], f"agent saw supervisor secrets: {leaked}")
        self.assertIn("RECIPE_VAR=from-recipe", out.splitlines())
        # operational vars still present so agents can actually run
        self.assertTrue(any(line.startswith("PATH=") for line in out.splitlines()), "agent lost PATH")


if __name__ == "__main__":
    unittest.main()
