# Roadmap

## Quick wins (small, high value)

- [ ] `bake run --watch` — stream status until the run finishes, instead of
      polling `status` by hand.
- [x] `--format json` on `status` and `collect` for scripting (2026-09-09:
      `bake status --format json`, `bake collect --format json`; JSON collect
      embeds sanitized agent output — fences escaped, control chars stripped,
      secret-shaped values redacted, long output capped — because agent
      output is untrusted).
- [ ] Recipe validation errors that name the file, section, and agent index
      (already done at the Python level; surface them cleanly in the CLI).
- [x] `bake retry <run-id>` — re-run only agents that failed or timed out
      (2026-09-09: bakes a NEW run `<run-id>-retry<N>` from the frozen recipe
      filtered to failed/timed-out agents; the original run is never
      rewritten; refuses while the source run is still active).
- [x] `bake report <run-id>` merge mode — read a completed run's per-agent
      report files and emit one merged markdown report: deduped corroborated
      findings, conflicts flagged with agent attribution, per-agent detail
      preserved; missing report files noted, unfinished agents flagged.
- [x] `bake clean` — prune old run directories, keeping the last N (2026-09-09:
      `bake clean [--keep N] [--dry-run]`; active runs are never deleted and
      don't count against `--keep`; dirs without a readable meta.json are
      skipped, never touched).
- [ ] Agent `depends_on` — run the aggregation agent only after the swarm
      finishes (the org-audit "coordinator compile" step as a first-class agent).
- [ ] Timeout granularity: warn (SIGTERM) a few seconds before SIGKILL.

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
