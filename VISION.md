# Bakery — Vision

Bakery orchestrates parallel AI agents. The endgame is bigger than one model: **one command fans work out to Kite, Claude, and Gemini, then collects and merges their reports automatically.**

## The problem it solves

Right now the founder has to go to each AI individually — open Kite, open Claude, open Gemini — ask each for a report, then hand-carry the results back. Bakery kills that errand. You describe the mission once; bakery dispatches it, supervises the swarm, and hands you one merged report.

## Shape of the system

- **Heterogeneous agents.** Not just Claude subagents: Kite (this assistant), Claude, and Gemini as first-class workers behind one interface.
- **Fan-out / collect.** `bake run` dispatches the same brief (or split briefs) across agents; `bake collect` merges their outputs into a single markdown report — already working for Claude runs, extend to the others.
- **Supervisor.** Detached, wave-capped parallelism, per-agent timeouts, kill/log/status — already built. Extend supervision to remote agents (API-driven Claude/Gemini runs, not just local processes).

## Security (non-negotiable)

- Agents run sandboxed with least privilege. A worker never gets the founder's credentials, tokens, or private keys.
- Secrets are never pasted across agent boundaries. If a task needs a credential, the supervisor injects it at the narrowest scope or the task doesn't run.
- Every agent action is logged. `bake logs` should be able to answer "who did what, when" for any run.
- Treat cross-agent traffic as untrusted: validate and sanitize reports at collection time (prompt-injection is the sharpest failure when agents read each other's output).

## Inspiration (pending)

Brandon flagged a repo from an independent builder with a large library of agents and skills — link incoming. Evaluate it for: reusable agent definitions, skill patterns worth porting, and anything that should become Kite workspace skills.

## Near-term tasks

1. Define the agent adapter interface (local CLI agent vs. API agent share one contract).
2. Add Claude API and Gemini API worker adapters behind the same `run/collect` UX.
3. `bake report` — one command: fan out a brief to all configured agents, collect, merge, deliver.
4. Security pass: sandbox profiles, secret redaction in logs, audit trail.
