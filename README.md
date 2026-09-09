# bakery

**Claude parallel swarm management.** Define a swarm of parallel agents as a recipe, bake them off at once, watch them converge, and collect their output into one report.

Bakery is a small, stdlib-only CLI for orchestrating parallel work — spawn N workers at once, track their status, kill runaways, and merge their results. No dependencies, no daemon, no cloud: a recipe file plus a run directory.

## Concepts

- **Recipe** — a TOML file describing a swarm: who runs, how many, what each agent does, timeouts, environment.
- **Agent** — one unit of parallel work. In the `shell` backend, an agent is a command run as a subprocess with its own stdout/stderr capture.
- **Run** — one execution of a recipe. Runs live under `.bakery/runs/<run-id>/` with per-agent logs, exit codes, and timing. A run is a directory, so it's inspectable with plain tools (`cat`, `jq`).

## Quickstart

```bash
# scaffold an example recipe
python -m bakery init

# bake off a swarm (returns immediately; agents run in background)
python -m bakery run hello.toml

# watch the swarm
python -m bakery status <run-id>

# per-agent logs
python -m bakery logs <run-id> analyzer-3

# merge everything into one report
python -m bakery collect <run-id> --format markdown

# kill a runaway swarm
python -m bakery kill <run-id>
```

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
                                   └── agents/
                                       ├── <name>.out  (stdout)
                                       ├── <name>.err  (stderr)
                                       └── <name>.code (exit code, written on finish)
```

The CLI is a thin orchestrator over the `shell` backend today. Backends are pluggable — see `docs/ARCHITECTURE.md` for the interface a new backend (Claude API agent, SSH worker, Docker container) needs to implement.

## Roadmap

Short-term: recipe validation errors with line numbers, `--watch` streaming status, JSON output mode for scripting, retry-on-failure. Longer-term: real agent backends and a web dashboard. Full list in `docs/ROADMAP.md`.

## License

MIT — see `LICENSE`.
