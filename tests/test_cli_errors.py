"""Tests for clean CLI recipe/validation errors (no tracebacks).

`bake run` and `bake report` must surface recipe validation failures as one
clean `<cmd>: <message>` line, the same treatment `bake plan` already has.
The messages themselves name the recipe file, the section, and the agent
index — these tests pin the single-line CLI surface.
"""

import os
import tempfile
import unittest
from pathlib import Path

from bakery import __main__ as cli


MISSING_CMD = """\
[bakery]
name = "bad-cli-errors"
backend = "shell"

[[agents]]
name = "no-cmd"
"""

NO_AGENTS = """\
[bakery]
name = "no-agents"
backend = "shell"
"""


class CliErrorSurfacingTest(unittest.TestCase):
    def _recipe(self, text):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, prefix="bakery-cli-err-"
        )
        tmp.write(text)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def _assert_clean(self, argv, prefix):
        with self.assertRaises(SystemExit) as cm:
            cli.main(argv)
        msg = cm.exception.code
        self.assertIsInstance(msg, str)
        self.assertTrue(
            msg.startswith(f"{prefix}: "),
            f"expected one clean '{prefix}: ...' line, got: {msg!r}",
        )
        self.assertNotIn("\n", msg, "error must be a single line")
        return msg

    def test_run_bad_recipe_is_one_line(self):
        path = self._recipe(MISSING_CMD)
        msg = self._assert_clean(["run", path], "run")
        self.assertIn(os.path.basename(path), msg)  # names the file
        self.assertIn("no-cmd", msg)  # names the agent

    def test_run_no_agents_is_one_line(self):
        path = self._recipe(NO_AGENTS)
        msg = self._assert_clean(["run", path], "run")
        self.assertIn("agents", msg)

    def test_run_missing_file_is_one_line(self):
        msg = self._assert_clean(["run", "/tmp/bakery-does-not-exist-xyz.toml"], "run")
        self.assertIn("does-not-exist", msg)

    def test_report_bad_recipe_is_one_line(self):
        path = self._recipe(MISSING_CMD)
        msg = self._assert_clean(["report", path], "report")
        self.assertIn(os.path.basename(path), msg)

    def test_report_no_recipe_no_task_is_one_line(self):
        msg = self._assert_clean(["report"], "report")
        self.assertIn("recipe", msg.lower())

    def test_plan_bad_recipe_still_one_line(self):
        # Regression pin: the pre-existing `plan` treatment must keep working.
        path = self._recipe(MISSING_CMD)
        msg = self._assert_clean(["plan", path], "plan")
        self.assertIn(os.path.basename(path), msg)


if __name__ == "__main__":
    unittest.main()
