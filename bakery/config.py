"""Per-run configuration: `bake.yaml`.

Found at, in order: `$BAKE_CONFIG` (explicit path), `./bake.yaml` (cwd),
`~/.config/bake/config.yaml`. Absent file = all defaults, no error.

Schema:

    timeout: 600                 # global default agent timeout, seconds
    retries: 1                   # extra attempts after a timeout (default 0)
    merge_strategy: concat       # concat | digest
    redact:                      # extra secret patterns (regex), on top of
      - 'internal-key-[A-Za-z0-9]{24}'   # the built-in redact() rules

    agents:                      # per-agent overrides, by agent name
      slow-auditor:
        timeout: 1800
        retries: 2

Every key is validated and EVERY unknown key fails loudly (typos in a
fan-out config must never silently misconfigure a run). Per-agent keys are
`timeout` / `retries` only. `merge_strategy` must be `concat` (full
per-agent sections) or `digest` (outcome lines + truncated bodies).

Timeout precedence, highest first:
    1. `bake report --timeout` CLI flag
    2. config `agents.<name>.timeout`
    3. recipe agent `timeout` (when the recipe sets it explicitly)
    4. config global `timeout`
    5. 3600s built-in default

Retry precedence: config `agents.<name>.retries` > config global `retries`
> 0. Retries are extra attempts *after* the first timeout of that agent.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 3600
MERGE_STRATEGIES = ("concat", "digest")

_TOP_LEVEL_KEYS = {"timeout", "retries", "merge_strategy", "redact", "agents"}
_AGENT_KEYS = {"timeout", "retries"}


class ConfigError(ValueError):
    """A bake.yaml that cannot be trusted — always loud, never silent."""


@dataclass
class AgentConfig:
    timeout: float | None = None
    retries: int | None = None


@dataclass
class Config:
    timeout: float | None = None        # global default, None = not set
    retries: int = 0                    # global retry count on timeout
    merge_strategy: str = "concat"
    redact: list[str] = field(default_factory=list)  # extra regex patterns
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    source: str = "<defaults>"          # which file produced this, for logs

    def timeout_for(self, name: str, recipe_timeout: float, recipe_timeout_set: bool,
                    cli_timeout: float | None = None) -> float:
        """Resolve one agent's effective timeout (see module docstring)."""
        if cli_timeout is not None:
            return cli_timeout
        override = self.agents.get(name)
        if override and override.timeout is not None:
            return override.timeout
        if recipe_timeout_set:
            return recipe_timeout
        if self.timeout is not None:
            return self.timeout
        return recipe_timeout

    def retries_for(self, name: str) -> int:
        """Extra attempts after this agent's first timeout."""
        override = self.agents.get(name)
        if override and override.retries is not None:
            return override.retries
        return self.retries


def _fail(path: str, msg: str) -> ConfigError:
    return ConfigError(f"{path}: {msg}")


