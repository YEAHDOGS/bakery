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


def merge_reports(paths: list[str], names: list[str] | None = None) -> str:
    """Return one markdown doc concatenating the report files, in order.

    `names` optionally overrides the per-section label (defaults to the path).
    """
    if not paths:
        raise SystemExit("merge: need at least one report file")
    if names and len(names) != len(paths):
        raise SystemExit("merge: names length must match paths length")
    parts = ["# Merged report", ""]
    for i, path in enumerate(paths):
        label = names[i] if names else path
        body = redact(sanitize(_read_report(path))).rstrip()
        parts.append(f"## Report: `{label}`")
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
