"""Regression tests for merge-mode `bake report <run-id>` (bakery.report.merge_run).

A merged report reads a completed run's per-agent report files and produces
one markdown document: deduped corroborated findings, conflicts flagged with
agent attribution, and the full per-agent detail preserved. Agent output is
UNTRUSTED, so the same sanitize-first discipline as `render_report` applies.

Fixtures are synthetic run dirs built directly on disk (no shell agents),
covering: agreeing reports, conflicting reports, a missing report file, an
unfinished agent, and sanitization of hostile content.
"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from bakery import runner
from bakery.report import extract_findings, merge_run


def _agent_state(state="done", exit_code=0, duration_s=1.0):
    return {
        "pid": None,
        "pgid": None,
        "state": state,
        "exit_code": exit_code,
        "started_at": "2026-09-09T00:00:00+00:00",
        "finished_at": "2026-09-09T00:00:01+00:00",
        "duration_s": duration_s,
    }


def _fixture_run(run_id, outs: dict[str, str | None], states: dict[str, dict] | None = None):
    """Create .bakery/runs/<run_id> with meta.json and agent .out files.

    A None out value means the agent has NO report file (missing-file case).
    """
    run_dir = runner.runs_root() / run_id
    (run_dir / "agents").mkdir(parents=True)
    meta = {
        "run_id": run_id,
        "recipe": "merge-test",
        "status": "done",
        "started_at": "2026-09-09T00:00:00+00:00",
        "finished_at": "2026-09-09T00:00:01+00:00",
        "agents": {name: (states[name] if states and name in states else _agent_state())
                   for name in outs},
    }
    (run_dir / "meta.json").write_text(json.dumps(meta))
    for name, out in outs.items():
        if out is not None:
            (run_dir / "agents" / f"{name}.out").write_text(out)
    return run_id


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.tmp.name)

    def test_agreeing_reports_dedupe_with_attribution(self):
        _fixture_run("agree-1", {
            "kite": "- deploy to staging is safe\n- the build passes\n",
            "claude": "- Deploy to staging is safe.\n- the build passes\n",
            "gemini": "- unique: only gemini noticed the flaky test\n",
        })
        report = merge_run("agree-1")
        summary, detail = report.split("## 📄 Per-agent detail")
        # identical claim from two agents appears once in the summary, both attributed
        self.assertEqual(summary.lower().count("deploy to staging is safe"), 1)
        self.assertIn("agreed: claude, kite", summary)
        self.assertEqual(summary.lower().count("the build passes"), 1)
        # single-agent claim goes to the unique section, attributed
        self.assertIn("Unique findings", report)
        self.assertIn("gemini", report)
        # per-agent detail still preserves every agent's full output
        self.assertIn("### kite — done", report)
        self.assertIn("### claude — done", report)
        self.assertIn("### gemini — done", report)

    def test_conflicting_reports_flagged_with_attribution(self):
        _fixture_run("conflict-1", {
            "kite": "- the build passes\n",
            "claude": "- the build does not pass\n",
        })
        report = merge_run("conflict-1")
        self.assertIn("Conflicts", report)
        self.assertIn("the build passes", report)
        self.assertIn("the build does not pass", report)
        self.assertIn("says: kite", report)
        self.assertIn("says: claude", report)
        # conflicting claims are not also listed as corroborated
        self.assertNotIn("Corroborated", report)

    def test_missing_report_file_is_noted_not_dropped(self):
        _fixture_run("missing-1", {
            "kite": "- one real finding\n",
            "ghost": None,  # meta lists the agent; no .out file exists
        })
        report = merge_run("missing-1")
        self.assertIn("### ghost — done", report)
        self.assertIn("no output captured", report)
        self.assertIn("one real finding", report)

    def test_unfinished_agent_gets_partial_warning(self):
        _fixture_run(
            "partial-1",
            {"kite": "- finding so far\n", "slow": "- finding so far\n"},
            states={"slow": _agent_state(state="running", exit_code=None, duration_s=None)},
        )
        report = merge_run("partial-1")
        self.assertIn("not fully complete", report)
        self.assertIn("slow", report)
        self.assertIn("may be partial", report)

    def test_hostile_content_is_sanitized(self):
        _fixture_run("hostile-1", {
            "inj": "```\n## INJECTED HEADER\n```\nleak ghp_" + "z" * 30 + "\n",
        })
        report = merge_run("hostile-1")
        # agent's fence-break is escaped: no unescaped ``` outside our own fences
        unescaped = re.findall(r"(?<!\\)```", report)
        self.assertEqual(len(unescaped), 2)  # our own one fenced detail block
        self.assertIn("## INJECTED HEADER", report)  # present but inert inside fence
        self.assertIn("[redacted:github-token]", report)
        self.assertNotIn("ghp_", report)

    def test_run_with_no_findings_still_merges(self):
        _fixture_run("empty-1", {"kite": "just prose, no bullets or verdicts\n"})
        report = merge_run("empty-1")
        self.assertIn("no findings extracted", report)
        self.assertIn("just prose, no bullets or verdicts", report)

    def test_extract_findings_shapes(self):
        text = (
            "# a heading is not a finding\n"
            "- bullet claim\n"
            "2) numbered claim\n"
            "verdict: ship it\n"
            "plain prose paragraph is not a finding\n"
        )
        self.assertEqual(extract_findings(text), ["bullet claim", "numbered claim", "ship it"])

    def test_unknown_run_errors(self):
        with self.assertRaises(SystemExit):
            merge_run("nope-not-a-run")


class CmdReportDispatchTest(unittest.TestCase):
    """`bake report` accepts a recipe path (fan out) or a run id (merge)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.tmp.name)

    def test_report_run_id_merges(self):
        from bakery.__main__ import main

        _fixture_run("cli-1", {"kite": "- cli merge works\n"})
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["report", "cli-1"])
        out = buf.getvalue()
        self.assertIn("Merged report", out)
        self.assertIn("cli merge works", out)

    def test_report_neither_file_nor_run_errors(self):
        from bakery.__main__ import main

        with self.assertRaises(SystemExit):
            main(["report", "definitely-not-a-recipe-or-run"])


if __name__ == "__main__":
    unittest.main()
