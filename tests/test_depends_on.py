"""Regression tests for agent `depends_on` — dependency-ordered swarms.

ROADMAP quick win: an agent with `depends_on = [...]` only launches after
every named dependency reaches a terminal state (done/timeout/killed).
The classic use is the aggregation agent: N analyzers fan out in parallel,
then one merge step runs over their reports (the org-audit "coordinator
compile" step as a first-class agent).

Contract under test:
- recipe: `depends_on` parses to a name list, defaults to []; non-list or
  non-string values, self-dependencies, unknown names, and cycles are all
  rejected with errors that name the recipe, agent, and problem;
- runner: a dependent agent starts only after its deps are terminal — even
  when a dependency FAILED (failed deps still unblock, so the aggregator
  merges whatever the swarm produced);
- `bake status` renders a blocked agent as "waiting" (text) / "waiting_for"
  (JSON);
- `bake retry` keeps only deps that are also being retried in the frozen
  retry recipe (deps that finished in the original run need no waiting and
  would name agents absent from the retry's recipe).
"""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from bakery import runner
from bakery.recipe import load_recipe


HEAD = '[bakery]\nname = "deps"\nbackend = "shell"\nmax_parallel = 3\n\n'


def _recipe(path: Path, blocks: list[str]) -> str:
    p = path / "r.toml"
    p.write_text(HEAD + "\n".join(blocks))
    return str(p)


class DependsOnRecipeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_parses_and_defaults_to_empty(self):
        r = load_recipe(_recipe(self.dir, [
            '[[agents]]\nname = "a"\ncmd = ["true"]\ndepends_on = ["b"]\n',
            '[[agents]]\nname = "b"\ncmd = ["true"]\n',
        ]))
        by = {a.name: a for a in r.agents}
        self.assertEqual(by["a"].depends_on, ["b"])
        self.assertEqual(by["b"].depends_on, [])

    def test_non_list_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            load_recipe(_recipe(self.dir, [
                '[[agents]]\nname = "a"\ncmd = ["true"]\ndepends_on = "b"\n',
            ]))
        self.assertIn("depends_on", str(cm.exception))
        self.assertIn("'a'", str(cm.exception))

    def test_unknown_dependency_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            load_recipe(_recipe(self.dir, [
                '[[agents]]\nname = "a"\ncmd = ["true"]\ndepends_on = ["ghost"]\n',
            ]))
        self.assertIn("unknown agent 'ghost'", str(cm.exception))

    def test_self_dependency_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            load_recipe(_recipe(self.dir, [
                '[[agents]]\nname = "a"\ncmd = ["true"]\ndepends_on = ["a"]\n',
            ]))
        self.assertIn("cannot depend on itself", str(cm.exception))

    def test_cycle_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            load_recipe(_recipe(self.dir, [
                '[[agents]]\nname = "a"\ncmd = ["true"]\ndepends_on = ["b"]\n',
                '[[agents]]\nname = "b"\ncmd = ["true"]\ndepends_on = ["a"]\n',
            ]))
        self.assertIn("cycle", str(cm.exception))

    def test_longer_cycle_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            load_recipe(_recipe(self.dir, [
                '[[agents]]\nname = "a"\ncmd = ["true"]\ndepends_on = ["b"]\n',
                '[[agents]]\nname = "b"\ncmd = ["true"]\ndepends_on = ["c"]\n',
                '[[agents]]\nname = "c"\ncmd = ["true"]\ndepends_on = ["a"]\n',
            ]))
        self.assertIn("cycle", str(cm.exception))

    def test_diamond_is_accepted(self):
        r = load_recipe(_recipe(self.dir, [
            '[[agents]]\nname = "merge"\ncmd = ["true"]\ndepends_on = ["a", "b"]\n',
            '[[agents]]\nname = "a"\ncmd = ["true"]\n',
            '[[agents]]\nname = "b"\ncmd = ["true"]\ndepends_on = ["a"]\n',
        ]))
        by = {a.name: a for a in r.agents}
        self.assertEqual(by["merge"].depends_on, ["a", "b"])

    def test_dump_round_trips_depends_on(self):
        r = load_recipe(_recipe(self.dir, [
            '[[agents]]\nname = "merge"\ncmd = ["true"]\ndepends_on = ["a"]\n',
            '[[agents]]\nname = "a"\ncmd = ["true"]\n',
        ]))
        text = runner._dump_recipe_toml(r, r.agents)
        self.assertIn('depends_on = ["a"]', text)
        reloaded = load_recipe(str(self._write_frozen(text)))
        by = {a.name: a for a in reloaded.agents}
        self.assertEqual(by["merge"].depends_on, ["a"])

    def _write_frozen(self, text: str) -> Path:
        p = self.dir / "frozen.toml"
        p.write_text(text)
        return p


