"""Agent adapters: every backend speaks one contract.

The vision needs heterogeneous workers — local CLI agents today, Claude/Gemini
API agents tomorrow — behind a single `bake run` / `bake collect` UX. An
adapter is the narrow seam where a backend-specific spawn strategy plugs in.

Contract::

    class MyAdapter(Adapter):
        name = "mybackend"

        def validate(self, spec) -> None:
            # raise ValueError if this spec can't run on this backend

        def spawn(self, spec, *, stdout, stderr, env) -> subprocess.Popen:
            # start the agent; stdout/stderr are open writable file objects
            # (owned by the caller — do NOT close them); env is the merged
            # environment mapping for this agent.

The caller owns everything else: timeouts, process-group kills, log files,
and meta.json bookkeeping. Adapters never touch credentials beyond what the
caller passes them in `env`.

SECURITY: the caller composes `env` via `bakery.sandbox.sandbox_env()`, which
strips the supervisor's credentials out of the inherited environment. The
adapter MUST use `env` as the child's complete environment — it must NOT
merge in `os.environ` itself, or the sandbox guarantee breaks.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path


class Adapter:
    """Base contract every backend implements."""

    name: str = "<unset>"

    def validate(self, spec) -> None:
        """Raise ValueError if this spec can't run on this backend."""
        return None

    def spawn(self, spec, *, stdout, stderr, env) -> subprocess.Popen:
        """Start the agent. Returns the child process handle."""
        raise NotImplementedError


class ShellAdapter(Adapter):
    """Local shell agents: `cmd` runs via subprocess in its own process group."""

    name = "shell"

    def validate(self, spec) -> None:
        if not spec.cmd or not isinstance(spec.cmd, list):
            raise ValueError(f"agent '{spec.name}' needs cmd = [...] for shell backend")

    def spawn(self, spec, *, stdout, stderr, env) -> subprocess.Popen:
        # `env` is already the sandboxed, complete environment (see module
        # docstring): use it as-is, never merge in os.environ.
        return subprocess.Popen(
            spec.cmd,
            stdout=stdout,
            stderr=stderr,
            cwd=spec.workdir,
            env={k: str(v) for k, v in env.items()},
            start_new_session=True,  # agent owns a process group: killpg is reliable
        )


class FixtureAdapter(Adapter):
    """Canned-report agents: stand-ins for real backends with no network.

    A fixture agent plays a canned report (inline `report = "..."` or a
    `report_file`) to stdout, optionally after `delay` seconds, and exits with
    `exit_code`. This is how you run the full bake-report pipeline end to end
    without live AI calls — each fixture stands in for one backend
    (kite / claude / gemini) while the orchestration, sandboxing, and merge
    layers exercise the same code paths as production.
    """

    name = "fixture"

    def validate(self, spec) -> None:
        if not spec.report and not spec.report_file:
            raise ValueError(
                f"agent '{spec.name}' needs report = \"...\" or report_file = \"...\" "
                "for the fixture backend"
            )
        if spec.delay < 0:
            raise ValueError(f"agent '{spec.name}': delay must be >= 0")
        if spec.report_file and not Path(spec.report_file).is_file():
            raise ValueError(
                f"agent '{spec.name}': report_file '{spec.report_file}' not found"
            )

    def spawn(self, spec, *, stdout, stderr, env) -> subprocess.Popen:
        if spec.report:
            text = spec.report
        else:
            text = Path(spec.report_file).read_text(errors="replace")
        # Write the canned report to a temp file and `cat` it: avoids shell
        # quoting games with arbitrary report text. Temp file is removed
        # by the agent process itself after reading.
        fd, tmp = tempfile.mkstemp(prefix=f"fixture-{spec.name}-", suffix=".md")
        os.write(fd, text.encode("utf-8", errors="replace"))
        os.close(fd)
        script = (
            f"sleep {int(spec.delay)}; "
            f"cat {shlex.quote(tmp)}; "
            f"rc={int(spec.exit_code)}; "
            f"rm -f {shlex.quote(tmp)}; "
            f"exit $rc"
        )
        return subprocess.Popen(
            ["bash", "-c", script],
            stdout=stdout,
            stderr=stderr,
            cwd=spec.workdir,
            env={k: str(v) for k, v in env.items()},
            start_new_session=True,
        )


_ADAPTERS: dict[str, Adapter] = {}


def register(adapter: Adapter) -> None:
    """Plug a backend into the registry (third-party backends use this)."""
    if not adapter.name or adapter.name == Adapter.name:
        raise ValueError("adapter needs a real name")
    _ADAPTERS[adapter.name] = adapter


def get(backend: str) -> Adapter:
    """Return the adapter for `backend`, or raise naming the valid ones."""
    try:
        return _ADAPTERS[backend]
    except KeyError:
        valid = ", ".join(sorted(_ADAPTERS)) or "(none registered)"
        raise ValueError(f"unknown backend '{backend}' (valid: {valid})")


register(ShellAdapter())
register(FixtureAdapter())
