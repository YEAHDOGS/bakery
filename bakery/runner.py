"""Swarm orchestration: spawn, supervise, status, kill, collect.

A run is a directory: .bakery/runs/<run-id>/
    recipe.toml        frozen copy of the recipe
    meta.json          pids, states, timing
    supervisor.log     supervisor stdout/stderr
    agents/<name>.out  agent stdout
    agents/<name>.err  agent stderr
    agents/<name>.code exit code, written when the agent finishes

`bake run` returns immediately; a detached supervisor process manages the
agents (batching up to max_parallel) and records everything.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .adapters import get as get_adapter
from .config import Config, load_config
from .recipe import Recipe, load_recipe
from .redact import redact
from .sandbox import sandbox_env


def runs_root() -> Path:
    return Path(".bakery") / "runs"


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Run states a watcher can stop on: the detached supervisor has finished
# bookkeeping. "killed" is terminal too (`bake kill` records it).
TERMINAL_STATES = {"done", "killed"}

# Seconds an over-time agent gets to shut itself down (SIGTERM) before the
# supervisor escalates to SIGKILL. Cooperative agents flush their final
# output and exit with their real code; only stuck agents die hard. The
# grace wait happens inside the supervisor's poll loop (agents are separate
# processes, so a stalled reap delays bookkeeping only, not other agents).
TERMINATE_GRACE_S = 5


def _terminate(proc: subprocess.Popen, pgid: int | None, grace: float = TERMINATE_GRACE_S) -> int | None:
    """SIGTERM an over-time agent, escalate to SIGKILL after `grace` seconds.

    Returns the child's exit code (real code on cooperative shutdown,
    negative on signal death), or None when the child was already reaped.
    Never raises.
    """
    rc = proc.poll()
    if rc is not None:
        return rc
    if pgid is None:
        return proc.wait()
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            return rc
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return proc.wait()


def _meta_path(run_id: str) -> Path:
    return runs_root() / run_id / "meta.json"


def _load_meta(run_id: str) -> dict:
    p = _meta_path(run_id)
    if not p.exists():
        raise SystemExit(f"unknown run '{run_id}' (no {_meta_path(run_id)})")
    return json.loads(p.read_text())


def _save_meta(run_id: str, meta: dict) -> None:
    _meta_path(run_id).write_text(json.dumps(meta, indent=2))


def _redacting_writer(path: Path):
    """Write-agent output to `path` with secrets redacted line by line.

    Returns (file_obj, stop). Agent output flows: child -> pipe -> daemon
    pump thread -> redact() -> log file. `stop()` closes the write end and
    waits for the pump to drain, so the log file is complete before the
    agent is marked finished.
    """
    rfd, wfd = os.pipe()
    write_end = os.fdopen(wfd, "w")

    def pump() -> None:
        with os.fdopen(rfd, "r", errors="replace") as src, open(path, "w") as dst:
            for line in src:
                dst.write(redact(line))

    thread = threading.Thread(target=pump, name=f"redact-{path.name}", daemon=True)
    thread.start()

    def stop() -> None:
        try:
            write_end.close()
        except OSError:
            pass
        thread.join(timeout=5)

    return write_end, stop


def _update_agent(run_id: str, name: str, **fields) -> dict:
    """Read-modify-write a single agent's record (avoids clobbering `kill`)."""
    meta = _load_meta(run_id)
    meta["agents"][name].update(fields)
    _save_meta(run_id, meta)
    return meta


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def resolve_run_params(recipe: Recipe, cfg: Config) -> dict[str, dict]:
    """Effective per-agent timeout/retries for a recipe under `cfg`.

    Pure helper shared by start_run (records it in meta.json) and _supervise
    (enforces it); both recompute from recipe + config so the frozen
    recipe.toml stays the source of truth for everything else.
    """
    params = {}
    for a in recipe.agents:
        params[a.name] = {
            "timeout": cfg.timeout_for(a.name, a.timeout, a.timeout_set),
            "retries": cfg.retries_for(a.name),
        }
    return params


