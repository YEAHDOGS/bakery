"""Tests for `--format json` on `bake status` and `bake collect`.

Covers the machine-readable renderings against fabricated run dirs (fast,
no subprocesses) plus one end-to-end fixture-backend run, and guards that
the existing text/markdown defaults are unchanged (regression).
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from bakery import __main__ as cli
from bakery import report, runner


def _write_meta(run_dir: Path, run_id: str, status: str = "done") -> dict:
    meta = {
        "run_id": run_id,
        "recipe": "json-test",
        "status": status,
        "started_at": "2026-09-09T00:00:00",
        "finished_at": "2026-09-09T00:01:00",
        "agents": {
            "a": {
                "pid": 999, "pgid": 999, "state": "done", "exit_code": 0,
                "started_at": "2026-09-09T00:00:00",
                "finished_at": "2026-09-09T00:00:30", "duration_s": 30.0,
                "attempts": 1,
            },
            "b": {
                "pid": None, "pgid": None, "state": "timeout", "exit_code": -9,
                "started_at": "2026-09-09T00:00:00",
                "finished_at": "2026-09-09T00:01:00", "duration_s": 60.0,
                "attempts": 1,
            },
        },
    }
    (run_dir / "meta.json").write_text(json.dumps(meta))
    return meta


class JsonOutputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        (self.tmp / ".bakery" / "runs").mkdir(parents=True)

    def tearDown(self):
        os.chdir(self.cwd)

    def _run_dir(self, run_id: str) -> Path:
        d = self.tmp / ".bakery" / "runs" / run_id
        (d / "agents").mkdir(parents=True, exist_ok=True)
        return d

    def test_status_json_parses_with_whitelisted_fields(self):
        self._run_dir("j1")
        _write_meta(self.tmp / ".bakery" / "runs" / "j1", "j1")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.status("j1", "json")
        doc = json.loads(buf.getvalue())  # must be valid JSON
        self.assertEqual(doc["run_id"], "j1")
        self.assertEqual(doc["recipe"], "json-test")
        self.assertEqual(doc["status"], "done")
        self.assertEqual(doc["agents"]["a"]["state"], "done")
        self.assertEqual(doc["agents"]["a"]["exit_code"], 0)
        self.assertAlmostEqual(doc["agents"]["a"]["duration_s"], 30.0)
        self.assertEqual(doc["agents"]["b"]["state"], "timeout")
        # Internal bookkeeping stays internal.
        for name, fields in doc["agents"].items():
            self.assertNotIn("pid", fields, name)
            self.assertNotIn("pgid", fields, name)
            self.assertNotIn("attempts", fields, name)

    def test_status_text_default_unchanged(self):
        """Default rendering keeps the human-readable table (regression)."""
        self._run_dir("j2")
        _write_meta(self.tmp / ".bakery" / "runs" / "j2", "j2")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.status("j2")
        out = buf.getvalue()
        self.assertIn("run j2", out)
        self.assertIn("agent", out)
        # Not JSON: json.loads must fail on the table.
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)

    def test_collect_json_includes_agent_outputs(self):
        run_dir = self._run_dir("j3")
        _write_meta(run_dir, "j3")
        (run_dir / "agents" / "a.out").write_text("# Agent A report\nall good\n")
        (run_dir / "agents" / "a.err").write_text("warn: slow\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.collect("j3", "json")
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["status"], "done")
        self.assertIn("Agent A report", doc["agents"]["a"]["stdout"])
        self.assertIn("warn: slow", doc["agents"]["a"]["stderr"])
        # Agent with no output files reads as empty strings.
        self.assertEqual(doc["agents"]["b"]["stdout"], "")
        self.assertEqual(doc["agents"]["b"]["stderr"], "")
        self.assertEqual(doc["agents"]["b"]["state"], "timeout")

    def test_collect_markdown_default_unchanged(self):
        """Markdown default still prints fences (regression)."""
        run_dir = self._run_dir("j4")
        _write_meta(run_dir, "j4")
        (run_dir / "agents" / "a.out").write_text("hello md\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.collect("j4")  # fmt defaults to markdown
        self.assertIn("# Run report", buf.getvalue())
        self.assertIn("```", buf.getvalue())

    def test_collect_json_e2e_fixture_backend(self):
        """Full path: real run -> `bake collect --format json` parses."""
        recipe = (
            "[bakery]\nname = \"json-e2e\"\nbackend = \"fixture\"\nmax_parallel = 2\n"
            "[[agents]]\nname = \"kite\"\nreport = \"# Kite says hi\"\n"
            "[[agents]]\nname = \"claude\"\nreport = \"# Claude says yo\"\n"
        )
        recipe_p = self.tmp / "r.toml"
        recipe_p.write_text(recipe)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["run", str(recipe_p), "--id", "e2e-json"])
        report.wait_for_run("e2e-json", timeout=60)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["collect", "e2e-json", "--format", "json"])
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["status"], "done")
        self.assertIn("Kite says hi", doc["agents"]["kite"]["stdout"])
        self.assertIn("Claude says yo", doc["agents"]["claude"]["stdout"])

    def test_status_json_e2e_cli_flag(self):
        """`bake status --format json` reaches runner.status."""
        run_dir = self._run_dir("j5")
        _write_meta(run_dir, "j5")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["status", "j5", "--format", "json"])
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["run_id"], "j5")


if __name__ == "__main__":
    unittest.main()
