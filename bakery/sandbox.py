"""Agent sandboxing: least-privilege execution environment.

VISION.md (Security, non-negotiable): agents run sandboxed with least
privilege — a worker never gets the founder's credentials, tokens, or
private keys.

The shell backend used to hand every agent `os.environ.copy()`: the
supervisor's full environment, including any exported tokens or keys
(GITHUB_TOKEN, ANTHROPIC_API_KEY, ...). The pre-flight guard
(`bakery.guard`) only vets values written in the recipe, so a secret that
reached the supervisor through its own environment leaked straight through
to every agent.

`sandboxed_env` builds the minimal environment instead: a small allowlist
of known-safe variables (PATH, HOME, LANG, ...) plus the recipe's own
`env` (already guard-vetted at dispatch). Everything else is dropped.
Allowlisted values are still screened against the guard's token patterns —
belt and suspenders, in case someone exported a token under a benign name.

If a task genuinely needs a credential, the answer is not to export it in
the supervisor's shell and hope: the supervisor should inject it at the
narrowest scope for that agent, or the task doesn't run.
"""

from __future__ import annotations

from collections.abc import Mapping

from .guard import _scan_value

# Variables an agent may inherit from the supervisor's environment. These
# are operational (where things live, what locale, which terminal) rather
# than secrets. Kept deliberately tight.
SAFE_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_NUMERIC",
    "LC_TIME",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "HOSTNAME",
)


def sandboxed_env(
    parent_env: Mapping[str, str],
    recipe_env: Mapping[str, object],
) -> dict[str, str]:
    """Build the least-privilege environment for one agent.

    - Copies only allowlisted names from the parent (supervisor) environment,
      skipping any allowlisted value that is itself secret-shaped.
    - Layers the recipe's per-agent `env` on top (explicit and guard-vetted;
      wins on name conflicts).
    - Drops everything else.
    """
    env: dict[str, str] = {}
    for name in SAFE_ENV_ALLOWLIST:
        value = parent_env.get(name)
        if value is None:
            continue
        if _scan_value(value) is not None:
            continue  # secret-shaped value, even under a benign name: drop it
        env[name] = value
    for key, val in recipe_env.items():
        env[str(key)] = str(val)
    return env