def start_run(recipe_path: str, run_id: str | None = None) -> str:
    recipe = load_recipe(recipe_path)
    # Config preflight: a broken bake.yaml fails HERE, before the supervisor
    # detaches, instead of producing a silently misconfigured run.
    cfg = load_config()
    params = resolve_run_params(recipe, cfg)
    # Sandbox preflight: reject secret-bearing recipe env BEFORE the
    # supervisor detaches, so the user sees the error instead of a dead run.
    for agent in recipe.agents:
        sandbox_env(agent.env)
    run_id = run_id or new_run_id()
    run_dir = runs_root() / run_id
    if run_dir.exists():
        raise SystemExit(f"run '{run_id}' already exists")
    (run_dir / "agents").mkdir(parents=True)
    shutil.copy(recipe_path, run_dir / "recipe.toml")

    meta = {
        "run_id": run_id,
        "recipe": recipe.name,
        "status": "starting",
        "started_at": _now(),
        "finished_at": None,
        "agents": {
            a.name: {
                "pid": None,
                "pgid": None,
                "state": "pending",
                "exit_code": None,
                "started_at": None,
                "finished_at": None,
                "duration_s": None,
                "attempts": 0,
            }
            for a in recipe.agents
        },
        # Audit trail: what bake.yaml (or its absence) resolved to per agent.
        "config": {
            "source": cfg.source,
            "merge_strategy": cfg.merge_strategy,
            "params": params,
        },
    }
    _save_meta(run_id, meta)

    log = open(run_dir / "supervisor.log", "w")
    # The supervisor is a detached `python -m bakery` process: it must find
    # the bakery package no matter what cwd it inherits, so pin PYTHONPATH
    # to this checkout's parent directory.
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    sup_env = os.environ.copy()
    sup_env["PYTHONPATH"] = pkg_parent + os.pathsep + sup_env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "bakery", "_supervise", run_id],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
        env=sup_env,
    )
    meta["supervisor_pid"] = proc.pid
    _save_meta(run_id, meta)
    print(f"baked run {run_id} ({recipe.name}, {len(recipe.agents)} agents)")
    return run_id


