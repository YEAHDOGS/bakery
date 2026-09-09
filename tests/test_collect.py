"""Regression tests for JSON output mode on `bake status` / `bake collect`.

Roadmap quick win: "--format json on status and collect for scripting."
The JSON surface is new, so it embeds SANITIZED agent output (agent output
is untrusted — see test_report.py). The legacy markdown/text formats are
pinned byte-for-byte so downstream consumers don't silently break.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from bakery import runner
from bakery.__main__ import main
from bakery.report import MAX_AGENT_OUTPUT_CHARS, render_collect


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _recipe(name: str = "t") -> str:
    return (
        f'[bakery]\nname = "{name}"\nbackend = "shell"\nmax_parallel = 2\n\n'
        '[[agents]]\nname = "ok"\ncmd = ["true"]\ntimeout = 30\n'
    )


class CollectJsonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.tmp.name)
        self.run_id = "fake-1"
        agents_dir = Path(".bakery") / "runs" / self.run_id / "agents"
        agents_dir.mkdir(parents=True)
        meta = {
            "run_id": self.run_id,
            "recipe": "t",
            "status": "done",
            "started_at": "2026-09-09T15:00:00+00:00",
            "finished_at": "2026-09-09T15:00:10+00:00",
            "agents": {
                "ok": {
                    "pid": 123, "pgid": 123, "state": "done", "exit_code": 0,
                    "started_at": "2026-09-09T15:00:00+00:00",
                    "finished_at": "2026-09-09T15:00:01+00:00", "duration_s": 1.5,
                },
                "big": {
                    "pid": 124, "pgid": 124, "state": "done", "exit_code": 0,
                    "started_at": "2026-09-09T15:00:01+00:00",
                    "finished_at": "2026-09-09T15:00:01+00:00", "duration_s": 0.2,
                },
            },
        }
        (Path(".bakery") / "runs" / self.run_id / "meta.json").write_text(
            json.dumps(meta, indent=2)
        )
        (Path(".bakery") / "runs" / self.run_id / "recipe.toml").write_text(_recipe())
        leak = "ghp_" + "z" * 30
        (agents_dir / "ok.out").write_text(
            "plain result\n```\nfence-break\n```\n\x1b[31mcolored\x1b[0m\nleak=" + leak + "\n"
        )
        (agents_dir / "ok.err").write_text("a warning\n")
        (agents_dir / "big.out").write_text("x" * (MAX_AGENT_OUTPUT_CHARS + 1000))

    def test_json_collect_is_parseable_and_sanitized(self):
        doc = json.loads(render_collect(self.run_id, "json"))
        self.assertEqual(doc["run_id"], self.run_id)
        self.assertEqual(doc["recipe"], "t")
        self.assertEqual(doc["status"], "done")
        ok = doc["agents"]["ok"]
        self.assertEqual((ok["state"], ok["exit_code"], ok["duration_s"]), ("done", 0, 1.5))
        # untrusted output is neutralized even in the machine-readable format
        self.assertNotIn("ghp_", ok["out"])
        self.assertIn("[redacted:github-token]", ok["out"])
        self.assertNotIn("\x1b[31m", ok["out"])
        self.assertNotIn("```\nfence-break", ok["out"])
        self.assertIn("plain result", ok["out"])
        self.assertEqual(ok["err"].strip(), "a warning")
        # chatty agents are capped, with a notice
        big = doc["agents"]["big"]
        self.assertIn("truncated", big["out"])
        self.assertLessEqual(len(big["out"]), MAX_AGENT_OUTPUT_CHARS + 200)

    def test_markdown_collect_byte_parity(self):
        out = render_collect(self.run_id, "markdown")
        backtick = chr(96) * 3
        self.assertEqual(
            out,
            "\n".join(
                [
                    "# Run report: t (`fake-1`)",
                    "",
                    "status: **done** · started 2026-09-09T15:00:00+00:00",
                    "",
                    "## ok — done (exit 0, 1.5s)",
                    "",
                    backtick,
                    "plain result\n" + backtick + "\nfence-break\n" + backtick
                    + "\n\x1b[31mcolored\x1b[0m\nleak=ghp_"
                    + "z" * 30,
                    backtick,
                    "",
                    "_stderr:_",
                    "",
                    backtick,
                    "a warning",
                    backtick,
                    "",
                    "## big — done (exit 0, 0.2s)",
                    "",
                    backtick,
                    "x" * (MAX_AGENT_OUTPUT_CHARS + 1000),
                    backtick,
                ]
            )
            + "\n",
        )

    def test_text_collect_byte_parity(self):
        self.assertEqual(
            render_collect(self.run_id, "text"),
            "=== ok [done] ===\n"
            "plain result\n```\nfence-break\n```\n\x1b[31mcolored\x1b[0m\nleak=ghp_"
            + "z" * 30
            + "\n=== big [done] ===\n"
            + "x" * (MAX_AGENT_OUTPUT_CHARS + 1000)
            + "\n",
        )

    def test_status_json_is_parseable(self):
        doc = json.loads(runner.render_status(self.run_id, "json"))
        self.assertEqual(doc["run_id"], self.run_id)
        self.assertEqual(doc["agents"]["ok"]["state"], "done")
        self.assertEqual(doc["agents"]["ok"]["pid"], 123)

    def test_status_text_byte_parity(self):
        self.assertEqual(
            runner.render_status(self.run_id, "text"),
            "run fake-1  recipe=t  status=done\n"
            + f"{'agent':<24}{'state':<10}{'exit':<6}{'duration':<10}{'pid'}\n"
            + f"{'ok':<24}{'done':<10}{'0':<6}{'1.5s':<10}{123}\n"
            + f"{'big':<24}{'done':<10}{'0':<6}{'0.2s':<10}{124}\n",
        )

    def test_cli_wires_json_through(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["status", self.run_id, "--format", "json"])
        self.assertEqual(json.loads(buf.getvalue())["run_id"], self.run_id)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["collect", self.run_id, "--format", "json"])
        self.assertEqual(json.loads(buf.getvalue())["agents"]["ok"]["state"], "done")


if __name__ == "__main__":
    unittest.main()
