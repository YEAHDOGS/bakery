"""`bake plan` — dry run: print the launch plan without spawning anything.

Shows what `bake run` / `bake report` WOULD do for a recipe: the effective
per-agent timeouts/retries (bake.yaml overrides resolved), the launch waves
(FIFO batches of `max_parallel`), a worst-case wall-clock estimate (every
attempt at full timeout, retries included), and the sandbox verdict for
each agent.
Runs the same preflights as a real run (recipe validation, config load,
secret-env rejection) so a plan that prints cleanly is a recipe that will
actually bake off. Spawns nothing, writes nothing, touches no run dirs.

    bake plan recipe.toml
    bake plan recipe.toml --only a1,a2
    bake plan --task "audit the org repos" --agents kite,claude,gemini
    bake plan recipe.toml --format json     # machine-readable plan document

Env values are NEVER printed (keys only) — a plan is safe to paste into a
ticket or log, unlike a full recipe dump.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from . import report
from .config import load_config
from .recipe import Recipe, load_recipe
from .runner import resolve_run_params
from .sandbox import sandbox_env


@dataclass
class AgentPlan:
    name: str
    cmd: list[str]
    workdir: str
    timeout: int
    retries: int
    env_keys: list[str]
    backend: str


@dataclass
class Plan:
    recipe_name: str
    recipe_path: str | None
    backend: str
    max_parallel: int
    config_source: str
    merge_strategy: str
    agents: list[AgentPlan] = field(default_factory=list)
    waves: list[list[str]] = field(default_factory=list)


def _plan_agents(recipe: Recipe) -> Plan:
    cfg = load_config()
    # Same preflights as a real run: config errors and secret-bearing recipe
    # env surface HERE (loudly, before anyone would spawn), not mid-supervise.
    params = resolve_run_params(recipe, cfg)
    for agent in recipe.agents:
        sandbox_env(agent.env)  # raises on secret-looking keys
    agents = [
        AgentPlan(
            name=a.name,
            cmd=list(a.cmd),
            workdir=a.workdir,
            timeout=int(params[a.name]["timeout"]),
            retries=int(params[a.name]["retries"]),
            env_keys=sorted(a.env.keys()),
            backend=recipe.backend,
        )
        for a in recipe.agents
    ]
    mp = recipe.max_parallel or len(agents)
    waves = [
        [a.name for a in agents[i : i + mp]] for i in range(0, len(agents), mp)
    ]
    return Plan(
        recipe_name=recipe.name,
        recipe_path=None,
        backend=recipe.backend,
        max_parallel=mp,
        config_source=cfg.source,
        merge_strategy=cfg.merge_strategy,
        agents=agents,
        waves=waves,
    )


def plan_recipe(recipe: Recipe, recipe_path: str | None = None) -> Plan:
    """Build the launch plan for a recipe. Pure read — spawns nothing."""
    plan = _plan_agents(recipe)
    plan.recipe_path = recipe_path
    return plan


def _cmd_str(cmd: list[str]) -> str:
    return " ".join(cmd) if cmd else "(fixture report)"


def _fmt_dur(seconds: int) -> str:
    """Human-readable duration: 45s, 2m 30s, 1h 5m, 3d 2h (drops zero units)."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def worst_case_wall(plan: Plan) -> dict:
    """Worst-case wall-clock seconds for this plan.

    Assumes every attempt of every agent burns its full timeout, scheduled
    FIFO onto `max_parallel` slots with list scheduling (the supervisor
    refills freed slots from the pending queue in FIFO order). A timed-out
    agent relaunches from scratch in its own slot, so one agent's total
    burn is `timeout * (1 + retries)` — retries ARE included.

    Returns {"total_s": makespan, "per_wave_s": [per-wave bounds]}, where
    each per-wave bound matches the strict FIFO-batch view that
    plan.waves prints.
    """
    jobs = [a.timeout * (1 + a.retries) for a in plan.agents]
    slots = max(1, plan.max_parallel)
    free_at = [0] * slots
    for burn in jobs:  # FIFO: next agent takes the earliest-free slot
        earliest = min(range(slots), key=lambda s: free_at[s])
        free_at[earliest] += burn
    by_name = {a.name: a for a in plan.agents}
    per_wave = [
        max(by_name[n].timeout * (1 + by_name[n].retries) for n in wave)
        if wave
        else 0
        for wave in plan.waves
    ]
    return {"total_s": max(free_at) if free_at else 0, "per_wave_s": per_wave}


