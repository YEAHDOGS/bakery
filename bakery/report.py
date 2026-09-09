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

import json
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

# --- merge-mode finding extraction -------------------------------------------------
# A "finding" is one claim an agent makes about the run's question. We extract
# them heuristically from markdown-looking output: bullet lines, numbered
# lines, and short `key: value` verdict lines. This is deliberately
# conservative — plain prose paragraphs stay out of the findings sections and
# remain available verbatim in the per-agent detail.
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(?P<f>.+)$")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(?P<f>.+)$")
_VERDICT_RE = re.compile(
    r"^\s*(?:verdict|result|status|recommendation|conclusion|outcome|decision)\s*:\s*(?P<f>.+)$",
    re.IGNORECASE,
)
_MAX_FINDING_CHARS = 400

# Tokens that decide a finding's polarity. The canonical form of a finding
# strips these, so "the build passes" and "the build does not pass" land in
# the same bucket with opposite polarity -> flagged as a conflict. Same-bucket
# findings with the same polarity (and identical normalized text) are merged
# as corroborated. This is a heuristic for triage, not a proof system.
_NEG_TOKENS = {"not", "no", "never", "n't", "none", "neither", "nor", "cannot", "can't"}
_FAIL_TOKENS = {"fail", "failed", "fails", "failing", "failure", "false", "broken", "bad", "missing", "reject", "rejected", "rejection"}
_PASS_TOKENS = {"pass", "passed", "passes", "passing", "true", "ok", "works", "working", "good", "accept", "accepted", "approve", "approved"}
_POLARITY_TOKENS = _NEG_TOKENS | _FAIL_TOKENS | _PASS_TOKENS
# Function words dropped from the canonical form so "the build does not pass"
# and "the build passes" share the bucket "build".
_STOPWORDS = {
    "the", "a", "an", "to", "of", "on", "in", "for", "and", "or", "it", "this",
    "that", "these", "those", "with", "do", "does", "did", "is", "are", "was",
    "were", "be", "been", "being", "has", "have", "had",
}


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


def _normalize_finding(text: str) -> str:
    """Canonical text for a finding: casefolded, de-punctuated at the edges."""
    text = " ".join(text.casefold().split())
    return text.strip("`'\".,;:!?-—–()[]{}")


def _word_key(w: str) -> str:
    """Token key: strip wrapping punctuation and possessive endings."""
    w = w.strip("`'\".,;:!?")
    for suffix in ("'s", "’s", "s'"):
        if w.endswith(suffix) and len(w) > len(suffix):
            w = w[: -len(suffix)]
            break
    return w


def _finding_signature(finding: str) -> tuple[str, str]:
    """(canonical, polarity) for conflict/dedup bucketing.

    Polarity is "pos", "neg", or "neutral"; the canonical form drops all
    polarity tokens so opposite claims about the same subject collide.
    """
    words = _normalize_finding(finding).split()
    stripped = [
        w for w in words
        if _word_key(w) not in (_POLARITY_TOKENS | _STOPWORDS)
    ]
    canonical = " ".join(stripped).strip()
    neg = any(_word_key(w) in _NEG_TOKENS | _FAIL_TOKENS for w in words)
    pos = any(_word_key(w) in _PASS_TOKENS for w in words)
    polarity = "neg" if neg else ("pos" if pos else "neutral")
    return canonical or _normalize_finding(finding), polarity


def extract_findings(text: str) -> list[str]:
    """Pull finding-shaped lines out of (already sanitized) agent output."""
    findings: list[str] = []
    for line in text.splitlines():
        m = _BULLET_RE.match(line) or _NUMBERED_RE.match(line) or _VERDICT_RE.match(line)
        if not m:
            continue
        f = " ".join(m.group("f").split())
        if f and len(f) <= _MAX_FINDING_CHARS:
            findings.append(f)
    return findings


