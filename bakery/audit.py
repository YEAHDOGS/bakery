"""Audit trail: every agent action, append-only and inspectable.

VISION.md (Security, non-negotiable): "Every agent action is logged" and
`bake logs` must be able to answer "who did what, when" for any run.
`<run-id>/audit.jsonl` records one JSON event per line, appended by the
supervisor as it launches/finishes agents and by operator commands
(`kill`, `retry`). `bake audit <run-id>` renders it as a timeline.

The audit log is evidence, so it must not become a secret leak of its own:
- env VALUES are never recorded — only env var NAMES (the guard has
  already refused any secret-bearing recipe, and the sandbox strips the
  supervisor's environment, so there is nothing to gain from logging values);
- `redact_secrets` is applied to any free-text field that reaches the log,
  the same belt-and-suspenders treatment as merged reports.

Corrupt lines are skipped on read, never fatal: a damaged audit file must
not take down the reader.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import runner as _runner_mod


def audit_path(run_dir: Path) -> Path:
    return run_dir / "audit.jsonl"


def append_event(
    run_id: str,
    event: str,
    actor: str = "supervisor",
    **fields: object,
) -> None:
    """Append one event to the run's audit log.

    Kept deliberately primitive: open/append/close, so the supervisor and
    operator commands can both write without locking. Sub-4KB appends to a
    regular file are effectively atomic.
    """
    from . import runner  # local import: runner imports audit at module level

    record: dict[str, object] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id,
        "event": event,
        "actor": actor,
    }
    record.update(fields)
    p = audit_path(runner.runs_root() / run_id)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def read_events(run_id: str) -> list[dict]:
    """Read a run's audit log, skipping corrupt lines."""
    from . import runner

    p = audit_path(runner.runs_root() / run_id)
    if not p.exists():
        return []
    events: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a damaged line must not kill the reader
    return events


def render_audit(
    run_id: str,
    agent: str | None = None,
    event: str | None = None,
) -> str:
    """Render a run's audit trail as a timeline table."""
    events = read_events(run_id)
    if not events:
        return f"no audit events recorded for run '{run_id}'\n"
    lines = [f"audit trail: run {run_id}", f"{'time':<22}{'event':<16}{'agent':<22}detail"]
    for ev in events:
        if agent and ev.get("agent") != agent:
            continue
        if event and ev.get("event") != event:
            continue
        detail = _detail_for(ev)
        lines.append(f"{ev.get('ts', '?'):<22}{str(ev.get('event', '?')):<16}{str(ev.get('agent') or '-'):<22}{detail}")
    return "\n".join(lines) + "\n"


def _detail_for(ev: dict) -> str:
    """One-line human detail for an event, built from safe fields only."""
    name = ev.get("event")
    if name == "run.started":
        parts = [f"recipe={ev.get('recipe')}"]
        parts.append(f"agents={ev.get('agents')}")
        if ev.get("retried_from"):
            parts.append(f"retried_from={ev['retried_from']}")
        return " ".join(parts)
    if name == "agent.launched":
        cmd = " ".join(str(c) for c in ev.get("cmd") or [])
        env = ",".join(str(e) for e in ev.get("env") or [])
        return f"pid={ev.get('pid')} pgid={ev.get('pgid')} timeout={ev.get('timeout')}s cmd=[{cmd}] env=[{env}]"
    if name == "agent.finished":
        detail = f"state={ev.get('state')} exit={ev.get('exit_code')} duration={ev.get('duration_s')}s"
        if ev.get("terminated_by"):
            detail += f" terminated_by={ev['terminated_by']}"
        return detail
    if name == "agent.timeout_warn":
        return (
            f"timeout={ev.get('timeout')}s SIGTERM sent, "
            f"SIGKILL in {ev.get('grace')}s if still running"
        )
    if name == "agent.killed":
        return f"killed by {ev.get('actor')}"
    if name == "run.killed":
        killed = ",".join(str(a) for a in ev.get("agents") or [])
        return f"killed {ev.get('killed')} agent(s): {killed or 'none'}"
    if name == "run.finished":
        return f"status={ev.get('status')}"
    if name == "retry.created":
        agents = ",".join(str(a) for a in ev.get("agents") or [])
        return f"retried_from={ev.get('retried_from')} agents=[{agents}]"
    return ""
