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


if __name__ == "__main__":
    unittest.main()
