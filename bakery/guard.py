"""Pre-flight secret guard: never hand an agent a credential.

VISION.md (Security, non-negotiable): a worker never gets the founder's
credentials, tokens, or private keys. The guard scans a recipe's per-agent
`env` values and `cmd` arguments for secret-looking material and refuses to
start the run when one is found. It runs at the dispatch boundary
(`runner.start_run`), so a bad recipe fails loudly before any agent spawns.

Detection is deliberately value-shaped and conservative: only strings that
look like real tokens are flagged. Obvious placeholders ("changeme",
"example", "${...}") always pass, so documenting a recipe with a sample key
never trips the guard.

If a task genuinely needs a credential, the answer is not to put it in the
recipe — the supervisor should inject it at the narrowest scope, or the task
doesn't run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .recipe import Recipe


@dataclass(frozen=True)
class SecretFinding:
    agent: str
    source: str  # e.g. "env:API_KEY" or "cmd"
    kind: str  # which pattern matched


# Value-shaped patterns: only match strings long/specific enough to be real.
_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws-access-key-id"),
    (re.compile(r"(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "github-token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github-fine-grained-pat"),
    (re.compile(r"sk-[A-Za-z0-9\-_]{20,}"), "openai-style-api-key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "slack-token"),
    (re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/=]{20,}"), "bearer-token"),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "pem-private-key"),
]

# Env var names that, by themselves, say "I hold a secret". Flagged only when
# the value is long enough to be real and not an obvious placeholder.
_SECRET_NAME = re.compile(
    r"(password|passwd|secret|api[_-]?key|auth[_-]?token|access[_-]?token|"
    r"private[_-]?key|client[_-]?secret|session[_-]?token)$",
    re.IGNORECASE,
)
_PLACEHOLDERS = ("changeme", "example", "placeholder", "sample", "test", "xxx", "none", "null", "todo", "your-key")


def _looks_like_placeholder(value: str) -> bool:
    v = value.strip().lower()
    if not v or v in _PLACEHOLDERS or v.startswith("${"):
        return True
    return any(p in v for p in _PLACEHOLDERS)


def _scan_value(value: str) -> str | None:
    """Return the matched pattern kind, or None."""
    for pattern, kind in _VALUE_PATTERNS:
        if pattern.search(value):
            return kind
    return None


def find_secrets(recipe: Recipe) -> list[SecretFinding]:
    """Scan a recipe for secret-looking material handed to agents."""
    findings: list[SecretFinding] = []
    for agent in recipe.agents:
        for key, val in agent.env.items():
            val = str(val)
            kind = _scan_value(val)
            if kind is None and _SECRET_NAME.search(key) and len(val) >= 12 and not _looks_like_placeholder(val):
                kind = f"secret-bearing-env:{key}"
            if kind:
                findings.append(SecretFinding(agent.name, f"env:{key}", kind))
        for arg in agent.cmd:
            kind = _scan_value(str(arg))
            if kind:
                findings.append(SecretFinding(agent.name, "cmd", kind))
    return findings


def guard_recipe(recipe: Recipe) -> None:
    """Refuse to dispatch a recipe that hands secrets to agents."""
    findings = find_secrets(recipe)
    if not findings:
        return
    lines = [
        f"recipe '{recipe.name}': refusing to bake — {len(findings)} secret-looking value(s) handed to agents:",
    ]
    for f in findings:
        lines.append(f"  - agent '{f.agent}' {f.source}: {f.kind}")
    lines.append(
        "Agents must never receive credentials. Remove the secret from the recipe; "
        "if a task needs one, the supervisor injects it at the narrowest scope instead."
    )
    raise SystemExit("\n".join(lines))


def redact_secrets(text: str) -> str:
    """Redact secret-looking values from free text (logs, merged reports).

    Cross-agent traffic is untrusted (VISION.md): an agent that echoes a
    credential — its own or a leaked one — must not have that credential
    land in a merged report or log verbatim. Each match is replaced with a
    labelled placeholder naming the pattern kind.
    """
    redacted = text
    for pattern, kind in _VALUE_PATTERNS:
        redacted = pattern.sub(f"[redacted:{kind}]", redacted)
    return redacted
