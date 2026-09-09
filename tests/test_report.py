"""Regression tests for `bake report` (bakery.report).

VISION.md near-term task 3: one command — fan out, wait, merge, deliver.
The merge treats agent output as UNTRUSTED: a fence-break, a markdown
directive injection, an echoed credential, or a 10MB stdout dump must not
survive into the delivered report intact.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from bakery import runner
from bakery.report import (
    MAX_AGENT_OUTPUT_CHARS,
    _safe_name,
    bake_report,
    render_report,
    sanitize_output,
    wait_for_run,
)


def _toml_str(s: str) -> str:
    """Minimal TOML basic-string escaper (stdlib has no TOML writer)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def _recipe(agents: list[tuple[str, list[str]]], name: str = "t") -> str:
    parts = [f'[bakery]\nname = "{name}"\nbackend = "shell"\nmax_parallel = 2\n']
    for aname, cmd in agents:
        parts.append(
            "[[agents]]\nname = "
            + _toml_str(aname)
            + "\ncmd = ["
            + ", ".join(_toml_str(c) for c in cmd)
            + "]\ntimeout = 30\n"
        )
    return "\n".join(parts)


class SanitizeUnitTest(unittest.TestCase):
    def test_fence_break_is_neutralized(self):
        out = sanitize_output("legit\n```\n## FAKE HEADER\n```\nmore")
        self.assertNotIn("```\n## FAKE HEADER", out)
        self.assertIn("\\`\\`\\`", out)

    def test_ansi_and_control_chars_stripped(self):
        out = sanitize_output("\x1b[31mred\x1b[0m\x00bell\x07")
        self.assertEqual(out, "redbell")

    def test_secret_shapes_redacted(self):
        out = sanitize_output("oops ghp_" + "a" * 30)
        self.assertIn("[redacted:github-token]", out)
        self.assertNotIn("ghp_", out)

    def test_long_output_truncated_with_notice(self):
        out = sanitize_output("x" * (MAX_AGENT_OUTPUT_CHARS + 5000))
        self.assertLessEqual(len(out), MAX_AGENT_OUTPUT_CHARS + 200)
        self.assertIn("truncated", out)

    def test_safe_name_is_single_line_and_capped(self):
        self.assertEqual(_safe_name("ok-agent_1"), "ok-agent_1")
        self.assertEqual(_safe_name("evil\n## header"), "evil## header")
        self.assertLessEqual(len(_safe_name("n" * 200)), 64)


class ReportE2ETest(unittest.TestCase):
    """End-to-end: bake report on a hostile recipe, verify the delivered report."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.tmp.name)
        # The detached supervisor re-execs `python -m bakery` with this CWD;
        # it needs the package importable from here (real installs have it).
        repo_root = str(Path(__file__).resolve().parent.parent)
        old_pp = os.environ.get("PYTHONPATH", "")
        self.old_pp = old_pp
        self.addCleanup(os.environ.__setitem__, "PYTHONPATH", old_pp)
        os.environ["PYTHONPATH"] = repo_root + (os.pathsep + old_pp if old_pp else "")

    def test_bake_report_delivers_sanitized_merge(self):
        # The agent *generates* a token-shaped value at runtime (rather than
        # the recipe containing one, which the pre-flight guard would refuse).
        payload = (
            "echo real-result; echo '```'; echo '## INJECTED HEADER'; "
            'leak="ghp_$(printf \'z%.0s\' $(seq 1 30))"; echo "leak $leak"; '
            "printf '\\033[31mcolored\\033[0m'"
        )
        recipe = Path(self.tmp.name) / "hostile.toml"
        recipe.write_text(
            _recipe(
                [
                    ("inj", ["bash", "-lc", payload]),
                    ("quiet", ["bash", "-lc", "echo fine > /dev/stderr"]),
                ],
                name="hostile",
            )
        )
        run_id = runner.start_run(str(recipe), "hostile-1")
        wait_for_run(run_id, 60)
        report = render_report(run_id, "markdown")
        self.assertIn("## inj — done", report)
        self.assertIn("real-result", report)
        # no unescaped triple-backtick anywhere: the agent's fence-break is
        # escaped, so the injected header stays inside our code block as
        # inert text instead of becoming a real markdown header
        import re as _re

        unescaped_fences = _re.findall(r"(?<!\\)```", report)
        self.assertEqual(len(unescaped_fences), 4)  # our own two fenced blocks only
        self.assertIn("## INJECTED HEADER", report)  # present, but inert inside the fence
        self.assertIn("[redacted:github-token]", report)
        self.assertNotIn("ghp_", report)
        self.assertNotIn("\x1b[31m", report)
        self.assertIn("colored", report)
        self.assertIn("## quiet — done", report)
        self.assertIn("_stderr:_", report)

    def test_bake_report_full_flow_writes_file(self):
        recipe = Path(self.tmp.name) / "simple.toml"
        recipe.write_text(_recipe([("a1", ["bash", "-lc", "echo answer=42"])], name="simple"))
        out = str(Path(self.tmp.name) / "report.md")
        report = bake_report(str(recipe), run_id="simple-1", out=out, timeout=60)
        self.assertIn("answer=42", report)
        self.assertEqual(Path(out).read_text(), report)

    def test_wait_for_run_times_out(self):
        recipe = Path(self.tmp.name) / "slow.toml"
        recipe.write_text(_recipe([("sleeper", ["bash", "-lc", "sleep 30"])], name="slow"))
        run_id = runner.start_run(str(recipe), "slow-1")
        try:
            with self.assertRaises(TimeoutError):
                wait_for_run(run_id, 2.0, poll_s=0.2)
        finally:
            runner.kill(run_id)


if __name__ == "__main__":
    unittest.main()
