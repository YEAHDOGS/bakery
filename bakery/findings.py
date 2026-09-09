"""Structured report merging — the core of bakery's value prop.

`bake collect --format json` gives one machine-readable doc per run, and
agents write structured findings as JSON. This module takes N such report
files and produces ONE merged report:

- dedupe: identical findings (same stable key) collapse into one entry;
- provenance: every merged finding records which agents reported it;
- conflict resolution: when agents disagree (e.g. different severities for
  the same finding), the disagreement is RECORDED in the output — never
  silently dropped. Resolved severity is the most severe claim (conservative).

Accepted input shapes (auto-detected per file):

1. Per-agent structured report::

      {"agent": "kite",
       "summary": "optional one-liner",
       "findings": [{"key": "optional-stable-id", "title": "...",
                    "severity": "high|medium|low|info|critical",
                    "detail": "...", "location": "...",
                    "recommendation": "..."}, ...]}

2. A `bake collect --format json` document::

      {"run_id": "...", "agents": {"kite": {"state": "done", "stdout": "...",
                                            "stderr": "...", ...}, ...}}

   Each agent's stdout is parsed as shape 1; non-JSON stdout becomes a single
   info-severity finding so nothing is silently dropped. Agents whose state
   is not "done" are flagged `partial: true` in the sources list.

All input is UNTRUSTED (cross-agent injection): every string in the merged
output goes through sanitize (ANSI/control stripping) + redact
(secret-shape redaction), reusing bakery.merge and bakery.redact.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .merge import sanitize
from .redact import redact

SEVERITIES = ("info", "low", "medium", "high", "critical")
_SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITIES)}


class FindingsError(Exception):
    """Raised for malformed report input (CLI turns this into a clean error)."""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def stable_key(finding: dict) -> str:
    """Stable dedupe key for one finding.

    An explicit `key` field wins (normalized). Otherwise hash normalized
    title + detail — title alone over-dedupes ("missing timeouts" in two
    different places), so detail must anchor it.
    """
    explicit = finding.get("key")
    if explicit is not None and str(explicit).strip():
        return "key:" + _norm(str(explicit))
    title = _norm(str(finding.get("title", "")))
    detail = _norm(str(finding.get("detail", "")))
    digest = hashlib.sha256(f"{title}\x00{detail}".encode()).hexdigest()[:16]
    return f"auto:{digest}"


def _validate_finding(raw, agent: str, file: str, idx: int) -> dict:
    if not isinstance(raw, dict):
        raise FindingsError(
            f"{file}: finding #{idx} from agent '{agent}' is not an object"
        )
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise FindingsError(
            f"{file}: finding #{idx} from agent '{agent}' has no usable 'title'"
        )
    severity = str(raw.get("severity", "info")).strip().lower()
    if severity not in _SEVERITY_RANK:
        raise FindingsError(
            f"{file}: finding '{title}' from agent '{agent}' has unknown "
            f"severity {raw.get('severity')!r} (valid: {', '.join(SEVERITIES)})"
        )
    return {
        "key": stable_key(raw),
        "title": title.strip(),
        "severity": severity,
        "detail": str(raw.get("detail", "")),
        "location": str(raw.get("location", "")),
        "recommendation": str(raw.get("recommendation", "")),
        "agent": agent,
    }


def _parse_agent_report(text: str, file: str) -> tuple[str, str, list]:
    """Parse one per-agent report (shape 1). Returns (agent, summary, findings)."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise FindingsError(f"{file}: not valid JSON ({e})") from e
    if not isinstance(doc, dict):
        raise FindingsError(f"{file}: top-level JSON must be an object")
    if not isinstance(doc.get("findings"), list):
        raise FindingsError(f"{file}: missing required 'findings' array")
    agent = str(doc.get("agent") or Path(file).stem)
    summary = str(doc.get("summary", ""))
    findings = [
        _validate_finding(f, agent, file, i) for i, f in enumerate(doc["findings"])
    ]
    return agent, summary, findings


