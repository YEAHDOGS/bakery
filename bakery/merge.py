"""Merge standalone report files into one markdown document.

Useful for the hand-carry workflow: Kite, Claude, and Gemini each produce a
report file somewhere; `bake merge kite.md claude.md gemini.md` stitches them
into a single document with clear per-agent attribution, in the order given.

Reports are treated as UNTRUSTED input (agents read each other's output;
prompt injection is the sharpest failure). The merger only sanitizes
mechanics: it strips ANSI escape sequences and stray control characters that
could mangle a terminal or break markdown fences. It never executes,
interprets, or rewrites report content.
"""

from __future__ import annotations

import re
from pathlib import Path

from .redact import redact

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize(text: str) -> str:
    """Strip ANSI escapes and stray control chars; keep tabs/newlines."""
    return _CONTROL_RE.sub("", _ANSI_RE.sub("", text))


def _read_report(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"merge: no such report file '{path}'")
    return p.read_text(errors="replace")


def _status_line(status: dict | None) -> str | None:
    """Render a bakery-generated outcome line for one agent section.

    `status` is supervisor bookkeeping (state / exit_code / duration_s), never
    agent output, so it can be trusted to describe what happened — but we
    still sanitize it (threat model: meta.json is writable outside the
    supervisor, so treat it as untrusted input too).
    """
    if not status:
        return None
    state = sanitize(str(status.get("state", "?")))
    code = status.get("exit_code")
    dur = status.get("duration_s")
    code_s = f"exit {int(code)}" if isinstance(code, int) else None
    dur_s = f"{float(dur):.1f}s" if isinstance(dur, (int, float)) else None
    detail = ", ".join(b for b in (code_s, dur_s) if b)
    if state == "done":
        return f"outcome: **done**" + (f" ({detail})" if detail else "")
    # Anything else means the output below may be truncated or missing.
    label = f"outcome: **{state.upper()}**" if state else "outcome: **unknown**"
    if detail:
        label += f" ({detail})"
    return label + " — captured output is **PARTIAL**, do not trust it as complete"


def _outcome_summary(labels: list[str], statuses: dict | None) -> str | None:
    """Top-of-doc line: how many agents finished vs timed out / failed."""
    if not statuses:
        return None
    bad = [l for l in labels if (statuses.get(l) or {}).get("state") != "done"]
    n_done = len(labels) - len(bad)
    if not bad:
        return f"**Outcome:** {n_done}/{len(labels)} agents finished cleanly."
    names = ", ".join(f"`{sanitize(l)}`" for l in bad)
    return (
        f"**Outcome:** {n_done}/{len(labels)} agents finished cleanly; "
        f"{len(bad)} did not: {names} (partial or missing output)"
    )


def merge_reports(
    paths: list[str],
    names: list[str] | None = None,
    statuses: dict[str, dict] | None = None,
) -> str:
    """Return one markdown doc concatenating the report files, in order.

    `names` optionally overrides the per-section label (defaults to the path).
    `statuses` optionally maps each label to the supervisor's bookkeeping
    (state / exit_code / duration_s); every section then carries its outcome
    and timed-out/killed agents are flagged as PARTIAL, so a dead agent's
    truncated output can never pass silently as a complete report.
    """
    if not paths:
        raise SystemExit("merge: need at least one report file")
    if names and len(names) != len(paths):
        raise SystemExit("merge: names length must match paths length")
    labels = names if names else paths
    parts = ["# Merged report", ""]
    summary = _outcome_summary(labels, statuses)
    if summary:
        parts.append(summary)
        parts.append("")
    for i, path in enumerate(paths):
        raw_label = labels[i]
        label = sanitize(raw_label)
        body = redact(sanitize(_read_report(path))).rstrip()
        parts.append(f"## Report: `{label}`")
        status_line = _status_line(statuses.get(raw_label) if statuses else None)
        if status_line:
            parts.append("")
            parts.append(status_line)
        parts.append("")
        parts.append(body if body else "_empty report_")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def write_merged(paths: list[str], out: str | None) -> str:
    """Merge the reports, printing to stdout or writing to `out`; return doc."""
    doc = merge_reports(paths)
    if out:
        Path(out).write_text(doc)
        print(f"merged {len(paths)} report(s) -> {out}")
    else:
        print(doc, end="")
    return doc