def _supervise(run_id: str) -> None:
    """Detached supervisor: launches agents in waves, enforces timeouts."""
    run_dir = runs_root() / run_id
    recipe = load_recipe(str(run_dir / "recipe.toml"))
    # Config is read live at supervise start (cwd inherited from `bake run`
    # / `bake report`), so the frozen recipe.toml never embeds config values;
    # start_run already recorded the resolved params in meta.json for audit.
    from .config import apply_redact

    cfg = load_config()
    apply_redact(cfg)
    params = resolve_run_params(recipe, cfg)
    meta = _load_meta(run_id)
    meta["status"] = "running"
    _save_meta(run_id, meta)

    pending = list(recipe.agents)
    # running: name -> (proc, [stop_stream, ...]); stops drain the redacting pumps
    running: dict[str, tuple[subprocess.Popen, list]] = {}

    def stop_streams(name: str) -> None:
        entry = running.pop(name, None)
        if entry:
            for stop in entry[1]:
                stop()

    def launch(agent) -> None:
        adapter = get_adapter(recipe.backend)
        out, out_stop = _redacting_writer(run_dir / "agents" / f"{agent.name}.out")
        err, err_stop = _redacting_writer(run_dir / "agents" / f"{agent.name}.err")
        try:
            # sandbox: the agent gets a scrubbed allowlist environment —
            # inherited credentials are stripped before spawn.
            proc = adapter.spawn(agent, stdout=out, stderr=err, env=sandbox_env(agent.env))
        except Exception:
            out_stop()
            err_stop()
            raise
        running[agent.name] = (proc, [out_stop, err_stop])
        cur = _load_meta(run_id)["agents"][agent.name].get("attempts", 0)
        _update_agent(
            run_id,
            agent.name,
            pid=proc.pid,
            pgid=os.getpgid(proc.pid),
            state="running",
            started_at=_now(),
            attempts=cur + 1,
        )
        print(f"[{_now()}] launched {agent.name} pid={proc.pid}", flush=True)

    def finish(name: str, state: str, code: int | None) -> None:
        stop_streams(name)  # flush redacted logs to disk before recording outcome
        meta = _load_meta(run_id)
        if meta["agents"][name]["state"] == "killed":
            return  # `bake kill` already recorded the outcome; don't clobber it
        st = meta["agents"][name]
        finished_at = _now()
        fields = {"state": state, "exit_code": code, "finished_at": finished_at}
        if st["started_at"]:
            t0 = datetime.fromisoformat(st["started_at"])
            t1 = datetime.fromisoformat(finished_at)
            fields["duration_s"] = round((t1 - t0).total_seconds(), 1)
        _update_agent(run_id, name, **fields)
        (run_dir / "agents" / f"{name}.code").write_text(str(code if code is not None else -1))

    by_name = {a.name: a for a in recipe.agents}
    try:
        while pending or running:
            while pending and len(running) < recipe.max_parallel:
                launch(pending.pop(0))
            time.sleep(1)
            for name, (proc, _stops) in list(running.items()):
                agent = by_name[name]
                rc = proc.poll()
                if rc is not None:
                    finish(name, "done", rc)
                    continue
                fresh = _load_meta(run_id)["agents"][name]
                t0 = datetime.fromisoformat(fresh["started_at"])
                elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
                if elapsed > params[name]["timeout"]:
                    retries_used = fresh.get("attempts", 1) - 1
                    if retries_used < params[name]["retries"]:
                        # Timeout, but the run still owes this agent attempts:
                        # give it a graceful shutdown first (it may flush a
                        # partial report), then relaunch it from scratch.
                        # Its partial output is already on disk, flagged
                        # PARTIAL at merge time.
                        print(
                            f"[{_now()}] {name} timed out after "
                            f"{params[name]['timeout']}s; SIGTERM, "
                            f"{TERMINATE_GRACE_S}s grace, then retrying "
                            f"(attempt {fresh.get('attempts', 1) + 1})",
                            flush=True,
                        )
                        _terminate(proc, fresh["pgid"])
                        stop_streams(name)
                        launch(by_name[name])
                        continue
                    rc = _terminate(proc, fresh["pgid"])
                    finish(name, "timeout", rc if rc is not None else -1)
    finally:
        meta = _load_meta(run_id)
        if meta["status"] == "running":
            meta["status"] = "done"
            meta["finished_at"] = _now()
            _save_meta(run_id, meta)
        print(f"[{_now()}] run {run_id} finished", flush=True)


def refresh_status(meta: dict) -> dict:
    """Best-effort liveness check from outside the supervisor."""
    changed = False
    for name, st in meta["agents"].items():
        if st["state"] == "running" and st["pid"] and not _pid_alive(st["pid"]):
            st["state"] = "unknown"  # supervisor died before recording the outcome
            changed = True
    return meta


def status(run_id: str, fmt: str = "text") -> None:
    meta = _load_meta(run_id)
    before = json.dumps(meta["agents"], sort_keys=True)
    meta = refresh_status(meta)
    if json.dumps(meta["agents"], sort_keys=True) != before:
        _save_meta(run_id, meta)
    if fmt == "json":
        # Machine-readable swarm snapshot. meta.json is the supervisor's
        # bookkeeping; agents themselves only contribute the whitelisted
        # fields below (pid, pgid, attempt counts stay internal).
        doc = {
            "run_id": meta["run_id"],
            "recipe": meta["recipe"],
            "status": meta["status"],
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "agents": {
                name: {
                    "state": st["state"],
                    "exit_code": st["exit_code"],
                    "duration_s": st["duration_s"],
                }
                for name, st in meta["agents"].items()
            },
        }
        print(json.dumps(doc, indent=2, sort_keys=True))
        return
    print(f"run {meta['run_id']}  recipe={meta['recipe']}  status={meta['status']}")
    print(f"{'agent':<24}{'state':<10}{'exit':<6}{'duration':<10}{'pid'}")
    for name, st in meta["agents"].items():
        dur = f"{st['duration_s']}s" if st["duration_s"] is not None else "-"
        code = str(st["exit_code"]) if st["exit_code"] is not None else "-"
        print(f"{name:<24}{st['state']:<10}{code:<6}{dur:<10}{st['pid'] or '-'}")