def _read_report(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise FindingsError(f"merge: no such report file '{path}'")
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        raise FindingsError(f"merge: cannot read '{path}': {e}") from e


def _load_reports(paths: list[str]) -> list[dict]:
    """Load N input files into per-agent records: {agent, summary, findings, state}."""
    records = []
    for path in paths:
        text = _read_report(path)
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise FindingsError(f"{path}: not valid JSON ({e})") from e
        if isinstance(doc, dict) and isinstance(doc.get("agents"), dict):
            # Shape 2: a `bake collect --format json` document.
            agents = doc["agents"]
            for name in sorted(agents):
                st = agents[name] if isinstance(agents[name], dict) else {}
                state = str(st.get("state", "unknown"))
                stdout = st.get("stdout", "")
                if isinstance(stdout, str) and stdout.strip():
                    try:
                        agent, summary, findings = _parse_agent_report(stdout, f"{path}:{name}")
                    except FindingsError:
                        # Unstructured stdout: keep it as one info finding
                        # so the agent's voice is not silently dropped.
                        agent, summary = name, ""
                        findings = [
                            {
                                "key": stable_key({"title": f"unstructured output ({name})", "detail": stdout}),
                                "title": f"unstructured output ({name})",
                                "severity": "info",
                                "detail": stdout,
                                "location": "",
                                "recommendation": "",
                                "agent": name,
                            }
                        ]
                else:
                    agent, summary, findings = name, "", []
                records.append(
                    {
                        "agent": agent,
                        "summary": summary,
                        "findings": findings,
                        "state": state,
                        "file": path,
                        "partial": state != "done",
                    }
                )
        else:
            agent, summary, findings = _parse_agent_report(text, path)
            records.append(
                {
                    "agent": agent,
                    "summary": summary,
                    "findings": findings,
                    "state": "done",
                    "file": path,
                    "partial": False,
                }
            )
    return records


def _clean(value):
    """Sanitize + redact every string in a nested structure."""
    if isinstance(value, str):
        return redact(sanitize(value))
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def merge_findings(paths: list[str]) -> dict:
    """Merge N structured report files; return the merged report dict."""
    if not paths:
        raise FindingsError("merge: need at least one report file")
    records = _load_reports(paths)

    grouped: dict[str, list[dict]] = {}
    for rec in records:
        seen: set[str] = set()  # dedupe the same finding repeated by one agent
        for f in rec["findings"]:
            if f["key"] in seen:
                continue
            seen.add(f["key"])
            grouped.setdefault(f["key"], []).append(f)

    duplicates_removed = sum(len(r["findings"]) for r in records) - len(grouped)
    merged = []
    n_disagreements = 0
    for key, entries in grouped.items():
        reported_by = sorted({e["agent"] for e in entries})
        sevs = {e["severity"] for e in entries}
        resolved = max(sevs, key=lambda s: _SEVERITY_RANK[s])
        disagreement = len(sevs) > 1
        if disagreement:
            n_disagreements += 1
        # Representative title: longest non-empty (most specific), deterministic
        # tie-break on agent name so output is stable across runs.
        title = max(
            ((e["title"], e["agent"]) for e in entries),
            key=lambda t: (len(t[0]), t[1]),
        )[0]
        evidence = [
            {
                "agent": e["agent"],
                "severity": e["severity"],
                "detail": e["detail"],
                "location": e["location"],
                "recommendation": e["recommendation"],
            }
            for e in sorted(entries, key=lambda e: e["agent"])
        ]
        entry = {
            "key": key,
            "title": title,
            "severity": resolved,
            "reported_by": reported_by,
            "disagreement": disagreement,
            "evidence": evidence,
        }
        if disagreement:
            entry["disagreements"] = [
                {"agent": e["agent"], "severity": e["severity"]}
                for e in sorted(entries, key=lambda e: e["agent"])
                if e["severity"] != resolved
            ]
        merged.append(entry)

    merged.sort(key=lambda m: (-_SEVERITY_RANK[m["severity"]], m["key"]))

    doc = {
        "merged_by": "bakery",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": [
            {
                "agent": r["agent"],
                "file": r["file"],
                "findings": len(r["findings"]),
                "state": r["state"],
                "partial": r["partial"],
            }
            for r in records
        ],
        "findings": merged,
        "stats": {
            "agents": len(records),
            "raw_findings": sum(len(r["findings"]) for r in records),
            "merged_findings": len(merged),
            "duplicates_removed": duplicates_removed,
            "disagreements": n_disagreements,
        },
    }
    return _clean(doc)


def write_merged_json(paths: list[str], out: str | None) -> dict:
    """Merge N structured report files to JSON; return the merged doc.

    Prints to stdout or writes to `out`. Malformed input raises
    FindingsError (the CLI turns it into a clean one-line error).
    """
    from .config import apply_redact, load_config

    apply_redact(load_config())
    doc = merge_findings(paths)
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"merged {len(paths)} report(s) -> {out}")
    else:
        print(text, end="")
    return doc
