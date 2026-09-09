"""Regression tests for `bake init` (bakery.scaffold).

Pin the contract: fresh init creates the expected files, a second init never
clobbers user edits, and --dry-run writes nothing.
"""

import tempfile
import tomllib
import unittest
from pathlib import Path

from bakery import scaffold


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _init(self, **kw):
        return scaffold.init_project(self.root, **kw)

    def test_fresh_init_creates_expected_files(self):
        res = self._init()
        self.assertFalse(res.dry_run)
        self.assertEqual(
            sorted(res.created),
            sorted(scaffold.FILES),
            "fresh init should create every scaffolded file",
        )
        self.assertEqual(res.skipped, ())

        config = tomllib.loads((self.root / "bakery.toml").read_text())
        self.assertEqual(config["bakery"]["merge_strategy"], "concat")
        self.assertIn("shell", config["backends"])
        self.assertIn("claude-cli", config["backends"])
        self.assertIn("gemini-cli", config["backends"])

        task = tomllib.loads(
            (self.root / ".bakery" / "tasks" / "hello.toml").read_text()
        )
        self.assertEqual(len(task["agents"]), 3)
        self.assertTrue(all("cmd" in a for a in task["agents"]))

        notes = (self.root / ".bakery" / "notes" / "agents-skills-eval.md").read_text()
        self.assertTrue(notes.strip(), "eval notes file must be non-empty")

    def test_second_init_does_not_clobber(self):
        self._init()
        config_path = self.root / "bakery.toml"
        edited = config_path.read_text().replace('merge_strategy = "concat"',
                                                  'merge_strategy = "best-of"')
        config_path.write_text(edited)  # user edit

        res = self._init()
        self.assertEqual(res.created, ())
        self.assertEqual(sorted(res.skipped), sorted(scaffold.FILES))
        # user edit survived byte-for-byte
        self.assertEqual(config_path.read_text(), edited)

    def test_dry_run_writes_nothing(self):
        res = self._init(dry_run=True)
        self.assertTrue(res.dry_run)
        self.assertEqual(sorted(res.created), sorted(scaffold.FILES))
        self.assertEqual(res.skipped, ())
        # nothing on disk — not even the .bakery dir
        self.assertEqual(list(self.root.iterdir()), [])

    def test_partial_init_fills_missing_only(self):
        # pre-existing config must survive; missing files get created
        config_path = self.root / "bakery.toml"
        config_path.write_text("# mine, do not touch\n")
        res = self._init()
        self.assertEqual(config_path.read_text(), "# mine, do not touch\n")
        self.assertIn("bakery.toml", res.skipped)
        self.assertIn(".bakery/tasks/hello.toml", res.created)
        self.assertIn(".bakery/notes/agents-skills-eval.md", res.created)


if __name__ == "__main__":
    unittest.main()
