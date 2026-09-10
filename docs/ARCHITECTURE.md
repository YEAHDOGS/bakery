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
  enforces per-agent timeouts in two phases — SIGTERM warning, then SIGKILL
  after `timeout_grace` seconds (recipe-configurable, default 5) — and records outcomes.
- **Run directory** — the entire state of a run is files. `status`, `logs`,
  `kill`, and `collect` are just readers/writers of `meta.json` and the
  `agents/` logs, so plain tools (`cat`, `jq`, `tail -f`) work on a live run.
- **Recipe** (`recipe.py`) — TOML in, validated `Recipe` out. TOML was chosen
  because it's stdlib-parseable (Python 3.11+) and human-friendly.

## Backend interface

Only the `shell` backend exists today. A backend needs to provide:

| Operation | Meaning |
|---|---|
| `spawn(agent) -> handle` | start one agent, return an opaque handle |
| `poll(handle) -> state` | `running` / `done(exit_code)` |
| `stop(handle)` | terminate the agent and its children |
| `capture_paths(handle)` | where stdout/stderr/exit code live |

The `shell` backend implements this with `subprocess.Popen(...,
start_new_session=True)` so each agent owns a process group — `killpg`
reliably kills the agent and everything it spawned. Candidates for future
backends: a Claude API agent (prompt in, transcript out), SSH workers, Docker
containers. Each would live in `bakery/backend_<name>.py` behind this table.

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