class _RunFixture(unittest.TestCase):
    """Self-contained e2e harness: tmp CWD + PYTHONPATH so the detached
    supervisor can re-exec `python -m bakery _supervise <run-id>`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.tmp.name)
        self.old_environ = dict(os.environ)
        self.addCleanup(os.environ.update, self.old_environ)
        self.addCleanup(os.environ.clear)
        repo_root = str(Path(__file__).resolve().parent.parent)
        old_pp = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = repo_root + (os.pathsep + old_pp if old_pp else "")

    def _wait_for_run(self, run_id: str, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            meta = json.loads((runner.runs_root() / run_id / "meta.json").read_text())
            if meta["status"] not in ("starting", "running"):
                return
            time.sleep(0.2)
        raise AssertionError(f"run {run_id} did not finish within {timeout}s")

    def _meta(self, run_id: str) -> dict:
        return json.loads((runner.runs_root() / run_id / "meta.json").read_text())


class DependsOnE2ETest(_RunFixture):
    def _recipe_path(self, blocks: list[str]) -> str:
        p = Path(self.tmp.name) / "deps.toml"
        p.write_text(HEAD + "\n".join(blocks))
        return str(p)

    def _sleep_agent(self, name: str, secs: float, extra: str = "") -> str:
        code = f"import time; time.sleep({secs})"
        return (
            f'[[agents]]\nname = "{name}"\n'
            f'cmd = ["python3", "-c", {json.dumps(code)}]\ntimeout = 60\n{extra}'
        )

    def test_aggregator_starts_only_after_deps_finish(self):
        # The aggregator is listed FIRST: without dependency enforcement it
        # would launch first. Its deps sleep, so any launch before they finish
        # proves the feature is broken.
        blocks = [
            self._sleep_agent("aggregator", 0.1, 'depends_on = ["a1", "a2"]\n'),
            self._sleep_agent("a1", 2),
            self._sleep_agent("a2", 2),
        ]
        run_id = "deps-order-1"
        runner.start_run(self._recipe_path(blocks), run_id)
        self._wait_for_run(run_id)
        meta = self._meta(run_id)
        states = meta["agents"]
        self.assertEqual({s["state"] for s in states.values()}, {"done"})
        agg_start = datetime.fromisoformat(states["aggregator"]["started_at"])
        for dep in ("a1", "a2"):
            dep_end = datetime.fromisoformat(states[dep]["finished_at"])
            self.assertGreaterEqual(
                agg_start, dep_end,
                f"aggregator started before {dep} finished — depends_on not enforced",
            )

    def test_failed_dependency_still_unblocks_dependent(self):
        blocks = [
            self._sleep_agent("aggregator", 0.1, 'depends_on = ["flaky"]\n'),
            '[[agents]]\nname = "flaky"\ncmd = ["bash", "-lc", "exit 3"]\ntimeout = 60\n',
        ]
        run_id = "deps-fail-1"
        runner.start_run(self._recipe_path(blocks), run_id)
        self._wait_for_run(run_id)
        meta = self._meta(run_id)
        self.assertEqual(meta["agents"]["flaky"]["state"], "done")
        self.assertEqual(meta["agents"]["flaky"]["exit_code"], 3)
        # The aggregator merges whatever exists — a failed dep must not wedge it.
        self.assertEqual(meta["agents"]["aggregator"]["state"], "done")

    def test_supervisor_log_notes_unblocking(self):
        blocks = [
            self._sleep_agent("aggregator", 0.1, 'depends_on = ["a1"]\n'),
            self._sleep_agent("a1", 0.5),
        ]
        run_id = "deps-log-1"
        runner.start_run(self._recipe_path(blocks), run_id)
        self._wait_for_run(run_id)
        log = (runner.runs_root() / run_id / "supervisor.log").read_text()
        self.assertIn("aggregator unblocked (deps: a1)", log)


class DependsOnStatusTest(_RunFixture):
    """`bake status` renders blocked agents as 'waiting' without racing the
    supervisor: the run dir is assembled by hand."""

    def _make_run(self, run_id: str) -> None:
        run_dir = runner.runs_root() / run_id
        (run_dir / "agents").mkdir(parents=True)
        (run_dir / "recipe.toml").write_text(
            HEAD
            + '[[agents]]\nname = "aggregator"\ncmd = ["true"]\ndepends_on = ["a1"]\n'
            + '[[agents]]\nname = "a1"\ncmd = ["true"]\n'
        )
        # refresh_status() liveness-checks running agents' pids, so a1 uses a
        # pid that is guaranteed alive: this test process itself.
        pid = os.getpid()
        meta = {
            "run_id": run_id,
            "recipe": "deps",
            "status": "running",
            "started_at": "2026-09-09T21:00:00+00:00",
            "finished_at": None,
            "retried_from": None,
            "agents": {
                "aggregator": {
                    "pid": None, "pgid": None, "state": "pending",
                    "exit_code": None, "started_at": None,
                    "finished_at": None, "duration_s": None,
                },
                "a1": {
                    "pid": pid, "pgid": pid, "state": "running",
                    "exit_code": None, "started_at": "2026-09-09T21:00:01+00:00",
                    "finished_at": None, "duration_s": None,
                },
            },
        }
        (run_dir / "meta.json").write_text(json.dumps(meta))

    def test_text_status_shows_waiting(self):
        self._make_run("deps-status-1")
        text = runner.render_status("deps-status-1")
        agg_line = next(l for l in text.splitlines() if l.startswith("aggregator"))
        self.assertIn("waiting", agg_line)
        a1_line = next(l for l in text.splitlines() if l.startswith("a1"))
        self.assertIn("running", a1_line)

    def test_json_status_has_waiting_for(self):
        self._make_run("deps-status-2")
        meta = json.loads(runner.render_status("deps-status-2", fmt="json"))
        self.assertEqual(meta["agents"]["aggregator"]["waiting_for"], ["a1"])
        self.assertNotIn("waiting_for", meta["agents"]["a1"])


class DependsOnRetryTest(_RunFixture):
    def _recipe_path(self, blocks: list[str]) -> str:
        p = Path(self.tmp.name) / "deps.toml"
        p.write_text(HEAD + "\n".join(blocks))
        return str(p)

    def test_retry_strips_deps_not_being_retried(self):
        # a1 succeeds, flaky fails, aggregator (depends on both) fails too.
        blocks = [
            '[[agents]]\nname = "a1"\ncmd = ["true"]\ntimeout = 60\n',
            '[[agents]]\nname = "flaky"\ncmd = ["bash", "-lc", "exit 3"]\ntimeout = 60\n',
            '[[agents]]\nname = "aggregator"\ncmd = ["bash", "-lc", "exit 4"]\n'
            'timeout = 60\ndepends_on = ["a1", "flaky"]\n',
        ]
        run_id = "deps-retry-1"
        runner.start_run(self._recipe_path(blocks), run_id)
        self._wait_for_run(run_id)
        new_id = runner.retry_run(run_id)
        self.assertIsNotNone(new_id)
        self._wait_for_run(new_id)
        frozen = (runner.runs_root() / new_id / "recipe.toml").read_text()
        # aggregator waits only on flaky (also retried); a1 already succeeded
        # and is absent from the retry's frozen recipe.
        self.assertIn('depends_on = ["flaky"]', frozen)
        self.assertNotIn('"a1"', frozen)
        reloaded = load_recipe(str(runner.runs_root() / new_id / "recipe.toml"))
        by = {a.name: a for a in reloaded.agents}
        self.assertEqual(set(by), {"flaky", "aggregator"})
        self.assertEqual(by["aggregator"].depends_on, ["flaky"])
        self.assertEqual(by["flaky"].depends_on, [])


if __name__ == "__main__":
    unittest.main()
