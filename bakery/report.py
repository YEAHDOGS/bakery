"""`bake report`: fan out, wait, merge, deliver.

VISION.md near-term task 3: one command that fans a brief out to all
configured agents, collects their outputs, merges them into a single
report, and delivers it.

Rendering treats agent output as UNTRUSTED (VISION.md security:
"prompt-injection is the sharpest failure when agents read each other's
output"). An agent's stdout could contain a markdown directive, an escape
from the fenced code block, or an echoed credential. `render_report`
neutralizes all three:

- output is fenced, and fence sequences inside the output are escaped so
  the fence cannot be broken out of;
- ANSI escapes and control characters are stripped;
- secret-shaped values are redacted (`guard.redact_secrets`);
- per-agent output is length-capped with a truncation notice, so one chatty
  agent can't blow up the merged report.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import runner
from .guard import redact_secrets
from .recipe import load_recipe

TERMINAL_STATES = ("done", "timeout", "killed", "unknown")
MAX_AGENT_OUTPUT_CHARS = 100_000
MAX_AGENT_NAME_CHARS = 64

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _strip_controls(text: str) -> str:
    """Drop ANSI escapes and non-printable control characters."""
    text = _ANSI_RE.sub("", text)
    return "".join(c for c in text if c.isprintable() or c in "\n\t")


def _escape_fences(text: str) -> str:
    """Neutralize triple-backtick sequences so a fenced block can't be broken."""
    return text.replace("```", "\\`\\`\\`")


def _safe_name(name: str) -> str:
    """Agent names reach the report as markdown headers; keep them one-line."""
    name = "".join(c for c in str(name) if c.isprintable() and c not in "\r\n")
    return name[:MAX_AGENT_NAME_CHARS] or "agent"


def sanitize_output(text: str, max_chars: int = MAX_AGENT_OUTPUT_CHARS) -> str:
    """Make agent output safe to embed in a merged report."""
    text = _strip_controls(text)
    text = redact_secrets(text)
    text = _escape_fences(text)
    truncated = ""
    if len(text) > max_chars:
        dropped = len(text) - max_chars
        text = text[:max_chars]
        truncated = f"\n\n_[truncated: {dropped:,} further characters omitted]_"
    return text.rstrip() + truncated


def _agent_text(runner_mod, run_id: str, name: str, stream: str) -> str:
    p = runner_mod.runs_root() / run_id / "agents" / f"{name}.{stream}"
    if not p.exists():
        return ""
    return p.read_text(errors="replace")


def render_report(run_id: str, fmt: str = "markdown") -> str:
    """Merge a finished run's agent outputs into one sanitized report."""
    meta = runner._load_meta(run_id)
    lines: list[str] = []
    if fmt == "markdown":
        lines.append(f"# Run report: {meta['recipe']} (`{run_id}`)")
        lines.append(f"\nstatus: **{meta['status']}** · started {meta['started_at']}")
        for name, st in meta["agents"].items():
            safe = _safe_name(name)
            dur = f"{st['duration_s']}s" if st["duration_s"] is not None else "?"
            lines.append(f"\n## {safe} — {st['state']} (exit {st['exit_code']}, {dur})")
            if st["state"] not in TERMINAL_STATES:
                lines.append("\n_note: agent had not finished; output below may be partial_")
            out = sanitize_output(_agent_text(runner, run_id, name, "out"))
            if out.strip():
                lines.append("\n```\n" + out + "\n```")
            err = sanitize_output(_agent_text(runner, run_id, name, "err"))
            if err.strip():
                lines.append("\n_stderr:_\n\n```\n" + err + "\n```")
        return "\n".join(lines) + "\n"
    for name, st in meta["agents"].items():
        lines.append(f"=== {_safe_name(name)} [{st['state']}] ===")
        out = sanitize_output(_agent_text(runner, run_id, name, "out"))
        if out.strip():
            lines.append(out)
    return "\n".join(lines) + "\n"


def wait_for_run(run_id: str, timeout: float, poll_s: float = 1.0) -> dict:
    """Block until every agent in the run reaches a terminal state.

    Returns the final meta. Raises TimeoutError if the deadline passes.
    """
    deadline = time.time() + timeout
    while True:
        meta = runner._load_meta(run_id)
        states = [st["state"] for st in meta["agents"].values()]
        if states and all(s in TERMINAL_STATES for s in states):
            return meta
        # refresh liveness so a dead supervisor can't strand us on "running"
        before = [st["state"] for st in meta["agents"].values()]
        meta = runner.refresh_status(meta)
        if [st["state"] for st in meta["agents"].values()] != before:
            runner._save_meta(run_id, meta)
            states = [st["state"] for st in meta["agents"].values()]
            if states and all(s in TERMINAL_STATES for s in states):
                return meta
        if time.time() >= deadline:
            raise TimeoutError(f"run '{run_id}' did not finish within {timeout:.0f}s")
        time.sleep(poll_s)


def bake_report(
    recipe_path: str,
    run_id: str | None = None,
    fmt: str = "markdown",
    out: str | None = None,
    timeout: float | None = None,
) -> str:
    """Start a run, wait for it to finish, merge a sanitized report, deliver."""
    recipe = load_recipe(recipe_path)
    if timeout is None:
        timeout = max(a.timeout for a in recipe.agents) + 120.0
    run_id = runner.start_run(recipe_path, run_id)
    wait_for_run(run_id, timeout)
    report = render_report(run_id, fmt)
    if out:
        Path(out).write_text(report)
    return report