def _check_timeout(path: str, where: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(path, f"{where}: timeout must be a number of seconds, got {value!r}")
    if value <= 0:
        raise _fail(path, f"{where}: timeout must be > 0, got {value!r}")
    return float(value)


def _check_retries(path: str, where: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(path, f"{where}: retries must be a non-negative integer, got {value!r}")
    if value < 0:
        raise _fail(path, f"{where}: retries must be >= 0, got {value!r}")
    return value


def parse_config(text: str, source: str = "<config>") -> Config:
    """Parse and validate bake.yaml text; unknown keys raise ConfigError."""
    try:
        import yaml
    except ImportError:
        raise _fail(source, "bake.yaml needs PyYAML (not installed)")
    try:
        data = yaml.safe_load(text)
    except Exception as e:
        raise _fail(source, f"not valid YAML: {e}")
    if data is None:
        return Config(source=source)
    if not isinstance(data, dict):
        raise _fail(source, "top level must be a mapping of config keys")

    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise _fail(
            source,
            f"unknown key(s): {', '.join(sorted(unknown))} "
            f"(valid: {', '.join(sorted(_TOP_LEVEL_KEYS))})",
        )

    cfg = Config(source=source)

    if "timeout" in data:
        cfg.timeout = _check_timeout(source, "timeout", data["timeout"])
    if "retries" in data:
        cfg.retries = _check_retries(source, "retries", data["retries"])
    if "merge_strategy" in data:
        strat = data["merge_strategy"]
        if strat not in MERGE_STRATEGIES:
            raise _fail(
                source,
                f"merge_strategy must be one of {MERGE_STRATEGIES}, got {strat!r}",
            )
        cfg.merge_strategy = strat
    if "redact" in data:
        pats = data["redact"]
        if not isinstance(pats, list) or any(not isinstance(p, str) for p in pats):
            raise _fail(source, "redact must be a list of regex strings")
        for p in pats:
            try:
                re.compile(p)
            except re.error as e:
                raise _fail(source, f"redact pattern {p!r} is not valid regex: {e}")
        cfg.redact = list(pats)
    if "agents" in data:
        raw_agents = data["agents"]
        if not isinstance(raw_agents, dict):
            raise _fail(source, "agents must be a mapping of agent name -> overrides")
        for name, raw in raw_agents.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                raise _fail(source, f"agents.{name}: must be a mapping of overrides")
            unknown_a = set(raw) - _AGENT_KEYS
            if unknown_a:
                raise _fail(
                    source,
                    f"agents.{name}: unknown key(s): {', '.join(sorted(unknown_a))} "
                    f"(valid: {', '.join(sorted(_AGENT_KEYS))})",
                )
            ac = AgentConfig()
            if "timeout" in raw:
                ac.timeout = _check_timeout(source, f"agents.{name}.timeout", raw["timeout"])
            if "retries" in raw:
                ac.retries = _check_retries(source, f"agents.{name}.retries", raw["retries"])
            cfg.agents[name] = ac

    return cfg


def find_config(start: Path | None = None) -> Path | None:
    """Locate the config file; None when there is no config anywhere."""
    explicit = os.environ.get("BAKE_CONFIG")
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise ConfigError(f"BAKE_CONFIG points at missing file: {p}")
        return p
    here = start or Path.cwd()
    local = here / "bake.yaml"
    if local.is_file():
        return local
    home_cfg = Path.home() / ".config" / "bake" / "config.yaml"
    if home_cfg.is_file():
        return home_cfg
    return None


def load_config(start: Path | None = None) -> Config:
    """Load the effective config; defaults when no bake.yaml exists."""
    path = find_config(start)
    if path is None:
        return Config()
    return parse_config(path.read_text(), source=str(path))


def apply_redact(cfg: Config) -> None:
    """Register the config's extra redact patterns in this process."""
    if cfg.redact:
        from . import redact as redact_mod

        redact_mod.register_extra(cfg.redact)


STARTER_CONFIG = """\
# bake.yaml — per-run configuration for the `bake` swarm runner.
# Location: ./bake.yaml (this directory), or ~/.config/bake/config.yaml.
# Unknown keys fail LOUDLY — a typo here aborts the run instead of
# silently misconfiguring your fan-out. Every key below is optional.

# Default per-agent timeout, in seconds. A recipe agent's own `timeout =`
# still wins unless an agents.<name>.timeout override below says otherwise.
timeout: 600

# Extra attempts after an agent times out (default 0 = give up on first
# timeout). Timed-out agents relaunch from scratch; their partial output
# is still flagged PARTIAL in the merged report.
retries: 1

# Merge strategy for `bake report` / `bake collect`:
#   concat — full per-agent report sections (default)
#   digest — outcome lines + first 20 lines of each report
merge_strategy: concat

# Extra secret patterns (regex), applied on top of bakery's built-in
# redaction rules. Use for org-specific token shapes.
redact:
  - 'internal-key-[A-Za-z0-9]{24}'

# Per-agent overrides, keyed by agent name from the recipe.
agents:
  slow-auditor:
    timeout: 1800   # this agent gets 30 minutes
    retries: 2      # and two extra attempts on timeout
"""
