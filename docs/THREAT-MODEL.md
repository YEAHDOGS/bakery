# Bakery Threat Model

Agents are programs you don't fully control — today canned fixtures, tomorrow
Claude/Gemini API calls — and the vision's sharpest failure is an agent
turning on the founder or on the other agents. This doc states the threats,
what the code enforces, and what stays the operator's job.

## Actors and trust boundaries

| Actor | Trust level |
|---|---|
| Operator (Brandon) and recipe authors | Trusted — a recipe is code; review it before running |
| Supervisor (`bake run` / `bake report`) | Trusted — runs on the operator's machine with their env |
| Agent processes | **Untrusted** — sandboxed workers; assume their output is hostile |
| Agent stdout/stderr/logs | **Untrusted** — prompt-injection surface; sanitize at collection |
| Backend adapters | Trusted code, untrusted data — adapters run the backend's spawn strategy but never smuggle data |

## Threats and mitigations

### T1. Credential leakage via environment
A subprocess inherits the parent's full environment by default — including
API keys and tokens in the operator's shell. An agent that echoes `$ env`
would exfiltrate them.

**Enforced in code** (`bakery/sandbox.py`, wired in `runner.py`):
- `sandbox_env()` is the single choke point every spawn goes through.
- Secret-named keys (`API_KEY`, `SECRET`, `TOKEN`, `PASSWORD`, AWS/GITHUB
  patterns, ...) are stripped from the inherited environment — even when set
  in the operator's shell.
- Recipe-declared `env` keys with secret-looking names are **rejected with
  ValueError at `start_run`**, before the supervisor detaches — a recipe that
  tries to hand a token to an agent fails loudly instead of pretending.
- Agents see only an allowlist (`PATH`, `HOME`, `LANG`, `TZ`, ...); everything
  else is dropped. Adapters must treat this mapping as the child's *complete*
  environment and never merge in `os.environ` (stated in the adapter
  contract; `ShellAdapter` complies).

### T2. Credential leakage via logs
Agent output (transcripts, tool traces) can contain pasted secrets.

**Enforced in code** (`bakery/redact.py`, `runner._redacting_writer`):
- Every byte of agent stdout/stderr passes through `redact()` before it hits
  disk. Logs on disk are already scrubbed.

### T3. Cross-agent prompt injection
Agents read each other's output (merged reports, shared logs). A malicious
or compromised agent can plant instructions for the next reader.

**Enforced in code** (`bakery/merge.py`):
- `merge_reports` treats reports as untrusted: strips ANSI escapes and stray
  control characters, redacts secret patterns, never executes content.
- Merged sections carry per-agent attribution so a reader can see *who* said
  what.

### T4. Runaway / destructive agents
An agent that hangs, forks bombs, or must be stopped now.

**Enforced in code** (`runner.py`):
- Per-agent timeouts; on expiry the supervisor kills the whole **process
  group** (`killpg`), not just the pid — children can't escape.
- `bake kill` kills all groups and records the outcome; the supervisor's
  `finish()` won't clobber a kill record.
- Detached supervisor + `wait_for_run` liveness check: a dead supervisor is
  reported (`RuntimeError`) instead of hanging forever.

## What is NOT enforced (operator's job)

- **Recipes are trusted code.** A recipe author can still `cmd = ["bash", "-lc",
  "rm -rf ~"]`. Review recipes before running them; never run a recipe from
  an untrusted source.
- **Adapters are trusted code.** A malicious adapter could ignore the sandbox
  contract. Audit third-party adapters before `register()`ing them.
- **Network egress.** Bakery does not firewall agent processes today. The
  fixture backend needs none; future API adapters will need scoped, logged
  egress per backend (roadmap).
- **Multi-user hosts.** Runs assume a single-operator machine. `.bakery/runs`
  holds scrubbed-but-sensitive transcripts — keep the machine's own access
  controls sane.

## Security review checklist (every run)

1. `git log` the recipe and adapter changes since last run — new `cmd`,
   `env`, or `register()` calls get a second look.
2. Grep the tree for secret-looking literals (`sk-`, `ghp_`, `AKIA`, PEM
   blocks) — the redactor covers logs, not source.
3. Confirm no new network hosts: bakery builds with what's on the machine
   (Brandon's default-deny stands).
