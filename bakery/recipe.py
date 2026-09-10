"""Recipe loading and validation.

A recipe is a TOML file:

    [bakery]
    name = "org-audit"
    backend = "shell"      # only backend implemented so far
    max_parallel = 7       # optional, default: all agents at once

    [[agents]]
    name = "repo-bakery"   # required, unique within the recipe
    cmd = ["bash", "-lc", "analyze-repo bakery"]   # required for shell backend
    timeout = 600          # optional seconds, default 3600
    timeout_grace = 5      # optional seconds of SIGTERM grace before SIGKILL, default 5
    depends_on = ["repo-bakery"]  # optional: launch only after these agents
                             # reach a terminal state (done/timeout/killed).
                             # The aggregation/merge agent is the classic use:
                             # N analyzers fan out, the aggregator waits.
    workdir = "."          # optional
    env = { FOO = "bar" }  # optional extra environment
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field


@dataclass
class AgentSpec:
    name: str
    cmd: list[str]
    timeout: int = 3600
    timeout_grace: int = 5  # SIGTERM warning window before SIGKILL on timeout
    depends_on: list = field(default_factory=list)  # agent names that must reach a terminal state first
    workdir: str = "."
    env: dict = field(default_factory=dict)


@dataclass
class Recipe:
    name: str
    backend: str
    max_parallel: int
    agents: list[AgentSpec]


def load_recipe(path: str) -> Recipe:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    try:
        head = data["bakery"]
        name = head["name"]
        backend = head.get("backend", "shell")
        max_parallel = int(head.get("max_parallel", 0)) or 0
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"recipe {path}: [bakery] section needs name/backend: {e}")

    raw_agents = data.get("agents")
    if not raw_agents:
        raise ValueError(f"recipe {path}: no [[agents]] defined")

    agents: list[AgentSpec] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_agents):
        aname = raw.get("name")
        cmd = raw.get("cmd")
        if not aname or not isinstance(aname, str):
            raise ValueError(f"recipe {path}: agents[{i}] needs a string 'name'")
        if aname in seen:
            raise ValueError(f"recipe {path}: duplicate agent name '{aname}'")
        seen.add(aname)
        if backend == "shell" and (not cmd or not isinstance(cmd, list)):
            raise ValueError(f"recipe {path}: agent '{aname}' needs cmd = [...] for shell backend")
        try:
            grace = int(raw.get("timeout_grace", 5))
        except (TypeError, ValueError):
            raise ValueError(f"recipe {path}: agent '{aname}' needs an integer 'timeout_grace'")
        if grace < 0:
            raise ValueError(f"recipe {path}: agent '{aname}' needs timeout_grace >= 0")
        deps = raw.get("depends_on", [])
        if deps is None:
            deps = []
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ValueError(f"recipe {path}: agent '{aname}' needs depends_on as a list of agent names")
        if aname in deps:
            raise ValueError(f"recipe {path}: agent '{aname}' cannot depend on itself")
        agents.append(
            AgentSpec(
                name=aname,
                cmd=list(cmd) if cmd else [],
                timeout=int(raw.get("timeout", 3600)),
                timeout_grace=grace,
                depends_on=list(deps),
                workdir=str(raw.get("workdir", ".")),
                env=dict(raw.get("env", {})),
            )
        )

    # Dependency graph validation: every name must exist and there must be no
    # cycles (a cycle would deadlock the supervisor — nothing could launch).
    by_name = {a.name: a for a in agents}
    for a in agents:
        for d in a.depends_on:
            if d not in by_name:
                raise ValueError(
                    f"recipe {path}: agent '{a.name}' depends on unknown agent '{d}'"
                )
    WHITE, GREY, BLACK = 0, 1, 2
    color = {a.name: WHITE for a in agents}

    def _visit(name: str, stack: list) -> None:
        color[name] = GREY
        for d in by_name[name].depends_on:
            if color[d] == GREY:
                cycle = " -> ".join(stack + [name, d])
                raise ValueError(f"recipe {path}: dependency cycle: {cycle}")
            if color[d] == WHITE:
                _visit(d, stack + [name])
        color[name] = BLACK

    for a in agents:
        if color[a.name] == WHITE:
            _visit(a.name, [])

    return Recipe(
        name=name,
        backend=backend,
        max_parallel=max_parallel or len(agents),
        agents=agents,
    )