def merge_run(run_id: str) -> str:
    """Merge a completed run's per-agent report files into one markdown report.

    Reads `<run-id>/agents/<agent>.out` for every agent in the run meta and
    produces a single merged document:

    - **corroborated findings**: identical claims made by 2+ agents, deduped
      with attribution;
    - **conflicts**: same-subject claims whose polarity disagrees across
      agents, each quoted with attribution;
    - **unique findings**: claims made by exactly one agent;
    - **per-agent detail**: every agent's full sanitized output, preserved
      verbatim (fenced). Agents with no output file are noted, not dropped.

    Agent output is sanitized exactly like `render_report` (fences escaped,
    control chars stripped, secret-shaped values redacted) before anything
    is parsed or embedded.
    """
    meta = runner._load_meta(run_id)
    agents_dir = runner.runs_root() / run_id / "agents"

    unfinished = [n for n, st in meta["agents"].items() if st.get("state") not in TERMINAL_STATES]

    # bucket key -> {norm_text: set(agents)}; conflict buckets keyed by canonical
    agreed: dict[str, set[str]] = {}          # norm finding -> agents
    buckets: dict[str, dict[str, set[str]]] = {}  # canonical -> polarity -> set(norm findings)
    detail: list[tuple[str, dict, str]] = []  # (name, state, sanitized out or "")

    for name, st in meta["agents"].items():
        p = agents_dir / f"{name}.out"
        raw = p.read_text(errors="replace") if p.exists() else ""
        clean = sanitize_output(raw)
        detail.append((name, st, clean))
        for f in extract_findings(clean):
            norm = _normalize_finding(f)
            if not norm:
                continue
            agreed.setdefault(norm, set()).add(name)
            canonical, polarity = _finding_signature(f)
            buckets.setdefault(canonical, {}).setdefault(polarity, set()).add(norm)

    lines = [f"# Merged report: {meta['recipe']} (`{run_id}`)"]
    lines.append(f"status: **{meta['status']}** · started {meta['started_at']} · {len(meta['agents'])} agents")
    if unfinished:
        names = ", ".join(_safe_name(n) for n in unfinished)
        lines.append(f"\n_⚠️ run not fully complete — agents still active: {names}; their output may be partial_")

    # Corroborated: same normalized text from 2+ agents.
    corroborated = sorted((f, names) for f, names in agreed.items() if len(names) >= 2)
    if corroborated:
        lines.append("\n## ✅ Corroborated findings")
        for f, names in corroborated:
            who = ", ".join(sorted(_safe_name(n) for n in names))
            lines.append(f"- {f} _(agreed: {who})_")

    # Conflicts: one canonical bucket holding 2+ distinct non-neutral polarities.
    conflicts = [
        (canon, polmap)
        for canon, polmap in buckets.items()
        if len({p for p in polmap if p != "neutral"}) >= 2
    ]
    if conflicts:
        lines.append("\n## ⚠️ Conflicts")
        for canon, polmap in sorted(conflicts):
            lines.append(f"\n**{canon or '(unparseable claim)'}**")
            for polarity in ("pos", "neg", "neutral"):
                if polarity not in polmap:
                    continue
                for norm in sorted(polmap[polarity]):
                    who = ", ".join(sorted(_safe_name(n) for n in agreed[norm]))
                    lines.append(f'- {norm} _(says: {who})_')

    # Unique: one agent only.
    unique = sorted((f, next(iter(names))) for f, names in agreed.items() if len(names) == 1)
    if unique:
        lines.append("\n## 🔍 Unique findings (single agent)")
        for f, name in unique:
            lines.append(f"- {f} _({_safe_name(name)})_")

    if not (corroborated or conflicts or unique):
        lines.append("\n_no findings extracted from agent output_")

    lines.append("\n## 📄 Per-agent detail")
    for name, st, clean in detail:
        safe = _safe_name(name)
        dur = f"{st.get('duration_s')}s" if st.get("duration_s") is not None else "?"
        lines.append(f"\n### {safe} — {st.get('state')} (exit {st.get('exit_code')}, {dur})")
        if st.get("state") not in TERMINAL_STATES:
            lines.append("_note: agent had not finished; output below may be partial_")
        if clean.strip():
            lines.append("\n```\n" + clean + "\n```")
        else:
            lines.append("_no output captured_")
    return "\n".join(lines) + "\n"


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


def render_collect(run_id: str, fmt: str = "markdown") -> str:
    """Render a run's collected outputs as a string.

    The ``json`` format is new for scripting (VISION.md: orchestration
    plumbing; roadmap "JSON output mode"). Unlike the legacy markdown/text
    formats — kept byte-for-byte compatible for downstream consumers —
    the JSON payload embeds sanitized agent output, because agent output
    is untrusted and a machine reader shouldn't inherit raw fences,
    control characters, or echoed credentials either.
    """
    meta = runner._load_meta(run_id)
    if fmt == "json":
        agents: dict[str, dict] = {}
        for name, st in meta["agents"].items():
            agents[name] = {
                "state": st.get("state"),
                "exit_code": st.get("exit_code"),
                "duration_s": st.get("duration_s"),
                "out": sanitize_output(_agent_text(runner, run_id, name, "out")),
                "err": sanitize_output(_agent_text(runner, run_id, name, "err")),
            }
        doc = {
            "run_id": meta.get("run_id"),
            "recipe": meta.get("recipe"),
            "status": meta.get("status"),
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "agents": agents,
        }
        return json.dumps(doc, indent=2) + "\n"
    agents_dir = runner.runs_root() / run_id / "agents"
    if fmt == "markdown":
        lines = [
            f"# Run report: {meta['recipe']} (`{run_id}`)",
            "",
            f"status: **{meta['status']}** · started {meta['started_at']}",
        ]
        for name, st in meta["agents"].items():
            dur = f"{st['duration_s']}s" if st["duration_s"] is not None else "?"
            lines += ["", f"## {name} — {st['state']} (exit {st['exit_code']}, {dur})"]
            out = (
                (agents_dir / f"{name}.out").read_text(errors="replace")
                if (agents_dir / f"{name}.out").exists()
                else ""
            )
            err = (
                (agents_dir / f"{name}.err").read_text(errors="replace")
                if (agents_dir / f"{name}.err").exists()
                else ""
            )
            if out.strip():
                lines += ["", "```", out.rstrip(), "```"]
            if err.strip():
                lines += ["", "_stderr:_", "", "```", err.rstrip(), "```"]
        return "\n".join(lines) + "\n"
    lines = []
    for name, st in meta["agents"].items():
        lines.append(f"=== {name} [{st['state']}] ===")
        p = agents_dir / f"{name}.out"
        if p.exists():
            lines.append(p.read_text(errors="replace").rstrip())
    return "\n".join(lines) + "\n"


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
