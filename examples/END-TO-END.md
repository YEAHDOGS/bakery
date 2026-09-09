# Worked end-to-end example: `bake report` with fixture backends

This is the full VISION.md loop running with zero network: one command fans
a mission to three agents, the supervisor runs them, and you get one merged
document. The agents here are **fixtures** (canned reports standing in for
live Kite/Claude/Gemini backends) — the orchestration, sandboxing, log
redaction, and merge layers are the real production code paths.

## Run it

From the repo root (no install — stdlib only):

```
./bake report examples/multi-ai-report.toml -o /tmp/report.md
```

Output:

```
baked run 20260909-112613-853005 (multi-ai-demo, 3 agents)
report: 3/3 agents done -> /tmp/report.md
```

`/tmp/report.md`:

```markdown
# Merged report

## Report: `kite`

# Kite — site performance pass
...per-agent findings, attributed...

## Report: `claude`

# Claude — accessibility pass
...

## Report: `gemini`

# Gemini — content pass
...
```

## The other entry points

No recipe file? A bare task builds a stub fixture swarm (stubs are clearly
labeled — they prove the plumbing, not the answers):

```
./bake report --task "audit the org repos" --agents kite,claude,gemini -o /tmp/report.md
```

Run only some agents from a recipe:

```
./bake report examples/multi-ai-report.toml --only kite,gemini -o /tmp/report.md
```

## What the security layers did during this run

- **Sandbox:** each fixture agent inherited only the scrubbed env allowlist
  (`bakery/sandbox.py`) — even with secrets in the operator's shell, the
  agents couldn't see them.
- **Redaction:** agent stdout/stderr passed through `redact()` before hitting
  disk (`.bakery/runs/<id>/agents/*.out`).
- **Injection hygiene:** `bake merge` sanitized the reports (ANSI/control
  chars stripped) and kept per-agent attribution.

## Going live

Swap `backend = "fixture"` for a real backend adapter and give each agent a
`cmd` (shell) or backend-specific spec instead of `report = ...`. The
`bake report` UX doesn't change — that's the point of the adapter contract.
```

## Recipe reference (`examples/multi-ai-report.toml`)

`backend = "fixture"`; each `[[agents]]` plays a canned `report = """..."""`
to stdout, optionally after `delay` seconds, exiting with `exit_code`.
`report_file = "..."` reads the canned report from disk instead.
