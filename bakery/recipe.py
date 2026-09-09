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
    workdir = "."          # optional
    env = { FOO = "bar" }  # optional extra environment
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field

from . import adapters


@dataclass
class AgentSpec:
    name: str
    cmd: list[str]
    timeout: int = 3600
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

    try:
        adapter = adapters.get(backend)
    except ValueError as e:
        raise ValueError(f"recipe {path}: {e}")

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
        spec = AgentSpec(
            name=aname,
            cmd=list(cmd) if cmd else [],
            timeout=int(raw.get("timeout", 3600)),
            workdir=str(raw.get("workdir", ".")),
            env=dict(raw.get("env", {})),
        )
        try:
            adapter.validate(spec)
        except ValueError as e:
            raise ValueError(f"recipe {path}: {e}")
        agents.append(spec)

    return Recipe(
        name=name,
        backend=backend,
        max_parallel=max_parallel or len(agents),
        agents=agents,
    )
