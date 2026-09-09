"""Regression tests for the structured report merger (bakery.findings).

Covers the core value prop: dedupe across agents, provenance, disagreement
surfacing, graceful malformed-input handling, and secret redaction.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bakery import findings


def _finding(title="missing timeout", severity="high", detail="agent hung",
             **kw):
    f = {"title": title, "severity": severity, "detail": detail}
    f.update(kw)
    return f


class MergeFindingsTest(unittest.TestCase):
    def _report(self, agent, founds, **extra):
        doc = {"agent": agent, "findings": founds}
        doc.update(extra)
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(doc))
        self.addCleanup(os.unlink, path)
        return path

    def _raw(self, text):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_dedupe_identical_findings(self):
        a = self._report("kite", [_finding()])
        b = self._report("claude", [_finding()])
        doc = findings.merge_findings([a, b])
        self.assertEqual(len(doc["findings"]), 1)
        self.assertEqual(doc["findings"][0]["reported_by"], ["claude", "kite"])
        self.assertEqual(doc["stats"]["duplicates_removed"], 1)

    def test_dedupe_by_explicit_key(self):
        a = self._report("kite", [_finding("x", "high", "d1", key="leak-1")])
        b = self._report("claude", [_finding("totally different title", "low",
                                            "different words", key="leak-1")])
        doc = findings.merge_findings([a, b])
        self.assertEqual(len(doc["findings"]), 1)
        self.assertEqual(doc["findings"][0]["key"], "key:leak-1")
        self.assertEqual(doc["findings"][0]["reported_by"], ["claude", "kite"])

    def test_same_title_different_detail_stays_separate(self):
        a = self._report("kite", [_finding("timeout", "high", "agent A hung")])
        b = self._report("claude", [_finding("timeout", "high", "agent B hung")])
        doc = findings.merge_findings([a, b])
        self.assertEqual(len(doc["findings"]), 2)

    def test_same_agent_repeating_finding_counts_once(self):
        a = self._report("kite", [_finding(), _finding()])
        doc = findings.merge_findings([a])
        self.assertEqual(len(doc["findings"]), 1)
        self.assertEqual(doc["findings"][0]["reported_by"], ["kite"])

    def test_disagreement_resolved_and_recorded(self):
        a = self._report("kite", [_finding(severity="low")])
        b = self._report("claude", [_finding(severity="high")])
        doc = findings.merge_findings([a, b])
        f = doc["findings"][0]
        self.assertEqual(f["severity"], "high")          # conservative: max wins
        self.assertTrue(f["disagreement"])
        self.assertEqual(
            f["disagreements"], [{"agent": "kite", "severity": "low"}]
        )
        self.assertEqual(doc["stats"]["disagreements"], 1)
        # Evidence from both sides survives — nothing silently dropped.
        self.assertEqual(len(f["evidence"]), 2)

    def test_no_disagreement_no_disagreements_key(self):
        a = self._report("kite", [_finding(severity="high")])
        b = self._report("claude", [_finding(severity="high")])
        doc = findings.merge_findings([a, b])
        f = doc["findings"][0]
        self.assertFalse(f["disagreement"])
        self.assertNotIn("disagreements", f)

    def test_sorted_by_severity_then_key(self):
        a = self._report("kite", [
            _finding("info thing", "info", "i"),
            _finding("crit thing", "critical", "c"),
            _finding("low thing", "low", "l"),
        ])
        doc = findings.merge_findings([a])
        self.assertEqual(
            [f["severity"] for f in doc["findings"]],
            ["critical", "low", "info"],
        )

    def test_stats_and_sources(self):
        a = self._report("kite", [_finding(), _finding("other", "low", "x")],
                         summary="two things")
        b = self._report("claude", [_finding()])
        doc = findings.merge_findings([a, b])
        self.assertEqual(doc["merged_by"], "bakery")
        self.assertEqual(doc["stats"]["agents"], 2)
        self.assertEqual(doc["stats"]["raw_findings"], 3)
        self.assertEqual(doc["stats"]["merged_findings"], 2)
        src = {s["agent"]: s for s in doc["sources"]}
        self.assertEqual(src["kite"]["findings"], 2)
        self.assertEqual(src["claude"]["findings"], 1)
        self.assertFalse(src["kite"]["partial"])

    def test_collect_doc_shape_accepted(self):
        inner = json.dumps({"agent": "kite", "findings": [_finding()]})
        doc = {
            "run_id": "r1", "recipe": "x", "status": "done",
            "agents": {
                "kite": {"state": "done", "exit_code": 0, "duration_s": 1.0,
                         "stdout": inner, "stderr": ""},
                "gemini": {"state": "timeout", "exit_code": -9, "duration_s": 60.0,
                           "stdout": "", "stderr": ""},
            },
        }
        p = self._raw(json.dumps(doc))
        merged = findings.merge_findings([p])
        self.assertEqual(len(merged["findings"]), 1)
        src = {s["agent"]: s for s in merged["sources"]}
        self.assertTrue(src["gemini"]["partial"])   # dead agent flagged
        self.assertEqual(src["gemini"]["findings"], 0)

    def test_unstructured_stdout_kept_as_info_finding(self):
        doc = {
            "run_id": "r1",
            "agents": {"kite": {"state": "done", "stdout": "just prose",
                               "stderr": ""}},
        }
        p = self._raw(json.dumps(doc))
        merged = findings.merge_findings([p])
        self.assertEqual(len(merged["findings"]), 1)
        self.assertEqual(merged["findings"][0]["severity"], "info")
        self.assertIn("just prose", merged["findings"][0]["evidence"][0]["detail"])


class MalformedInputTest(unittest.TestCase):
    def _raw(self, text):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def _report_obj(self, obj):
        return self._raw(json.dumps(obj))

    def test_not_json(self):
        with self.assertRaises(findings.FindingsError):
            findings.merge_findings([self._raw("not json {{{")])

    def test_missing_file(self):
        with self.assertRaises(findings.FindingsError):
            findings.merge_findings(["/tmp/definitely-not-here-xyz.json"])

    def test_no_files(self):
        with self.assertRaises(findings.FindingsError):
            findings.merge_findings([])

    def test_top_level_not_object(self):
        with self.assertRaises(findings.FindingsError):
            findings.merge_findings([self._raw("[1, 2]")])

    def test_missing_findings_array(self):
        with self.assertRaises(findings.FindingsError):
            findings.merge_findings([self._report_obj({"agent": "kite"})])

    def test_finding_not_object(self):
        with self.assertRaises(findings.FindingsError):
            findings.merge_findings(
                [self._report_obj({"agent": "kite", "findings": ["nope"]})])

    def test_finding_without_title(self):
        with self.assertRaises(findings.FindingsError):
            findings.merge_findings(
                [self._report_obj({"agent": "kite",
                                  "findings": [{"severity": "high"}]})])

    def test_finding_with_bad_severity(self):
        with self.assertRaises(findings.FindingsError):
            findings.merge_findings(
                [self._report_obj({"agent": "kite", "findings": [
                    {"title": "t", "severity": "catastrophic"}]})])

    def test_errors_are_clean_exceptions_not_tracebacks(self):
        # CLI catches FindingsError -> one-line SystemExit message.
        err = None
        try:
            findings.merge_findings([self._raw("junk")])
        except findings.FindingsError as e:
            err = e
        self.assertIsNotNone(err)
        self.assertIn("not valid JSON", str(err))


class SecurityTest(unittest.TestCase):
    def _report(self, agent, founds):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"agent": agent, "findings": founds}))
        self.addCleanup(os.unlink, path)
        return path

    def test_secret_shapes_redacted_in_merged_output(self):
        detail = ("leaked in env: API_KEY=AKIA" + "X" * 16 +
                  " and ghp_" + "a" * 24 + " and password=hunter2-hunter2")
        a = self._report("kite", [
            {"title": "creds in output", "severity": "high", "detail": detail}
        ])
        doc = findings.merge_findings([a])
        blob = json.dumps(doc)
        self.assertNotIn("AKIA" + "X" * 16, blob)
        self.assertNotIn("ghp_" + "a" * 24, blob)
        self.assertNotIn("hunter2-hunter2", blob)
        self.assertIn("REDACTED", blob)

    def test_ansi_and_controls_stripped(self):
        a = self._report("kite", [
            {"title": "\x1b[31mred\x1b[0m\x00title", "severity": "high",
             "detail": "ok"}
        ])
        doc = findings.merge_findings([a])
        self.assertEqual(doc["findings"][0]["title"], "redtitle")


class StableKeyTest(unittest.TestCase):
    def test_explicit_key_wins(self):
        self.assertEqual(
            findings.stable_key({"key": "  My-Key ", "title": "t", "detail": "d"}),
            "key:my-key",
        )

    def test_whitespace_case_insensitive(self):
        f1 = {"title": "Missing  Timeout", "detail": "Agent\n hung"}
        f2 = {"title": "missing timeout", "detail": "agent hung"}
        self.assertEqual(findings.stable_key(f1), findings.stable_key(f2))

    def test_title_alone_not_enough(self):
        f1 = {"title": "timeout", "detail": "agent A hung"}
        f2 = {"title": "timeout", "detail": "agent B hung"}
        self.assertNotEqual(findings.stable_key(f1), findings.stable_key(f2))


if __name__ == "__main__":
    unittest.main()
