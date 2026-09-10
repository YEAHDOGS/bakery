# bakery

**Claude parallel swarm management.** Define a swarm of parallel agents as a recipe, bake them off at once, watch them converge, and collect their output into one report.

Bakery is a small, stdlib-only CLI for orchestrating parallel work — spawn N workers at once, track their status, kill runaways, and merge their results. No dependencies, no daemon, no cloud: a recipe file plus a run directory.

## Concepts

- **Recipe** — a TOML file describing a swarm: who runs, how many, what each agent does, timeouts, environment.
- **Agent** — one unit of parallel work. In the `shell` backend, an agent is a command run as a subprocess with its own stdout/stderr capture.
- **Run** — one execution of a recipe. Runs live under `.bakery/runs/<run-id>/` with per-agent logs, exit codes, and timing. A run is a directory, so it's inspectable with plain tools (`cat`, `jq`).

## Quickstart

```bash
# scaffold a fresh bakery project (bakery.toml + .bakery/ tasks & notes)
python -m bakery init

# peek at what init would create, without writing anything
python -m bakery init --dry-run

# bake off the example task (returns immediately; agents run in background)
python -m bakery run .bakery/tasks/hello.toml

# watch the swarm
python -m bakery status <run-id>

# per-agent logs
python -m bakery logs <run-id> analyzer-3

# audit trail: who did what, when (filters: --agent, --event)
python -m bakery audit <run-id> --event agent.finished

The merge treats agent output as **untrusted**: fence-breaks are escaped,
ANSI/control characters stripped, secret-shaped values redacted
(`guard.redact_secrets`), and per-agent output length-capped — so one
malicious or chatty agent can't poison the merged report (see
`tests/test_report.py` for the hostile-recipe e2e test).

# merge everything into one report
python -m bakery collect <run-id> --format markdown

# one command: fan out, wait, merge a *sanitized* report, deliver
python -m bakery report .bakery/tasks/hello.toml --out report.md

# merge mode: <run-id> of a completed run -> one merged report
# (deduped corroborated findings, conflicts flagged with agent attribution,
#  per-agent detail preserved). Use for runs baked by other tools too.
python -m bakery report <run-id> --out merged.md

# retry just the failures of a finished run (new <run-id>-retryN run)
python -m bakery retry <run-id>

# prune old runs, keeping the last 10 finished runs (--dry-run to preview)
python -m bakery clean --keep 5 --dry-run

# kill a runaway swarm
python -m bakery kill <run-id>
```

## Project layout (`bake init`)

`bake init [dir]` scaffolds a fresh project and is idempotent — run it
twice and existing files are never overwritten, only reported as skipped:

```
my-project/
├── bakery.toml                        project config: named AI backends + merge strategy
└── .bakery/
    ├── tasks/hello.toml               example task file (a runnable swarm recipe)
    └── notes/agents-skills-eval.md    starter checklist for evaluating agent/skill repos
```

`bakery.toml` registers named backends (`shell`, `claude-cli`, `gemini-cli`
stubs — the shell backend is what runs today) and picks a
`merge_strategy` (`concat` today; `best-of`/`summarize` reserved for the LLM
backends). Pass `--dry-run` to preview the tree without writing anything.

## Example recipe

```toml
[bakery]
name = "org-audit"
backend = "shell"
max_parallel = 7

[[agents]]
name = "repo-bakery"
cmd = ["bash", "-lc", "analyze-repo bakery"]
timeout = 600
env = { ORG = "YEAHDOGS" }

[[agents]]
name = "repo-phoenix"
cmd = ["bash", "-lc", "analyze-repo phoenix"]
timeout = 600
```

See `examples/` for runnable recipes, including `org-audit.toml` — the parallel-audit pattern: N analyzers plus an aggregation step.

## Architecture

```
recipe.toml ──▶ bake run ──▶ .bakery/runs/<id>/
                                   ├── recipe.toml      (frozen copy)
                                   ├── meta.json        (pids, status, timing)
                                   ├── audit.jsonl      (append-only audit trail: who did what, when)
                                   └── agents/
                                       ├── <name>.out  (stdout)
                                       ├── <name>.err  (stderr)
                                       └── <name>.code (exit code, written on finish)
```

The CLI is a thin orchestrator over the `shell` backend today. Backends are pluggable — see `docs/ARCHITECTURE.md` for the interface a new backend (Claude API agent, SSH worker, Docker container) needs to implement.

## Roadmap

Short-term: recipe validation errors with line numbers, `--watch` streaming status. Shipped: JSON output mode for scripting (`bake status --format json`, `bake collect --format json`; the JSON collect embeds sanitized agent output), `bake retry <run-id>` — re-runs only the failed/timed-out agents of a finished run as a new `<run-id>-retry<N>` run, `bake clean [--keep N] [--dry-run]` — prune old run directories (active runs never deleted), and `bake audit <run-id>` — an append-only per-run audit trail answering "who did what, when" (env values never recorded, only names; supervisor secrets provably absent). Longer-term: real agent backends and a web dashboard. Full list in `docs/ROADMAP.md`.

## Security

- **Pre-flight secret guard.** `bake run` refuses a recipe that hands a
  secret-looking value (API tokens, private keys, `*_SECRET`-style env vars
  with real values) to an agent — it fails loudly before any agent spawns.
  Placeholders like `changeme` or `${API_KEY}` never trip it. If a task needs
  a credential, the supervisor injects it at the narrowest scope; it never
  goes in the recipe.
- **Least-privilege agent environment.** Agents no longer inherit the
  supervisor's full environment — `bakery.sandbox` builds a minimal one
  (PATH, HOME, LANG, ... plus the recipe's guard-vetted `env`) and drops
  everything else, including secret-shaped values. An exported token in your
  shell can't leak to a worker.
- **Audit trail.** Every run writes `<run-id>/audit.jsonl` — one JSON event
  per line for run start, agent launch (pid/pgid/timeout/command), agent
  finish/timeout, kill, retry, and run end. `bake audit <run-id>` renders it
  as a timeline (`--agent`, `--event` filters). The audit log never records
  env *values* — only names — so it can't become a secret leak of its own.

## License

MIT — see `LICENSE`.
