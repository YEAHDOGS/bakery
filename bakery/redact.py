"""Secret redaction for agent logs and merged reports.

Agents run sandboxed and never get the founder's credentials — but their
stdout/stderr can still leak secrets: an agent might `env | grep` debug
output, print a config file, or echo a token it was (wrongly) handed. This
module redacts the obvious shapes before they hit disk (`runner` pumps agent
output through it) and before reports are merged (`merge`).

It is a safety net, not a vault: patterns cover common secret shapes and
`key = value` assignments. Unknown formats won't be caught. Agents must still
never receive real credentials.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"


def _kv(match: re.Match) -> str:
    """Keep the key label so logs stay readable, drop the value."""
    return f"{match.group(1)}={REDACTED}"


_RULES: list[tuple[re.Pattern, str | object]] = [
    # AWS access key id
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED-AWS-KEY]"),
    # GitHub tokens
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"ghu_[A-Za-z0-9]{20,}"), REDACTED),
    # OpenAI-style keys
    (re.compile(r"sk-[A-Za-z0-9\-_]{16,}"), REDACTED),
    # Slack tokens
    (re.compile(r"xox[bpras]-[A-Za-z0-9\-]{10,}"), REDACTED),
    # Bearer tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/=]{10,}"), f"Bearer {REDACTED}"),
    # PEM private key blocks (multiline)
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED-PRIVATE-KEY]",
    ),
    # key=value / key: value secrets (env files, configs, CLI flags)
    (
        re.compile(
            r"""(?ix)\b
            (api[_-]?key|secret|passwd|password|auth[_-]?token|
             access[_-]?token|client[_-]?secret|private[_-]?key)\b
            \s*[:=]\s*["']?[^"'\s]+["']?"""
        ),
        _kv,
    ),
]


def redact(text: str) -> str:
    """Return `text` with recognized secret shapes replaced."""
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text
