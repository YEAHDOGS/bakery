# Roadmap

## Shipped

- [x] `bake report` — one command: fan out a mission to agents, wait, collect, merge into a single markdown document (recipe or `--task` mode).
- [x] `fixture` backend — canned-report agents for offline end-to-end demos (no live AI calls, default-deny friendly).
- [x] Agent sandboxing: `sandbox_env()` choke point — agents inherit a scrubbed env allowlist, never credentials; secret-looking recipe env keys rejected. Threat model in `docs/THREAT-MODEL.md`.

## Quick wins (small, high value)

- [x] `bake run --watch` — stream status until the run finishes, instead of
      polling `status` by hand (runner.watch; also `bake watch <run-id>`).
- [x] `--format json` on `status` and `collect` for scripting — machine-readable
      swarm snapshots (state/exit_code/duration_s, internal pids excluded)
      and one JSON run document with every agent's captured output.
      (tests/test_json_output.py)
- [ ] Recipe validation errors that name the file, section, and agent index
      (already done at the Python level; surface them cleanly in the CLI).
- [ ] `bake retry <run-id>` — re-run only agents that failed or timed out.
- [ ] `bake clean` — prune old run directories, keeping the last N.
- [ ] Agent `depends_on` — run the aggregation agent only after the swarm
      finishes (the org-audit "coordinator compile" step as a first-class agent).
- [x] Timeout granularity: SIGTERM + a short grace period before
      SIGKILL, so cooperative agents flush final output and keep their
      real exit code (see `_terminate` in `bakery/runner.py`).

## Bigger bets

- [ ] **Claude backend** — agents that are real model calls: prompt template
      in, transcript out, with per-agent token/cost accounting in `collect`.
- [ ] **Swarm resume** — re-attach a supervisor to an existing run id after
      a crash instead of leaving agents orphaned.
- [ ] **Web dashboard** — tail agent logs in a browser; Brandon checks things
      on his phone, so this should be phone-first.
- [ ] **Recipe includes** — compose recipes from shared fragments
      (e.g. one fragment per repo in the org).
- [ ] **Dry run** — `bake plan` prints the launch waves without spawning.
- [ ] SSH and Docker backends for running swarms across machines (Castle
      nodes would be a natural fit).