def _read_agent_text(agents_dir: Path, name: str, stream: str) -> str:
    """Read one agent's captured stream; missing files read as empty.

    Log files are already secret-redacted at write time (see
    _redacting_writer), so re-serializing them in JSON leaks nothing new
    beyond what the text formats already expose.
    """
    p = agents_dir / f"{name}.{stream}"
    return p.read_text(errors="replace") if p.exists() else ""


def _summary_line(meta: dict) -> str:
    """One-line swarm state snapshot, e.g. `running 1/3 done (a1:running a2:done a3:pending)`."""
    agents = meta["agents"]
    n_done = sum(1 for s in agents.values() if s["state"] == "done")
    states = " ".join(f"{n}:{s['state']}" for n, s in agents.items())
    return f"{meta['status']} {n_done}/{len(agents)} done ({states})"


def watch(run_id: str, poll: float = 1.0, timeout: float | None = None) -> dict:
    """Stream a run's status to stdout until it reaches a terminal state.

    Prints a one-line snapshot whenever the swarm state changes, then a
    final line. Returns the final meta. Raises TimeoutError past `timeout`
    seconds and RuntimeError if the supervisor dies mid-run — a stuck
    "starting"/"running" meta would otherwise block forever.
    """
    deadline = time.monotonic() + timeout if timeout else None
    last = None
    while True:
        meta = _load_meta(run_id)
        if meta["status"] in TERMINAL_STATES:
            print(f"run {run_id} finished: {_summary_line(meta)}", flush=True)
            return meta
        sup_pid = meta.get("supervisor_pid")
        if sup_pid and not _pid_alive(sup_pid):
            raise RuntimeError(
                f"run '{run_id}': supervisor (pid {sup_pid}) died; "
                f"see .bakery/runs/{run_id}/supervisor.log"
            )
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(f"run '{run_id}' did not finish within {timeout}s")
        line = _summary_line(meta)
        if line != last:
            print(f"{_now()} run {run_id}: {line}", flush=True)
            last = line
        time.sleep(poll)


def logs(run_id: str, agent: str, stream: str = "out", tail: int = 0) -> None:
    p = runs_root() / run_id / "agents" / f"{agent}.{stream}"
    if not p.exists():
        raise SystemExit(f"no {stream} log for agent '{agent}' in run '{run_id}'")
    lines = p.read_text(errors="replace").splitlines()
    if tail:
        lines = lines[-tail:]
    print("\n".join(lines))


def kill(run_id: str) -> None:
    meta = _load_meta(run_id)
    killed = []
    for name, st in meta["agents"].items():
        if st["state"] in ("running", "pending") and st.get("pgid"):
            try:
                os.killpg(st["pgid"], signal.SIGKILL)
                killed.append(name)
                st["state"] = "killed"
                st["finished_at"] = _now()
            except (ProcessLookupError, PermissionError):
                pass
        elif st["state"] == "pending":
            st["state"] = "killed"
            killed.append(name)
    meta["status"] = "killed"
    meta["finished_at"] = _now()
    _save_meta(run_id, meta)
    print(f"killed {len(killed)} agents in run {run_id}: {', '.join(killed) or 'none'}")


def _agent_failed(st: dict) -> bool:
    """True unless the agent finished cleanly: done with exit code 0.

    Nonzero exits, timeouts, kills, and unknown/supervisor-died states all
    count as failed — any of them is worth a second attempt.
    """
    return not (st["state"] == "done" and st["exit_code"] == 0)


