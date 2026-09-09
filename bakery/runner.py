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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .recipe import Recipe, load_recipe
from .guard import guard_recipe
from .sandbox import sandboxed_env


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
    guard_recipe(recipe)  # refuse to bake a run that hands secrets to agents
    run_id = run_id or new_run_id()
    return _launch_run(recipe, run_id, Path(recipe_path).read_text(), None)


def _launch_run(recipe: Recipe, run_id: str, recipe_text: str, retried_from: str | None) -> str:
    """Create a run dir from an in-memory recipe and detach its supervisor."""
    run_dir = runs_root() / run_id
    if run_dir.exists():
        raise SystemExit(f"run '{run_id}' already exists")
    (run_dir / "agents").mkdir(parents=True)
    (run_dir / "recipe.toml").write_text(recipe_text)

    meta = {
        "run_id": run_id,
        "recipe": recipe.name,
        "status": "starting",
        "started_at": _now(),
        "finished_at": None,
        "retried_from": retried_from,
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
    running: dict[str, subprocess.Popen] = {}

    def launch(agent) -> None:
        # Least privilege: the agent gets a sandboxed environment, never the
        # supervisor's full one (bakery.sandbox) — exported tokens in the
        # supervisor's shell must not reach workers.
        env = sandboxed_env(os.environ, agent.env)
        out = open(run_dir / "agents" / f"{agent.name}.out", "w")
        err = open(run_dir / "agents" / f"{agent.name}.err", "w")
        proc = subprocess.Popen(
            agent.cmd,
            stdout=out,
            stderr=err,
            cwd=agent.workdir,
            env=env,
            start_new_session=True,
        )
        running[agent.name] = proc
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
            for name, proc in list(running.items()):
                agent = by_name[name]
                rc = proc.poll()
                if rc is not None:
                    finish(name, "done", rc)
                    del running[name]
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
                    del running[name]
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


def render_status(run_id: str, fmt: str = "text") -> str:
    """Render a run's status as a string (text table or JSON for scripting)."""
    meta = _load_meta(run_id)
    before = json.dumps(meta["agents"], sort_keys=True)
    meta = refresh_status(meta)
    if json.dumps(meta["agents"], sort_keys=True) != before:
        _save_meta(run_id, meta)
    if fmt == "json":
        return json.dumps(meta, indent=2, sort_keys=True) + "\n"
    lines = [
        f"run {meta['run_id']}  recipe={meta['recipe']}  status={meta['status']}",
        f"{'agent':<24}{'state':<10}{'exit':<6}{'duration':<10}{'pid'}",
    ]
    for name, st in meta["agents"].items():
        dur = f"{st['duration_s']}s" if st["duration_s"] is not None else "-"
        code = str(st["exit_code"]) if st["exit_code"] is not None else "-"
        lines.append(f"{name:<24}{st['state']:<10}{code:<6}{dur:<10}{st['pid'] or '-'}")
    return "\n".join(lines) + "\n"


def status(run_id: str, fmt: str = "text") -> None:
    print(render_status(run_id, fmt), end="")


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


def _agent_needs_retry(st: dict) -> bool:
    """Failed (non-zero exit) or timed-out agents get retried; everything
    else — done cleanly, killed deliberately, still running/pending — does not."""
    if st["state"] == "timeout":
        return True
    return st["state"] == "done" and st["exit_code"] not in (0, None)


def _toml_escape(s: str) -> str:
    return (
        '"'
        + s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        + '"'
    )


def _dump_recipe_toml(recipe: Recipe, agents: list) -> str:
    """Serialize a recipe (or a subset of its agents) back to TOML.

    `bake retry` bakes a *filtered* recipe, so it needs a writer, not just the
    tomllib reader. The schema here mirrors recipe.py's loader exactly.
    """
    lines = [
        "[bakery]",
        f"name = {_toml_escape(recipe.name)}",
        f"backend = {_toml_escape(recipe.backend)}",
        f"max_parallel = {recipe.max_parallel}",
        "",
    ]
    for a in agents:
        lines += [
            "[[agents]]",
            f"name = {_toml_escape(a.name)}",
            "cmd = [" + ", ".join(_toml_escape(c) for c in a.cmd) + "]",
            f"timeout = {a.timeout}",
            f"workdir = {_toml_escape(a.workdir)}",
            "env = {"
            + ", ".join(f"{_toml_escape(k)} = {_toml_escape(v)}" for k, v in a.env.items())
            + "}",
            "",
        ]
    return "\n".join(lines)


def retry_run(run_id: str) -> str | None:
    """Re-run only the failed/timed-out agents of a finished run.

    Bakes a new run `<run-id>-retry<N>` from the run's frozen recipe, filtered
    to the retryable agents. Returns the new run id, or None when there is
    nothing to retry (agents that succeeded keep their results; a retry never
    rewrites the original run).
    """
    meta = _load_meta(run_id)
    if meta["status"] in ("running", "starting"):
        raise SystemExit(
            f"run '{run_id}' is still {meta['status']} — wait for it to finish before retrying"
        )
    recipe = load_recipe(str(runs_root() / run_id / "recipe.toml"))
    retry_agents = [a for a in recipe.agents if _agent_needs_retry(meta["agents"][a.name])]
    if not retry_agents:
        print(f"nothing to retry in run '{run_id}': no failed or timed-out agents")
        return None
    guard_recipe(Recipe(recipe.name, recipe.backend, recipe.max_parallel, retry_agents))
    n = 1
    while (runs_root() / f"{run_id}-retry{n}").exists():
        n += 1
    new_id = f"{run_id}-retry{n}"
    sub_recipe = Recipe(recipe.name, recipe.backend, recipe.max_parallel, retry_agents)
    new_id = _launch_run(sub_recipe, new_id, _dump_recipe_toml(recipe, retry_agents), run_id)
    print(f"retrying {len(retry_agents)} of {len(recipe.agents)} agents from run '{run_id}'")
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


def clean_runs(keep: int = 10, dry_run: bool = False) -> dict:
    """Prune old run directories, keeping the most recent `keep` finished runs.

    Roadmap quick win: `.bakery/runs` accumulates one directory per swarm;
    `bake clean` keeps the freshest `keep` runs and prunes the rest.

    Safety (a run dir holds the only copy of a swarm's logs and reports):
    - runs still "running"/"starting" are NEVER deleted and don't count
      against `keep`;
    - directories without a readable meta.json are skipped, never touched;
    - `dry_run=True` lists what would go without deleting anything.

    Returns {"deleted": [...], "skipped": [...], "kept": n}. With `dry_run`,
    "deleted" is the list of runs that *would* be pruned.
    """
    if keep < 0:
        raise SystemExit("--keep must be >= 0")
    root = runs_root()
    if not root.exists():
        return {"deleted": [], "skipped": [], "kept": 0}
    runs: list[tuple[str, str, str | None, Path]] = []
    skipped: list[str] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        try:
            meta = json.loads((d / "meta.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            skipped.append(d.name)
            continue
        runs.append((meta.get("started_at") or d.name, d.name, meta.get("status"), d))
    runs.sort(key=lambda r: (r[0], r[1]), reverse=True)  # newest first
    active = [r for r in runs if r[2] in ("running", "starting")]
    finished = [r for r in runs if r[2] not in ("running", "starting")]
    targets = finished[keep:]
    deleted: list[str] = []
    for _, run_id, _status, path in targets:
        if not dry_run:
            shutil.rmtree(path)
        deleted.append(run_id)
    return {
        "deleted": sorted(deleted),
        "skipped": sorted(skipped),
        "kept": len(finished[:keep]) + len(active),
    }


def collect(run_id: str, fmt: str = "markdown") -> None:
    # Imported here: bakery.report imports this module at its top level.
    from . import report as _report

    print(_report.render_collect(run_id, fmt), end="")
