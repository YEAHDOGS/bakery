"""Agent sandboxing: what an agent process is allowed to see.

The threat model (docs/THREAT-MODEL.md) says an agent must never receive the
founder's credentials. The most common leak path is the environment:
subprocess on POSIX inherits the parent's full `os.environ` by default —
including API keys, tokens, and passwords sitting in the supervisor's shell.

`sandbox_env()` is the single choke point. The runner calls it before every
agent spawn, and adapters treat the returned mapping as the agent's COMPLETE
environment (they add nothing). Guarantees:

- Secret-named keys are STRIPPED from the inherited environment, even when
  the supervisor's own shell has them set.
- Recipe-declared `env` keys with secret-looking names are REJECTED loudly
  (ValueError), not silently dropped — a recipe that tries to hand a token
  to an agent fails instead of pretending it didn't.
- Only a small allowlist of harmless keys (PATH, LANG, TZ, ...) carries over.
"""

from __future__ import annotations

import os
import re

# Keys that smell like credentials. Deliberately broad: a false positive
# (e.g. TOKEN_BUCKET_SIZE) rejects a recipe env key and the author renames it.
_SECRET_KEY_RE = re.compile(
    r"(?i)("
    r"api[_-]?key|"
    r"secret|"
    r"passwd|password|pwd|"
    r"token|"
    r"bearer|"
    r"credential|"
    r"private[_-]?key|"
    r"aws_access|aws_secret|"
    r"github_|"
    r"session[_-]?key"
    r")"
)

# Harmless environment keys an agent may inherit. Everything else is dropped.
_SAFE_BASE_KEYS = frozenset(
    {
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
        "TZ",
        "TMPDIR",
        "PYTHONHASHSEED",
        "PYTHONIOENCODING",
    }
)


def looks_secret(name: str) -> bool:
    """True if an env var name smells like a credential."""
    return bool(_SECRET_KEY_RE.search(name))


def sandbox_env(extra: dict | None = None) -> dict[str, str]:
    """Build the agent's complete environment.

    `extra` is the recipe-declared env for this agent. Raises ValueError if any
    of its keys looks secret-bearing. The returned mapping is everything the
    agent will see — adapters must not merge in more of the parent's env.
    """
    extra = extra or {}
    bad = sorted(k for k in extra if looks_secret(str(k)))
    if bad:
        raise ValueError(
            "recipe env may not hand credentials to agents; "
            f"secret-looking keys rejected: {', '.join(bad)}"
        )
    env = {k: os.environ[k] for k in _SAFE_BASE_KEYS if k in os.environ}
    for k, v in extra.items():
        env[str(k)] = str(v)
    return env
