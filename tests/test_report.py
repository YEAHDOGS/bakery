"""Tests for `bake report` (bakery/report.py)."""

import os
import tempfile
import unittest
from pathlib import Path

from bakery import merge, report
from bakery.recipe import load_recipe


class EmitTomlTest(unittest.TestCase):
    def test_round_trip(self):
        r = report.stub_recipe_from_task("demo task", ["kite", "claude"])
        text = report.emit_recipe_toml(r)
        tmp = Path(tempfile.mkdtemp()) / "r.toml"
        tmp.write_text(text)
        back = load_recipe(str(tmp))
        self.assertEqual(back.name, r.name)
        self.assertEqual(back.backend, "fixture")
        self.assertEqual([a.name for a in back.agents], ["kite", "claude"])
        self.assertIn("demo task", back.agents[0].report)

    def test_stub_reports_labeled_stub(self):
        r = report.stub_recipe_from_task("t", ["gemini"])
        self.assertIn("fixture stub", r.agents[0].report)


class MergeNamesTest(unittest.TestCase):
    def test_names_override_labels(self):
        tmp = Path(tempfile.mkdtemp())
        p1, p2 = tmp / "a.md", tmp / "b.md"
        p1.write_text("# one")
        p2.write_text("# two")
        doc = merge.merge_reports([str(p1), str(p2)], names=["kite", "claude"])
        self.assertIn("## Report: `kite`", doc)
        self.assertIn("## Report: `claude`", doc)
        self.assertNotIn(str(p1), doc)


class BakeReportEndToEndTest(unittest.TestCase):
    """Full pipeline: fan out fixture agents, wait, merge to one doc."""

    def test_end_to_end(self):
        tmp = Path(tempfile.mkdtemp())
        recipe_p = tmp / "r.toml"
        recipe_p.write_text(
            '[bakery]\nname = "e2e"\nbackend = "fixture"\nmax_parallel = 2\n'
            '[[agents]]\nname = "kite"\nreport = "# kite findings\\nship it"\n'
            '[[agents]]\nname = "claude"\nreport = "# claude findings\\nlooks good"\n'
            '[[agents]]\nname = "gemini"\nreport = "# gemini findings\\nagree"\n'
        )
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            out = tmp / "merged.md"
            got = report.bake_report(str(recipe_p), only_agents=["kite", "claude"], output=str(out))
        finally:
            os.chdir(cwd)
        self.assertEqual(got, str(out))
        doc = out.read_text()
        self.assertIn("## Report: `kite`", doc)
        self.assertIn("## Report: `claude`", doc)
        self.assertIn("ship it", doc)
        self.assertNotIn("gemini", doc)

    def test_task_mode(self):
        tmp = Path(tempfile.mkdtemp())
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            out = report.bake_report(None, task="say hi", agent_names=["kite"], output=str(tmp / "r.md"))
        finally:
            os.chdir(cwd)
        doc = Path(out).read_text()
        self.assertIn("fixture stub", doc)
        self.assertIn("say hi", doc)


if __name__ == "__main__":
    unittest.main()
