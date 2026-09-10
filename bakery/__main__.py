"""bakery CLI: bake off parallel swarms, watch them, collect the results."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import runner
from . import report as bake_report_mod
from . import scaffold
from . import audit as audit_mod


def cmd_report(args) -> None:
    """`bake report`: fan out a recipe, or merge a completed run's reports.

    The positional arg disambiguates by existence: a file path is treated as
    a recipe (fan out, wait, merge, deliver); an existing run id is treated
    as a merge target (`bake report <run-id>`).
    """
    arg = args.target
    if Path(arg).is_file():
        print(bake_report_mod.bake_report(arg, args.run_id, args.format, args.out, args.timeout), end="")
        return
    if (runner.runs_root() / arg).exists():
        report = bake_report_mod.merge_run(arg)
        if args.out:
            Path(args.out).write_text(report)
        print(report, end="")
        return
    raise SystemExit(f"'{arg}' is neither a recipe file nor a known run id")


def cmd_clean(args) -> None:
    result = runner.clean_runs(args.keep, dry_run=args.dry_run)
    verb = "would prune" if args.dry_run else "pruned"
    if result["deleted"]:
        print(f"{verb}: {', '.join(result['deleted'])}")
    else:
        print("nothing to prune")
    if result["skipped"]:
        print(f"skipped (no readable meta.json): {', '.join(result['skipped'])}")
    print(f"kept {result['kept']} run(s)")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="bakery", description="parallel swarm management")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="scaffold a fresh bakery project layout (idempotent)")
    p.add_argument("dir", nargs="?", default=".", help="directory to initialize (default: .)")
    p.add_argument("--dry-run", action="store_true", help="print the file tree, write nothing")
    p.set_defaults(fn=lambda a: cmd_init(a))

    p = sub.add_parser("run", help="bake off a swarm from a recipe (returns immediately)")
    p.add_argument("recipe", help="recipe TOML file")
    p.add_argument("--id", dest="run_id", default=None, help="run id (default: generated)")
    p.set_defaults(fn=lambda a: runner.start_run(a.recipe, a.run_id))

    p = sub.add_parser("list", help="list runs")
    p.set_defaults(fn=lambda a: runner.list_runs())

    p = sub.add_parser("status", help="show agent states for a run")
    p.add_argument("run_id")
    p.add_argument("--format", choices=["text", "json"], default="text",
                   help="table (default) or JSON for scripting")
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

    p = sub.add_parser("collect", help="merge a run's outputs into one report")
    p.add_argument("run_id")
    p.add_argument("--format", choices=["markdown", "text", "json"], default="markdown",
                   help="markdown/text, or sanitized JSON for scripting")
    p.set_defaults(fn=lambda a: runner.collect(a.run_id, a.format))

    p = sub.add_parser("report", help="fan out a recipe, or merge a completed run's reports")
    p.add_argument("target", metavar="recipe|run-id",
                   help="recipe TOML file (fan out, wait, merge, deliver) or a run id (merge that run's reports)")
    p.add_argument("--id", dest="run_id", default=None, help="run id (default: generated)")
    p.add_argument("--format", choices=["markdown", "text"], default="markdown",
                   help="fan-out mode only; merge mode always emits markdown")
    p.add_argument("--out", default=None, help="write the report to this file (default: stdout)")
    p.add_argument("--timeout", type=float, default=None, help="fan-out mode: overall wait deadline in seconds")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("retry", help="re-run only a run's failed/timed-out agents")
    p.add_argument("run_id")
    p.set_defaults(fn=lambda a: runner.retry_run(a.run_id))

    p = sub.add_parser("clean", help="prune old run directories, keeping the last N finished runs")
    p.add_argument("--keep", type=int, default=10, help="finished runs to keep (default: 10)")
    p.add_argument("--dry-run", action="store_true", help="list what would be pruned, delete nothing")
    p.set_defaults(fn=lambda a: cmd_clean(a))

    p = sub.add_parser("audit", help="show a run's audit trail (who did what, when)")
    p.add_argument("run_id")
    p.add_argument("--agent", default=None, help="show only events for this agent")
    p.add_argument("--event", default=None, help="show only events of this type")
    p.set_defaults(fn=lambda a: print(audit_mod.render_audit(a.run_id, a.agent, a.event), end=""))

    p = sub.add_parser("_supervise", help=argparse.SUPPRESS)
    p.add_argument("run_id")
    p.set_defaults(fn=lambda a: runner._supervise(a.run_id))

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