def render_text(plan: Plan) -> str:
    """Human-readable launch plan."""
    lines = [
        f"plan: {plan.recipe_name} (backend={plan.backend}, "
        f"max_parallel={plan.max_parallel}, config={plan.config_source})",
        f"merge strategy: {plan.merge_strategy} · "
        f"env: scrubbed allowlist (credentials stripped)",
        "",
    ]
    if not plan.agents:
        lines.append("(no agents)")
    for i, wave in enumerate(plan.waves, 1):
        hint = "launch first" if i == 1 else "launch when a slot frees"
        lines.append(f"wave {i} — {hint}:")
        by_name = {a.name: a for a in plan.agents}
        for name in wave:
            a = by_name[name]
            lines.append(
                f"  {a.name:<24}timeout={a.timeout}s retries={a.retries} "
                f"cmd={_cmd_str(a.cmd)}"
            )
            if a.env_keys:
                lines.append(f"  {'':<24}env keys: {', '.join(a.env_keys)} (values never shown)")
    lines += [
        "",
        f"launch waves: {len(plan.waves)} · total agents: {len(plan.agents)}",
        _worst_case_line(plan),
        "nothing was spawned — `bake run` to execute this plan.",
    ]
    return "\n".join(lines)


def _worst_case_line(plan: Plan) -> str:
    """One-line worst-case wall-time estimate for the text render."""
    wc = worst_case_wall(plan)
    per_wave = ", ".join(_fmt_dur(w) for w in wc["per_wave_s"])
    return (
        f"worst-case wall time: ~{_fmt_dur(wc['total_s'])} "
        f"(per wave: {per_wave}; every attempt at full timeout, "
        "retries included)"
    )


def render_json(plan: Plan) -> str:
    """Machine-readable launch plan (env values never included)."""
    wc = worst_case_wall(plan)
    doc = {
        "spawned": False,
        "recipe_name": plan.recipe_name,
        "recipe_path": plan.recipe_path,
        "backend": plan.backend,
        "max_parallel": plan.max_parallel,
        "config_source": plan.config_source,
        "merge_strategy": plan.merge_strategy,
        "waves": plan.waves,
        # seconds only: assumes every attempt burns its full timeout,
        # retries included — an upper bound, not a prediction.
        "worst_case_wall_s": wc["total_s"],
        "worst_case_wave_s": wc["per_wave_s"],
        "agents": [
            {
                "name": a.name,
                "cmd": a.cmd,
                "workdir": a.workdir,
                "timeout": a.timeout,
                "retries": a.retries,
                "env_keys": a.env_keys,
                "backend": a.backend,
            }
            for a in plan.agents
        ],
    }
    return json.dumps(doc, indent=2, sort_keys=True)


def bake_plan(
    recipe_path: str | None = None,
    *,
    task: str | None = None,
    agent_names: list[str] | None = None,
    only_agents: list[str] | None = None,
    fmt: str = "text",
) -> Plan:
    """Print the launch plan for a recipe (or --task stub) and return it.

    Mirrors `report.bake_report`'s argument validation. Raises ValueError
    (surfaced by the CLI as a one-line `plan: ...` error, no traceback) on
    bad recipes — this also answers the roadmap's "recipe validation errors
    with file/section/agent index" quick win at the CLI surface.
    """
    if recipe_path and task:
        raise ValueError("give either a recipe or --task, not both")
    if recipe_path:
        recipe = load_recipe(recipe_path)
    elif task:
        if not agent_names:
            raise ValueError("--task needs --agents to name the workers")
        recipe = report.stub_recipe_from_task(task, agent_names)
    else:
        raise ValueError("need a recipe path or --task")

    if only_agents:
        known = {a.name for a in recipe.agents}
        missing = [n for n in only_agents if n not in known]
        if missing:
            raise ValueError(f"unknown agents in recipe: {', '.join(missing)}")
        recipe = Recipe(
            name=recipe.name,
            backend=recipe.backend,
            max_parallel=recipe.max_parallel,
            agents=[a for a in recipe.agents if a.name in only_agents],
        )

    plan = plan_recipe(recipe, recipe_path if not task else None)
    if fmt == "json":
        print(render_json(plan))
    else:
        print(render_text(plan))
    return plan
