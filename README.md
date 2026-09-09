# bakery

**Claude parallel swarm management.** Define a swarm of parallel agents as a recipe, bake them off at once, watch them converge, and collect their output into one report.

Bakery is a small, stdlib-only CLI for orchestrating parallel work — spawn N workers at once, track their status, kill runaways, and merge their results. No dependencies, no daemon, no cloud: a recipe file plus a run directory.

The endgame (see `VISION.md`): **one command fans work out to Kite, Claude, and Gemini, then merges their reports automatically.** That's `bake report`.

## Concepts

- **Recipe** — a TOML file describing a swarm: who runs, how many, what each agent does, timeouts, environment.
- **Agent** — one unit of parallel work. In the `shell` backend, an agent is a command run as a subprocess with its own stdout/stderr capture. In the `fixture` backend, an agent plays a canned report (a stand-in for a live AI backend — no network).
- **Run** — one execution of a recipe. Runs live under `.bakery/runs/<run-id>/` with per-agent logs, exit codes, and timing. A run is a directory, so it's inspectable with plain tools (`cat`, `jq`).
- **Backend** — a spawn strategy behind the `Adapter` contract (`bakery/adapters.py`): `shell` (local commands), `fixture` (canned reports for offline demos). Claude/Gemini API adapters plug in here.

## Quickstart

```bash
# THE one command: fan out a mission, wait, collect, merge — one document
./bake report examples/multi-ai-report.toml -o /tmp/report.md

# or from a bare task (builds a labeled fixture-stub swarm, no recipe needed)
./bake report --task "audit the org repos" --agents kite,claude,gemini -o /tmp/report.md

# the classic detached workflow is still there:
./bake init
./bake run hello.toml            # returns immediately; agents run in background
./bake status <run-id>
./bake logs <run-id> analyzer-3
./bake collect <run-id> --format markdown
./bake kill <run-id>             # kill a runaway swarm
```

`./bake` is a tiny shim that finds the package from its own directory — works from any cwd, no install.

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

The CLI is a thin orchestrator over pluggable backends — see `docs/ARCHITECTURE.md` for the interface a new backend (Claude API agent, SSH worker, Docker container) needs to implement.

## Security

Agents are untrusted workers. Bakery enforces: **scrubbed environments** (agents never inherit the operator's credentials — secret-named env keys are stripped, secret-looking recipe env keys are rejected), **secret redaction** on every log byte before it hits disk, and **sanitize-at-merge** against cross-agent prompt injection. Full threat model in `docs/THREAT-MODEL.md`.

## Roadmap

Short-term: recipe validation errors with line numbers, `--watch` streaming status, JSON output mode for scripting, retry-on-failure. Longer-term: real agent backends and a web dashboard. Full list in `docs/ROADMAP.md`.

## License

MIT — see `LICENSE`.
