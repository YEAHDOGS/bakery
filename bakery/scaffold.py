"""`bake init`: scaffold a fresh bakery project layout (idempotent).

Layout created under the target directory::

    bakery.toml                       project config: named AI backends + merge strategy
    .bakery/tasks/hello.toml          example task file (a runnable swarm recipe)
    .bakery/notes/agents-skills-eval.md   starter notes for evaluating agent/skill repos

Init never overwrites: existing files are left byte-for-byte identical and
reported as skipped. Run it twice, edit the config, run it again — your edits
survive. ``--dry-run`` prints the tree it would create and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CONFIG_TOML = """\
# bakery.toml — bakery project config.
#
# [bakery] names the project and picks how `bake collect`/`bake report`
# merge agent outputs into one report.
#
# merge_strategy: how to combine agent outputs
#   "concat"     — concatenate, newest-first by finish time (default)
#   "best-of"    — keep only the highest-scoring output (needs a scorer)
#   "summarize"  — ask a summarizer backend to distill (needs an LLM backend)
#
# [backends.<name>] registers a named AI backend. Agents in task files pick one
# via `backend = "<name>"`. The shell backend runs local commands; real LLM
# backends (claude, gemini, ...) plug in here as they land.

[bakery]
name = "my-project"
merge_strategy = "concat"

[backends.shell]
kind = "shell"
# Local subprocess backend — what runs today.

[backends.claude-cli]
kind = "claude-cli"
model = "sonnet"
# Example future backend: the Claude CLI as a worker.

[backends.gemini-cli]
kind = "gemini-cli"
model = "flash"
# Example future backend: the Gemini CLI as a worker.

[defaults]
backend = "shell"
max_parallel = 4
timeout = 300
"""

TASK_HELLO_TOML = """\
# hello.toml — example bakery task file: a small swarm of local agents.
# Bake it off with:  python -m bakery run .bakery/tasks/hello.toml
[bakery]
name = "hello-swarm"
backend = "shell"
max_parallel = 3

[[agents]]
name = "agent-1"
cmd = ["bash", "-lc", "echo hello from agent 1; sleep 1; echo done"]
timeout = 60

[[agents]]
name = "agent-2"
cmd = ["bash", "-lc", "echo hello from agent 2; sleep 2; echo done"]
timeout = 60

[[agents]]
name = "agent-3"
cmd = ["bash", "-lc", "echo hello from agent 3; sleep 1; echo done"]
timeout = 60
"""

EVAL_NOTES_MD = """\
# Agents / Skills evaluation notes

Starter checklist for evaluating an external agents/skills repo before wiring
it into bakery. Score each 0-2; anything scoring 0 on a security line is a
hard no.

## Capability
- [ ] Does it do something bakery can't already do with the shell backend?
- [ ] Are its inputs/outputs cleanly separable (JSON in, JSON out)?
- [ ] Timeout behavior documented? What happens on hang?

## Security (hard requirements)
- [ ] No network exfiltration of task content (or: which hosts, and why?)
- [ ] No credential handling — skills must never ask for or store secrets
- [ ] Treats tool/agent output as untrusted (injection-safe prompts/templates)
- [ ] License allows reuse; no opaque binary blobs

## Operability
- [ ] Install story: stdlib only, or pinned deps with hashes?
- [ ] Works headless / air-gapped?
- [ ] Error modes are observable (exit codes, logs, not silent corruption)

## Notes
_Candidate repos and findings go here._
"""


@dataclass(frozen=True)
class InitResult:
    created: tuple[str, ...]
    skipped: tuple[str, ...]
    dry_run: bool


# relpath -> file content, in tree order
FILES: dict[str, str] = {
    "bakery.toml": CONFIG_TOML,
    ".bakery/tasks/hello.toml": TASK_HELLO_TOML,
    ".bakery/notes/agents-skills-eval.md": EVAL_NOTES_MD,
}


def init_project(target: str | Path = ".", dry_run: bool = False) -> InitResult:
    """Create the bakery project layout under *target*.

    Idempotent: files that already exist are never touched, only reported
    as skipped. With ``dry_run=True`` nothing is written at all.
    """
    root = Path(target)
    created: list[str] = []
    skipped: list[str] = []
    for rel, content in FILES.items():
        path = root / rel
        if path.exists():
            skipped.append(rel)
            print(f"skip:    {rel} (already exists)")
            continue
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        created.append(rel)
        print(("would create" if dry_run else "created") + f": {rel}")
    if dry_run:
        print("dry-run: nothing written")
    elif not created and skipped:
        print("nothing to do: project already initialized")
    return InitResult(tuple(created), tuple(skipped), dry_run)
