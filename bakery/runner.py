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
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .adapters import get as get_adapter
from .recipe import Recipe, load_recipe
from .redact import redact
from .sandbox import sandbox_env


def runs_root() -> Path:
    return Path(".bakery") / "runs"


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def start_run(recipe_path: str, run_id: str | None = None) -> str:
    recipe = load_recipe(recipe_path)
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
            }
            for a in recipe.agents
        },
    }
    _save_meta(run_id, meta)

    log = open(run_dir / "supervisor.log", "w")
    subprocess.Popen(
        [sys.executable, "-m", "bakery", "_supervise", run_id],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    print(f"baked run {run_id} ({recipe.name}, {len(recipe.agents)} agents)")
    return run_id


def _supervise(run_id: str) -> None:
    """Detached supervisor: launches agents in waves, enforces timeouts."""
    run_dir = runs_root() / run_id
    recipe = load_recipe(str(run_dir / "recipe.toml"))
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
        _update_agent(
            run_id,
            agent.name,
            pid=proc.pid,
            pgid=os.getpgid(proc.pid),
            state="running",
            started_at=_now(),
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
                if elapsed > agent.timeout:
                    try:
                        os.killpg(fresh["pgid"], signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    proc.wait()
                    finish(name, "timeout", -1)
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


def status(run_id: str) -> None:
    meta = _load_meta(run_id)
    before = json.dumps(meta["agents"], sort_keys=True)
    meta = refresh_status(meta)
    if json.dumps(meta["agents"], sort_keys=True) != before:
        _save_meta(run_id, meta)
    print(f"run {meta['run_id']}  recipe={meta['recipe']}  status={meta['status']}")
    print(f"{'agent':<24}{'state':<10}{'exit':<6}{'duration':<10}{'pid'}")
    for name, st in meta["agents"].items():
        dur = f"{st['duration_s']}s" if st["duration_s"] is not None else "-"
        code = str(st["exit_code"]) if st["exit_code"] is not None else "-"
        print(f"{name:<24}{st['state']:<10}{code:<6}{dur:<10}{st['pid'] or '-'}")


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
    run_dir = runs_root() / run_id
    meta = _load_meta(run_id)
    agents_dir = run_dir / "agents"
    if fmt == "markdown":
        print(f"# Run report: {meta['recipe']} (`{run_id}`)")
        print(f"\nstatus: **{meta['status']}** · started {meta['started_at']}")
        for name, st in meta["agents"].items():
            dur = f"{st['duration_s']}s" if st["duration_s"] is not None else "?"
            print(f"\n## {name} — {st['state']} (exit {st['exit_code']}, {dur})")
            out = (agents_dir / f"{name}.out").read_text(errors="replace") if (agents_dir / f"{name}.out").exists() else ""
            err = (agents_dir / f"{name}.err").read_text(errors="replace") if (agents_dir / f"{name}.err").exists() else ""
            if out.strip():
                print("\n```\n" + out.rstrip() + "\n```")
            if err.strip():
                print("\n_stderr:_\n\n```\n" + err.rstrip() + "\n```")
    else:
        for name, st in meta["agents"].items():
            print(f"=== {name} [{st['state']}] ===")
            p = agents_dir / f"{name}.out"
            if p.exists():
                print(p.read_text(errors="replace").rstrip())
