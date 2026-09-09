"""bakery CLI: bake off parallel swarms, watch them, collect the results."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import findings, merge, plan, report, runner
from .config import STARTER_CONFIG

EXAMPLE_RECIPE = """\
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


def cmd_init(args) -> None:
    if args.config:
        dest = Path(args.path if args.path != "hello.toml" else "bake.yaml")
        if dest.exists():
            raise SystemExit(f"{dest} already exists")
        dest.write_text(STARTER_CONFIG)
        print(f"wrote starter config to {dest}")
        print("edit it, then: python -m bakery report recipe.toml")
        return
    dest = Path(args.path)
    if dest.exists():
        raise SystemExit(f"{dest} already exists")
    dest.write_text(EXAMPLE_RECIPE)
    print(f"wrote example recipe to {dest}")
    print("bake it off with: python -m bakery run", dest)


def _clean_cli_errors(prefix, fn):
    """Wrap a CLI dispatch so ValueErrors (recipe validation, bad CLI args)
    surface as one clean `<prefix>: <message>` line instead of a traceback.
    Recipe errors already name the file, section, and agent index."""
    def wrapped(a):
        try:
            return fn(a)
        except (ValueError, FileNotFoundError) as e:
            raise SystemExit(f"{prefix}: {e}")
    return wrapped


def cmd_run(args) -> None:
    # Recipe validation errors already name the file, section, and agent
    # index — surface them as one clean line, never a traceback.
    run_id = _clean_cli_errors("run", lambda a: runner.start_run(a.recipe, a.run_id))(args)
    if args.watch:
        _check_watched(run_id, args.timeout)


def _check_watched(run_id: str, timeout: float | None) -> None:
    meta = runner.watch(run_id, timeout=timeout)
    if meta["status"] == "killed":
        raise SystemExit(2)


def cmd_watch(args) -> None:
    _check_watched(args.run_id, args.timeout)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="bakery", description="parallel swarm management")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write an example recipe or starter config")
    p.add_argument("path", nargs="?", default="hello.toml")
    p.add_argument(
        "--config",
        action="store_true",
        help="write a commented starter bake.yaml instead of a recipe",
    )
    p.set_defaults(fn=lambda a: cmd_init(a))

    p = sub.add_parser("run", help="bake off a swarm from a recipe (returns immediately)")
    p.add_argument("recipe", help="recipe TOML file")
    p.add_argument("--id", dest="run_id", default=None, help="run id (default: generated)")
    p.add_argument(
        "--watch",
        action="store_true",
        help="stream status until the run finishes instead of returning immediately",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="with --watch: max seconds to wait before giving up",
    )
    p.set_defaults(fn=lambda a: cmd_run(a))

    p = sub.add_parser("watch", help="stream a run's status until it finishes")
    p.add_argument("run_id")
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="max seconds to wait before giving up",
    )
    p.set_defaults(fn=lambda a: cmd_watch(a))

    p = sub.add_parser("list", help="list runs")
    p.set_defaults(fn=lambda a: runner.list_runs())

    p = sub.add_parser("clean", help="delete old run directories, keeping the newest N")
    p.add_argument(
        "--keep",
        type=int,
        default=10,
        help="how many of the newest runs to keep (default: 10)",
    )
    p.set_defaults(fn=lambda a: runner.clean(a.keep))

    p = sub.add_parser("status", help="show agent states for a run")
    p.add_argument("run_id")
    p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="text: human-readable table; json: machine-readable snapshot",
    )
    p.set_defaults(fn=lambda a: runner.status(a.run_id, a.format))

    p = sub.add_parser("logs", help="print an agent's captured output")
    p.add_argument("run_id")
    p.add_argument("agent")
    p.add_argument("--err", action="store_true", help="show stderr instead of stdout")
    p.add_argument("--tail", type=int, default=0, help="last N lines only")
    p.set_defaults(fn=lambda a: runner.logs(a.run_id, a.agent, "err" if a.err else "out", a.tail))

    p = sub.add_parser("kill", help="kill every agent in a run")
    p.add_argument("run_id")
    p.set_defaults(fn=lambda a: runner.kill(a.run_id))

    p = sub.add_parser(
        "retry",
        help="re-run only the agents of a finished run that did not succeed",
    )
    p.add_argument("run_id")
    p.set_defaults(fn=lambda a: runner.retry(a.run_id))

    p = sub.add_parser("collect", help="merge a run's outputs into one report")
    p.add_argument("run_id")
    p.add_argument(
        "--format",
        choices=["markdown", "text", "json"],
        default="markdown",
        help="json: one machine-readable doc with run metadata + per-agent output",
    )
    p.set_defaults(fn=lambda a: runner.collect(a.run_id, a.format))

    p = sub.add_parser("merge", help="merge standalone report files into one document")
    p.add_argument("reports", nargs="+", help="report files to merge, in order")
    p.add_argument("-o", "--output", default=None, help="write merged doc to file (default: stdout)")
    p.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="json: structured finding-level merge of JSON reports "
             "(dedupe + provenance + disagreements), per bakery.findings",
    )

    def _merge_dispatch(a):
        if a.format == "json":
            try:
                findings.write_merged_json(a.reports, a.output)
            except findings.FindingsError as e:
                raise SystemExit(f"merge: {e}")
        else:
            merge.write_merged(a.reports, a.output)

    p.set_defaults(fn=_merge_dispatch)

    p = sub.add_parser("report", help="one command: fan out a mission to agents, collect, merge")
    p.add_argument("recipe", nargs="?", default=None, help="recipe TOML file (or use --task)")
    p.add_argument("--task", default=None, help="bare task string; builds a fixture stub recipe")
    p.add_argument("--agents", default=None, help="comma-separated agent names for --task")
    p.add_argument("--only", default=None, help="run only these recipe agents (comma-separated)")
    p.add_argument("-o", "--output", default=None, help="write merged doc to file (default: .bakery/runs/<id>/report.md)")
    p.add_argument("--timeout", type=float, default=None, help="max seconds to wait for the swarm")
    p.set_defaults(
        fn=_clean_cli_errors(
            "report",
            lambda a: report.bake_report(
                a.recipe,
                task=a.task,
                agent_names=a.agents.split(",") if a.agents else None,
                only_agents=a.only.split(",") if a.only else None,
                output=a.output,
                timeout=a.timeout,
            ),
        )
    )

    p = sub.add_parser(
        "plan",
        help="dry run: print the launch plan without spawning anything",
    )
    p.add_argument("recipe", nargs="?", default=None, help="recipe TOML file (or use --task)")
    p.add_argument("--task", default=None, help="bare task string; builds a fixture stub recipe")
    p.add_argument("--agents", default=None, help="comma-separated agent names for --task")
    p.add_argument("--only", default=None, help="plan only these recipe agents (comma-separated)")
    p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="text: human-readable waves; json: machine-readable plan document",
    )

    def _plan_dispatch(a):
        # Recipe validation errors surface as one clean line ("plan: ..."),
        # not a traceback — they already carry file, section, and agent index.
        return _clean_cli_errors(
            "plan",
            lambda x: plan.bake_plan(
                x.recipe,
                task=x.task,
                agent_names=x.agents.split(",") if x.agents else None,
                only_agents=x.only.split(",") if x.only else None,
                fmt=x.format,
            ),
        )(a)

    p.set_defaults(fn=_plan_dispatch)

    p = sub.add_parser("_supervise", help=argparse.SUPPRESS)
    p.add_argument("run_id")
    p.set_defaults(fn=lambda a: runner._supervise(a.run_id))

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