def retry(run_id: str, new_run_id: str | None = None) -> str | None:
    """Re-run only the agents of `run_id` that did not succeed.

    The retry is a FRESH run (new run id, own run dir) holding a filtered
    copy of the finished run's frozen recipe.toml, so a bad retry never
    clobbers the original run's evidence; the new meta records `retry_of`
    and `retried_agents` for the audit trail. The retried run goes through
    the same preflights as a normal run (recipe validation, config load,
    secret-env rejection) because it boots through start_run.

    Returns the new run id, or None when every agent already succeeded
    (no run is created in that case). Refuses a run that hasn't finished —
    the supervisor still owns those agents, and two supervisors writing
    one meta.json would race.
    """
    from . import report as _report  # local import: report.py imports this module

    meta = _load_meta(run_id)  # SystemExit("unknown run ...") when missing
    recipe = load_recipe(str(runs_root() / run_id / "recipe.toml"))
    if meta["status"] not in TERMINAL_STATES:
        raise SystemExit(
            f"run '{run_id}' is still {meta['status']}; retry only after it finishes"
        )
    failed = [a.name for a in recipe.agents if _agent_failed(meta["agents"][a.name])]
    if not failed:
        print(f"run {run_id}: nothing to retry — every agent succeeded")
        return None
    failed_set = set(failed)
    text = _report.emit_recipe_toml(recipe, [a for a in recipe.agents if a.name in failed_set])
    fd, tmp_path = tempfile.mkstemp(suffix=".toml", prefix="bakery-retry-")
    try:
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        new_id = start_run(tmp_path, new_run_id)
    finally:
        os.unlink(tmp_path)
    meta2 = _load_meta(new_id)
    meta2["retry_of"] = run_id
    meta2["retried_agents"] = failed
    _save_meta(new_id, meta2)
    print(
        f"retrying {len(failed)}/{len(recipe.agents)} agents from run {run_id} "
        f"as {new_id}: {', '.join(failed)}"
    )
    return new_id


def list_runs() -> None:
    root = runs_root()
    if not root.exists():
        print("no runs yet")
        return
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        try:
            meta = json.loads((d / "meta.json").read_text())
            n = len(meta["agents"])
            done = sum(1 for s in meta["agents"].values() if s["state"] == "done")
            print(f"{meta['run_id']}  {meta['recipe']:<20} {meta['status']:<9} {done}/{n} done")
        except (FileNotFoundError, json.JSONDecodeError):
            print(f"{d.name}  (corrupt)")


def collect(run_id: str, fmt: str = "markdown") -> None:
    from .config import apply_redact

    apply_redact(load_config())
    run_dir = runs_root() / run_id
    meta = _load_meta(run_id)
    agents_dir = run_dir / "agents"
    if fmt == "markdown":
        print(f"# Run report: {meta['recipe']} (`{run_id}`)")
        print(f"\nstatus: **{meta['status']}** · started {meta['started_at']}")
        for name, st in meta["agents"].items():
            dur = f"{st['duration_s']}s" if st["duration_s"] is not None else "?"
            print(f"\n## {name} — {st['state']} (exit {st['exit_code']}, {dur})")
            out = _read_agent_text(agents_dir, name, "out")
            err = _read_agent_text(agents_dir, name, "err")
            if out.strip():
                print("\n```\n" + out.rstrip() + "\n```")
            if err.strip():
                print("\n_stderr:_\n\n```\n" + err.rstrip() + "\n```")
    elif fmt == "json":
        # One machine-readable document: run bookkeeping plus every agent's
        # captured output, so scripts can filter/summarize without scraping
        # the markdown or text formats.
        doc = {
            "run_id": meta["run_id"],
            "recipe": meta["recipe"],
            "status": meta["status"],
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "agents": {
                name: {
                    "state": st["state"],
                    "exit_code": st["exit_code"],
                    "duration_s": st["duration_s"],
                    "stdout": _read_agent_text(agents_dir, name, "out"),
                    "stderr": _read_agent_text(agents_dir, name, "err"),
                }
                for name, st in meta["agents"].items()
            },
        }
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        for name, st in meta["agents"].items():
            print(f"=== {name} [{st['state']}] ===")
            p = agents_dir / f"{name}.out"
            if p.exists():
                print(p.read_text(errors="replace").rstrip())
