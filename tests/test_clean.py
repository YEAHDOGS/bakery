"""Regression tests for `bake clean`.

Roadmap quick win: prune old run directories, keeping the last N finished
runs. The safety invariants are the whole point here — a run dir holds the
only copy of a swarm's logs and reports:

- runs still "running"/"starting" are NEVER deleted (and don't count
  against --keep);
- directories without a readable meta.json are skipped, never touched;
- --dry-run lists what would go without deleting anything.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from bakery import runner
from bakery.__main__ import main


def _meta(run_id, status, started_at):
    return {
        "run_id": run_id,
        "recipe": "t",
        "status": status,
        "started_at": started_at,
        "finished_at": None,
        "retried_from": None,
        "agents": {},
    }


class CleanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.tmp.name)

    def _write_run(self, run_id, status="done", started_at="2026-09-09T15:00:00+00:00"):
        d = runner.runs_root() / run_id
        (d / "agents").mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(_meta(run_id, status, started_at)))
        (d / "agents" / "a.out").write_text("log data")

    def _run_names(self):
        return sorted(d.name for d in runner.runs_root().iterdir() if d.is_dir())

    def test_keeps_newest_prunes_oldest(self):
        for i in range(5):
            self._write_run(f"run-{i}", started_at=f"2026-09-09T1{i}:00:00+00:00")
        result = runner.clean_runs(keep=2)
        self.assertEqual(result["deleted"], ["run-0", "run-1", "run-2"])
        self.assertEqual(self._run_names(), ["run-3", "run-4"])
        self.assertEqual(result["kept"], 2)
        self.assertEqual(result["skipped"], [])

    def test_active_runs_are_never_deleted(self):
        # An old *active* run must survive even a keep=1 sweep, and must not
        # eat the keep quota for finished runs.
        self._write_run("old-active", status="running", started_at="2026-09-01T00:00:00+00:00")
        self._write_run("f-old", started_at="2026-09-08T00:00:00+00:00")
        self._write_run("f-new", started_at="2026-09-09T00:00:00+00:00")
        result = runner.clean_runs(keep=1)
        self.assertEqual(result["deleted"], ["f-old"])
        self.assertEqual(self._run_names(), ["f-new", "old-active"])
        self.assertEqual(result["kept"], 2)

    def test_keep_zero_prunes_all_finished(self):
        self._write_run("f1")
        self._write_run("f2")
        self._write_run("active", status="starting")
        result = runner.clean_runs(keep=0)
        self.assertEqual(result["deleted"], ["f1", "f2"])
        self.assertEqual(self._run_names(), ["active"])

    def test_dry_run_deletes_nothing(self):
        self._write_run("r1")
        self._write_run("r2")
        result = runner.clean_runs(keep=1, dry_run=True)
        self.assertEqual(result["deleted"], ["r1"])
        self.assertEqual(self._run_names(), ["r1", "r2"])

    def test_corrupt_dir_is_skipped_not_deleted(self):
        self._write_run("good")
        corrupt = runner.runs_root() / "junk"
        corrupt.mkdir(parents=True)
        (corrupt / "leftover.txt").write_text("x")
        result = runner.clean_runs(keep=0)
        self.assertEqual(result["deleted"], ["good"])
        self.assertEqual(result["skipped"], ["junk"])
        self.assertTrue((corrupt / "leftover.txt").exists())

    def test_negative_keep_is_rejected(self):
        with self.assertRaises(SystemExit):
            runner.clean_runs(keep=-1)

    def test_no_runs_dir_is_a_noop(self):
        result = runner.clean_runs(keep=10)
        self.assertEqual(result, {"deleted": [], "skipped": [], "kept": 0})

    def test_cli_clean_end_to_end(self):
        self._write_run("r1", started_at="2026-09-08T00:00:00+00:00")
        self._write_run("r2", started_at="2026-09-09T00:00:00+00:00")
        main(["clean", "--keep", "1", "--dry-run"])
        self.assertEqual(self._run_names(), ["r1", "r2"])  # dry run: nothing gone
        main(["clean", "--keep", "1"])
        self.assertEqual(self._run_names(), ["r2"])


if __name__ == "__main__":
    unittest.main()
