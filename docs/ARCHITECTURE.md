# Architecture

## Moving parts

```
recipe.toml ──▶ bake run ──▶ supervisor (detached) ──▶ N agents
                      │
                      ▼
              .bakery/runs/<run-id>/
                ├── recipe.toml      frozen copy of the recipe
                ├── meta.json        run status + per-agent records
                ├── supervisor.log   supervisor's own stdout/stderr
                └── agents/
                    ├── <name>.out   agent stdout
                    ├── <name>.err   agent stderr
                    └── <name>.code  exit code, written on finish
```

- **CLI** (`bakery/__main__.py`) — parses commands, never blocks on agents.
- **Supervisor** (`runner._supervise`) — a detached process started by `bake run`.
  Launches agents in waves capped by `max_parallel`, polls each second,
  enforces per-agent timeouts with `killpg`, and records outcomes.
- **Run directory** — the entire state of a run is files. `status`, `logs`,
  `kill`, and `collect` are just readers/writers of `meta.json` and the
  `agents/` logs, so plain tools (`cat`, `jq`, `tail -f`) work on a live run.
- **Recipe** (`recipe.py`) — TOML in, validated `Recipe` out. TOML was chosen
  because it's stdlib-parseable (Python 3.11+) and human-friendly.
- **Secret redaction** (`redact.py`) — agent stdout/stderr is pumped through
  `redact()` line by line before it hits the log files, so tokens, `key=value`
  secrets, and PEM blocks land as `[REDACTED]`, never on disk. `bake merge`
  redacts report bodies too (reports are untrusted cross-agent input).

## Backend interface (`bakery/adapters.py`)

Only the `shell` adapter exists today. Every backend implements the `Adapter`
contract — subclass, set `name`, call `register()`:

| Operation | Meaning |
|---|---|
| `validate(spec)` | raise `ValueError` if this agent spec can't run on this backend |
| `spawn(spec, *, stdout, stderr, env) -> Popen` | start one agent, return its process handle. `stdout`/`stderr` are open writable file objects owned by the caller — never close them. |

The recipe loader resolves the backend through the adapter registry and fails
fast on unknown backends, naming the valid ones. Everything else — timeouts,
process-group kills, log files, `meta.json` — stays in the supervisor, so a
future Claude/Gemini API adapter only needs to implement spawn (prompt in,
transcript out) behind the same `bake run`/`bake collect` UX. The `shell`
adapter uses `subprocess.Popen(..., start_new_session=True)` so each agent
owns a process group and `killpg` reliably kills the agent and its children.

## Concurrency and race notes

- The supervisor and the CLI both write `meta.json`. Writes are
  read-modify-write at the single-agent granularity (`_update_agent`), and
  `finish()` refuses to overwrite a `killed` record left by `bake kill`.
- The supervisor is the only process that reaps agent processes; the CLI's
  `status` only checks pid liveness (`os.kill(pid, 0)`) and reports `unknown`
  if a pid vanished without the supervisor recording an outcome.
- A run whose supervisor dies mid-flight leaves agents running. `bake kill`
  still works (it kills by process group from `meta.json`), and `bake list`
  shows the run; re-running the supervisor for an existing run id is a
  roadmap item.
