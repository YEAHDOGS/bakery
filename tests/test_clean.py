"""Tests for `bake clean`: prune old run directories, keep the newest N.

Runs entirely against fabricated .bakery/runs dirs in a temp cwd — no
supervisor spawns, no network, no fixtures needed.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from bakery import runner


def _mk_run(root: Path, name: str, status: str, started_at: str) -> Path:
    d = root / name
    (d / "agents").mkdir(parents=True)
    meta = {
        "run_id": name,
        "recipe": "r",
        "status": status,
        "started_at": started_at,
        "finished_at": started_at if status in ("done", "killed") else None,
        "agents": {},
    }
    (d / "meta.json").write_text(json.dumps(meta))
    return d


class CleanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.old)
        self.runs = self.tmp / ".bakery" / "runs"

    def _runs_exist(self):
        return sorted(p.name for p in self.runs.iterdir())

    def test_keeps_newest(self):
        for i in range(5):
            _mk_run(self.runs, f"run-{i}", "done", f"2026-01-0{i+1}T00:00:00+00:00")
        removed = runner.clean(keep=2)
        self.assertEqual(sorted(removed), ["run-0", "run-1", "run-2"])
        self.assertEqual(self._runs_exist(), ["run-3", "run-4"])

    def test_default_keep_ten(self):
        for i in range(12):
            _mk_run(self.runs, f"run-{i:02d}", "done", f"2026-01-{i+1:02d}T00:00:00+00:00")
        runner.clean()
        self.assertEqual(len(self._runs_exist()), 10)
        self.assertNotIn("run-00", self._runs_exist())
        self.assertIn("run-11", self._runs_exist())

    def test_skips_live_runs(self):
        _mk_run(self.runs, "old-done", "done", "2026-01-01T00:00:00+00:00")
        _mk_run(self.runs, "old-running", "running", "2026-01-02T00:00:00+00:00")
        _mk_run(self.runs, "old-starting", "starting", "2026-01-03T00:00:00+00:00")
        _mk_run(self.runs, "new-done", "done", "2026-01-04T00:00:00+00:00")
        # live run is OLD — clean must refuse to delete it even when over budget
        removed = runner.clean(keep=2)
        self.assertNotIn("old-running", removed)
        self.assertTrue((self.runs / "old-running").exists())
        self.assertNotIn("old-starting", removed)
        # newest kept + live skipped, oldest done pruned
        self.assertEqual(sorted(removed), ["old-done"])

    def test_ignores_non_run_dirs_and_files(self):
        stray = self.runs / "not-a-run"
        stray.mkdir(parents=True)
        (stray / "junk.txt").write_text("leave me alone")
        _mk_run(self.runs, "run-a", "done", "2026-01-01T00:00:00+00:00")
        removed = runner.clean(keep=0)
        self.assertEqual(removed, ["run-a"])
        self.assertTrue((stray / "junk.txt").exists())

    def test_no_runs_yet(self):
        self.assertEqual(runner.clean(keep=5), [])


if __name__ == "__main__":
    unittest.main()
