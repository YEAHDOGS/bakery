"""Smoke/regression tests for `bake merge` (bakery.merge)."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bakery import merge


class MergeTest(unittest.TestCase):
    def _report(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_merges_in_order_with_headers(self):
        a = self._report("# Kite\nk1")
        b = self._report("# Claude\nc1")
        doc = merge.merge_reports([a, b])
        self.assertTrue(doc.startswith("# Merged report"))
        self.assertLess(doc.index("k1"), doc.index("c1"))
        self.assertIn(f"## Report: `{a}`", doc)
        self.assertIn(f"## Report: `{b}`", doc)

    def test_empty_report_gets_placeholder(self):
        a = self._report("")
        doc = merge.merge_reports([a])
        self.assertIn("_empty report_", doc)

    def test_sanitize_strips_ansi_and_controls(self):
        a = self._report("\x1b[31mred\x1b[0m\x00\x01text\x07")
        doc = merge.merge_reports([a])
        self.assertNotIn("\x1b", doc)
        self.assertNotIn("\x00", doc)
        self.assertIn("redtext", doc)

    def test_tabs_and_newlines_survive(self):
        a = self._report("col1\tcol2\nline2")
        doc = merge.merge_reports([a])
        self.assertIn("col1\tcol2\nline2", doc)

    def test_missing_file_raises(self):
        with self.assertRaises(SystemExit):
            merge.merge_reports(["/tmp/definitely-not-here-xyz.md"])

    def test_no_files_raises(self):
        with self.assertRaises(SystemExit):
            merge.merge_reports([])

    def test_write_merged_to_file(self):
        a = self._report("hello")
        fd, out = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        self.addCleanup(os.unlink, out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            merge.write_merged([a], out)
        with open(out) as f:
            on_disk = f.read()
        self.assertIn("# Merged report", on_disk)
        self.assertIn("merged 1 report(s)", buf.getvalue())


class StatusesTest(unittest.TestCase):
    """Per-agent outcome annotation: a dead agent's partial output is flagged."""

    def _report(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_done_status_annotated(self):
        a = self._report("findings")
        doc = merge.merge_reports(
            [a], names=["kite"],
            statuses={"kite": {"state": "done", "exit_code": 0, "duration_s": 12.3}},
        )
        self.assertIn("## Report: `kite`", doc)
        self.assertIn("**done** (exit 0, 12.3s)", doc)
        self.assertNotIn("PARTIAL", doc)
        self.assertIn("**Outcome:** 1/1 agents finished cleanly.", doc)

    def test_timeout_status_flagged_partial(self):
        a = self._report("half a report")
        doc = merge.merge_reports(
            [a], names=["kite"],
            statuses={"kite": {"state": "timeout", "exit_code": -1, "duration_s": 60.0}},
        )
        self.assertIn("**TIMEOUT** (exit -1, 60.0s)", doc)
        self.assertIn("**PARTIAL**", doc)
        self.assertIn("1 did not: `kite`", doc)

    def test_killed_and_unknown_flagged(self):
        a = self._report("x")
        b = self._report("y")
        doc = merge.merge_reports(
            [a, b], names=["a1", "a2"],
            statuses={
                "a1": {"state": "killed", "exit_code": None, "duration_s": None},
                "a2": {"state": "unknown", "exit_code": None, "duration_s": None},
            },
        )
        self.assertIn("**KILLED**", doc)
        self.assertIn("**UNKNOWN**", doc)
        self.assertEqual(doc.count("**PARTIAL**"), 2)

    def test_status_text_sanitized(self):
        a = self._report("ok")
        doc = merge.merge_reports(
            [a], names=["kite"],
            statuses={"kite": {"state": "done\x1b[31m", "exit_code": 0, "duration_s": 1}},
        )
        self.assertNotIn("\x1b", doc)

    def test_no_statuses_preserves_old_output(self):
        a = self._report("plain")
        doc = merge.merge_reports([a], names=["kite"])
        self.assertIn("## Report: `kite`", doc)
        self.assertNotIn("outcome:", doc)
        self.assertNotIn("**Outcome:**", doc)


if __name__ == "__main__":
    unittest.main()
