"""`bake report` — the one-command entry point from VISION.md.

Describe the mission once; bakery dispatches it, supervises the swarm, and
hands you one merged report:

    bake report recipe.toml [-o merged.md] [--agents kite,claude] [--timeout 600]
    bake report --task "audit the org repos" --agents kite,claude,gemini -o r.md

The run is synchronous: it starts the swarm, waits for every agent to
finish (or time out), then merges the per-agent outputs into a single
markdown document. All the security machinery stays on: scrubbed agent
environments (sandbox_env), secret redaction in logs, and sanitize-at-merge
against cross-agent injection.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from . import merge, runner
from .runner import TERMINAL_STATES
from .config import apply_redact, load_config
from .recipe import Recipe, load_recipe


def wait_for_run(run_id: str, timeout: float = 3600, poll: float = 1.0) -> dict:
    """Block until the run reaches a terminal state; return the final meta.

    Raises RuntimeError if the detached supervisor dies before finishing
    (a stuck "starting"/"running" meta would otherwise block forever) and
    TimeoutError on the deadline.
    """
    deadline = time.monotonic() + timeout
    while True:
        meta = runner._load_meta(run_id)
        if meta["status"] in TERMINAL_STATES:
            return meta
        sup_pid = meta.get("supervisor_pid")
        if sup_pid and not runner._pid_alive(sup_pid):
            raise RuntimeError(
                f"run '{run_id}': supervisor (pid {sup_pid}) died; "
                f"see .bakery/runs/{run_id}/supervisor.log"
            )
        if time.monotonic() > deadline:
            raise TimeoutError(f"run '{run_id}' did not finish within {timeout}s")
        time.sleep(poll)


def _quote(value: str) -> str:
    """TOML basic string via JSON quoting (escape-safe for our schema)."""
    import json

    return json.dumps(value)


def emit_recipe_toml(recipe: Recipe, agents: list | None = None) -> str:
    """Render a recipe (optionally agent-filtered) back to TOML text."""
    chosen = agents if agents is not None else recipe.agents
    lines = [
        "[bakery]",
        f"name = {_quote(recipe.name)}",
        f"backend = {_quote(recipe.backend)}",
        f"max_parallel = {recipe.max_parallel}",
        "",
    ]
    for a in chosen:
        lines += [
            "[[agents]]",
            f"name = {_quote(a.name)}",
            f"timeout = {a.timeout}",
            f"workdir = {_quote(a.workdir)}",
        ]
        if a.cmd:
            lines.append("cmd = [" + ", ".join(_quote(c) for c in a.cmd) + "]")
        if a.env:
            lines.append("env = {" + ", ".join(f"{_quote(k)} = {_quote(v)}" for k, v in a.env.items()) + "}")
        if a.report:
            lines.append(f"report = {_quote(a.report)}")
        if a.report_file:
            lines.append(f"report_file = {_quote(a.report_file)}")
        if a.delay:
            lines.append(f"delay = {a.delay}")
        if a.exit_code:
            lines.append(f"exit_code = {a.exit_code}")
        lines.append("")
    return "\n".join(lines)


def stub_recipe_from_task(task: str, agent_names: list[str]) -> Recipe:
    """Build a fixture recipe from a bare task string.

    Each agent is a fixture playing an honest stub report: the orchestration,
    sandboxing, and merge layers run for real, but the "report" is clearly
    labeled as a stub until a live backend adapter is plugged in behind that
    agent's name.
    """
    from .recipe import AgentSpec

    agents = [
        AgentSpec(
            name=name,
            cmd=[],
            report=(
                f"# Stub report — {name}\n\n"
                f"**Task:** {task}\n\n"
                "_This is a fixture stub, not a real agent answer._\n"
                "Plug a live backend adapter (claude/gemini API) behind this "
                "agent name for production runs."
            ),
        )
        for name in agent_names
    ]
    return Recipe(name=f"report-{len(agent_names)}-agents", backend="fixture",
                  max_parallel=len(agent_names), agents=agents)


def bake_report(
    recipe_path: str | None = None,
    *,
    task: str | None = None,
    agent_names: list[str] | None = None,
    only_agents: list[str] | None = None,
    output: str | None = None,
    timeout: float | None = None,
) -> str:
    """Fan out, wait, collect, merge — return the path of the merged doc."""
    if recipe_path and task:
        raise ValueError("give either a recipe or --task, not both")
    if recipe_path:
        recipe = load_recipe(recipe_path)
    elif task:
        if not agent_names:
            raise ValueError("--task needs --agents to name the workers")
        recipe = stub_recipe_from_task(task, agent_names)
    else:
        raise ValueError("need a recipe path or --task")

    agents = recipe.agents
    if only_agents:
        known = {a.name for a in recipe.agents}
        missing = [n for n in only_agents if n not in known]
        if missing:
            raise ValueError(f"unknown agents in recipe: {', '.join(missing)}")
        agents = [a for a in recipe.agents if a.name in only_agents]
    if not agents:
        raise ValueError("no agents selected")

    # Per-run configuration: resolve timeouts (bake.yaml agent overrides,
    # recipe explicit, bake.yaml global) and register extra redact patterns
    # for the merge below. The resolved timeouts are baked into the frozen
    # recipe so the detached supervisor enforces exactly what the waiter
    # expects. NOTE: --timeout stays the waiter's deadline; it never changes
    # agent timeouts.
    cfg = load_config()
    apply_redact(cfg)
    for a in agents:
        a.timeout = int(cfg.timeout_for(a.name, a.timeout, a.timeout_set))
        a.timeout_set = True

    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(emit_recipe_toml(recipe, agents))
        tmp_recipe = f.name

    run_id = runner.start_run(tmp_recipe)
    try:
        limit = timeout or max(a.timeout for a in agents) + 60
        meta = wait_for_run(run_id, timeout=limit)
        run_dir = runner.runs_root() / run_id
        out_paths = [str(run_dir / "agents" / f"{a.name}.out") for a in agents]
        labels = [a.name for a in agents]
        # Outcome bookkeeping: timed-out / killed / failed agents get a
        # PARTIAL flag on their section, so a dead agent's truncated output
        # can never pass silently as a complete report.
        statuses = {
            a.name: {
                "state": meta["agents"][a.name]["state"],
                "exit_code": meta["agents"][a.name]["exit_code"],
                "duration_s": meta["agents"][a.name]["duration_s"],
            }
            for a in agents
        }
        doc = merge.merge_reports(
            out_paths, names=labels, statuses=statuses, strategy=cfg.merge_strategy
        )
        if output:
            Path(output).write_text(doc)
        else:
            out_default = run_dir / "report.md"
            out_default.write_text(doc)
            output = str(out_default)
        n_done = sum(1 for s in meta["agents"].values() if s["state"] == "done")
        print(f"report: {n_done}/{len(agents)} agents done -> {output}")
        return output
    finally:
        Path(tmp_recipe).unlink(missing_ok=True)
