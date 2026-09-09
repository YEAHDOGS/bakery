"""Tests for bake.yaml per-run configuration (bakery/config.py)."""

import os
import tempfile
import unittest
from pathlib import Path

from bakery import config
from bakery.config import Config, ConfigError, parse_config


class ParseTest(unittest.TestCase):
    def test_empty_is_defaults(self):
        cfg = parse_config("", source="t")
        self.assertEqual(cfg.timeout, None)
        self.assertEqual(cfg.retries, 0)
        self.assertEqual(cfg.merge_strategy, "concat")
        self.assertEqual(cfg.redact, [])
        self.assertEqual(cfg.agents, {})

    def test_full_config(self):
        cfg = parse_config(
            """
timeout: 600
retries: 1
merge_strategy: digest
redact:
  - 'internal-key-[A-Za-z0-9]{24}'
agents:
  slow:
    timeout: 1800
    retries: 2
"""
        )
        self.assertEqual(cfg.timeout, 600)
        self.assertEqual(cfg.retries, 1)
        self.assertEqual(cfg.merge_strategy, "digest")
        self.assertEqual(cfg.redact, ["internal-key-[A-Za-z0-9]{24}"])
        self.assertEqual(cfg.agents["slow"].timeout, 1800)
        self.assertEqual(cfg.agents["slow"].retries, 2)

    def test_starter_config_is_valid(self):
        cfg = parse_config(config.STARTER_CONFIG)
        self.assertEqual(cfg.timeout, 600)
        self.assertEqual(cfg.retries, 1)
        self.assertEqual(cfg.merge_strategy, "concat")
        self.assertEqual(cfg.agents["slow-auditor"].timeout, 1800)


class LoudFailureTest(unittest.TestCase):
    def test_unknown_top_level_key(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config("timeot: 600\n")
        self.assertIn("timeot", str(ctx.exception))
        self.assertIn("unknown key", str(ctx.exception))

    def test_unknown_agent_key(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config("agents:\n  a:\n    timeut: 5\n")
        self.assertIn("timeut", str(ctx.exception))

    def test_bad_merge_strategy(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config("merge_strategy: smart\n")
        self.assertIn("smart", str(ctx.exception))

    def test_bad_regex(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config("redact:\n  - '[unclosed'\n")
        self.assertIn("regex", str(ctx.exception))

    def test_negative_timeout(self):
        with self.assertRaises(ConfigError):
            parse_config("timeout: -5\n")

    def test_non_int_retries(self):
        with self.assertRaises(ConfigError):
            parse_config("retries: 1.5\n")

    def test_non_mapping_top_level(self):
        with self.assertRaises(ConfigError):
            parse_config("- just\n- a\n- list\n")

    def test_invalid_yaml(self):
        with self.assertRaises(ConfigError):
            parse_config("timeout: [unclosed\n")


class PrecedenceTest(unittest.TestCase):
    def test_cli_flag_wins(self):
        # --timeout is the waiter's deadline, never a per-agent timeout:
        # it must not move agent resolution at all.
        cfg = parse_config("timeout: 600\nagents:\n  a:\n    timeout: 900\n")
        self.assertEqual(cfg.timeout_for("a", 60, True), 900)

    def test_agent_override_beats_recipe_and_global(self):
        cfg = parse_config("timeout: 600\nagents:\n  a:\n    timeout: 900\n")
        self.assertEqual(cfg.timeout_for("a", 60, True), 900)

    def test_explicit_recipe_timeout_beats_global(self):
        cfg = parse_config("timeout: 600\n")
        self.assertEqual(cfg.timeout_for("a", 60, True), 60)

    def test_global_timeout_replaces_default(self):
        cfg = parse_config("timeout: 600\n")
        self.assertEqual(cfg.timeout_for("a", 3600, False), 600)

    def test_default_when_nothing_set(self):
        cfg = Config()
        self.assertEqual(cfg.timeout_for("a", 3600, False), 3600)

    def test_retries_precedence(self):
        cfg = parse_config("retries: 1\nagents:\n  a:\n    retries: 3\n")
        self.assertEqual(cfg.retries_for("a"), 3)
        self.assertEqual(cfg.retries_for("other"), 1)
        self.assertEqual(Config().retries_for("x"), 0)


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old_cwd = os.getcwd()
        self.old_home = os.environ.get("HOME")
        self.old_bake = os.environ.pop("BAKE_CONFIG", None)
        os.environ["HOME"] = str(self.tmp)
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.old_cwd)
        if self.old_home is not None:
            os.environ["HOME"] = self.old_home
        if self.old_bake is not None:
            os.environ["BAKE_CONFIG"] = self.old_bake

    def test_no_config_is_defaults(self):
        cfg = config.load_config()
        self.assertEqual(cfg.source, "<defaults>")

    def test_cwd_bake_yaml_found(self):
        (self.tmp / "bake.yaml").write_text("timeout: 111\n")
        cfg = config.load_config()
        self.assertEqual(cfg.timeout, 111)
        self.assertTrue(cfg.source.endswith("bake.yaml"))

    def test_cwd_beats_home(self):
        home_cfg = self.tmp / ".config" / "bake"
        home_cfg.mkdir(parents=True)
        (home_cfg / "config.yaml").write_text("timeout: 222\n")
        (self.tmp / "bake.yaml").write_text("timeout: 111\n")
        self.assertEqual(config.load_config().timeout, 111)

    def test_home_config_used_when_no_cwd(self):
        home_cfg = self.tmp / ".config" / "bake"
        home_cfg.mkdir(parents=True)
        (home_cfg / "config.yaml").write_text("timeout: 222\n")
        self.assertEqual(config.load_config().timeout, 222)

    def test_bake_config_env_wins(self):
        other = self.tmp / "custom.yaml"
        other.write_text("timeout: 333\n")
        (self.tmp / "bake.yaml").write_text("timeout: 111\n")
        os.environ["BAKE_CONFIG"] = str(other)
        self.assertEqual(config.load_config().timeout, 333)

    def test_bake_config_missing_file_is_loud(self):
        os.environ["BAKE_CONFIG"] = str(self.tmp / "nope.yaml")
        with self.assertRaises(ConfigError):
            config.load_config()


class RedactHookTest(unittest.TestCase):
    def tearDown(self):
        from bakery import redact

        redact.clear_extra()

    def test_apply_redact_registers_patterns(self):
        from bakery import redact

        cfg = parse_config("redact:\n  - 'internal-key-[A-Za-z0-9]{8}'\n")
        config.apply_redact(cfg)
        self.assertEqual(redact.redact("leak internal-key-ABCDEFGH here"), "leak [REDACTED] here")

    def test_apply_redact_no_patterns_is_noop(self):
        from bakery import redact

        config.apply_redact(Config())
        self.assertEqual(redact.redact("nothing to hide"), "nothing to hide")

    def test_register_dedupes(self):
        from bakery import redact

        config.apply_redact(parse_config("redact:\n  - 'zz-[0-9]+'\n"))
        config.apply_redact(parse_config("redact:\n  - 'zz-[0-9]+'\n"))
        self.assertEqual(len(redact._EXTRA_RULES), 1)


if __name__ == "__main__":
    unittest.main()


class IsolatedCwdTest(unittest.TestCase):
    """chdir + HOME isolation so bake.yaml discovery can't leak between tests."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old_cwd = os.getcwd()
        self.old_home = os.environ.get("HOME")
        self.old_bake = os.environ.pop("BAKE_CONFIG", None)
        os.environ["HOME"] = str(self.tmp)
        os.chdir(self.tmp)
        from bakery import redact

        redact.clear_extra()

    def tearDown(self):
        os.chdir(self.old_cwd)
        if self.old_home is not None:
            os.environ["HOME"] = self.old_home
        else:
            os.environ.pop("HOME", None)
        if self.old_bake is not None:
            os.environ["BAKE_CONFIG"] = self.old_bake
        else:
            os.environ.pop("BAKE_CONFIG", None)
        from bakery import redact

        redact.clear_extra()

    def _recipe(self, path: str = "r.toml", timeout_line: str = "") -> str:
        p = self.tmp / path
        p.write_text(
            '[bakery]\nname = "cfg-e2e"\nbackend = "fixture"\nmax_parallel = 2\n\n'
            '[[agents]]\nname = "slow"\ndelay = 5\n'
            f'report = "# slow report\\n"\n{timeout_line}'
        )
        return str(p)


class ConfigTimeoutE2ETest(IsolatedCwdTest):
    """Config timeouts/retries honored through the full bake-report path."""

    def test_config_global_timeout_honored(self):
        (self.tmp / "bake.yaml").write_text("timeout: 2\n")
        import json

        from bakery import report, runner

        out = report.bake_report(self._recipe(), output=str(self.tmp / "m.md"))
        run_dirs = [d for d in runner.runs_root().iterdir() if d.is_dir()]
        self.assertEqual(len(run_dirs), 1)
        meta = json.loads((run_dirs[0] / "meta.json").read_text())
        st = meta["agents"]["slow"]
        self.assertEqual(st["state"], "timeout")  # 2s config beat the 5s fixture
        self.assertEqual(st["attempts"], 1)       # no retries configured
        self.assertEqual(meta["config"]["params"]["slow"]["timeout"], 2)
        self.assertTrue(Path(out).is_file())

    def test_config_retry_relaunches_timed_out_agent(self):
        (self.tmp / "bake.yaml").write_text("timeout: 2\nretries: 1\n")
        import json

        from bakery import report, runner

        report.bake_report(self._recipe(), output=str(self.tmp / "m.md"))
        run_dirs = [d for d in runner.runs_root().iterdir() if d.is_dir()]
        meta = json.loads((run_dirs[0] / "meta.json").read_text())
        st = meta["agents"]["slow"]
        self.assertEqual(st["state"], "timeout")
        self.assertEqual(st["attempts"], 2)  # first attempt + one retry
        self.assertEqual(meta["config"]["params"]["slow"]["retries"], 1)

    def test_per_agent_timeout_override(self):
        # agent override (30s) beats the config global (2s); the 5s fixture
        # finishes cleanly under it. --timeout stays the waiter deadline and
        # does not touch per-agent timeouts.
        (self.tmp / "bake.yaml").write_text(
            "timeout: 2\nagents:\n  slow:\n    timeout: 30\n"
        )
        import json

        from bakery import report, runner

        report.bake_report(self._recipe(), output=str(self.tmp / "m.md"), timeout=60)
        run_dirs = [d for d in runner.runs_root().iterdir() if d.is_dir()]
        meta = json.loads((run_dirs[0] / "meta.json").read_text())
        self.assertEqual(meta["agents"]["slow"]["state"], "done")
        self.assertEqual(meta["config"]["params"]["slow"]["timeout"], 30)


class ConfigMergeStrategyTest(IsolatedCwdTest):
    def test_digest_strategy_in_report(self):
        (self.tmp / "bake.yaml").write_text("merge_strategy: digest\n")
        body = "".join(f"line {i}\n" for i in range(40))
        p = self.tmp / "r.toml"
        p.write_text(
            '[bakery]\nname = "digest-e2e"\nbackend = "fixture"\nmax_parallel = 1\n\n'
            '[[agents]]\nname = "a"\ndelay = 0\nreport = """\n' + body + '"""\n'
        )
        from bakery import report

        out = report.bake_report(str(p), output=str(self.tmp / "m.md"))
        doc = Path(out).read_text()
        self.assertIn("truncated", doc)
        self.assertIn("line 0", doc)
        self.assertNotIn("line 39", doc)

    def test_unknown_strategy_is_loud(self):
        from bakery import merge

        with self.assertRaises(ValueError):
            merge.merge_reports([], strategy="smart")


class ConfigRedactE2ETest(IsolatedCwdTest):
    def test_extra_redact_pattern_scrubs_merged_report(self):
        (self.tmp / "bake.yaml").write_text(
            "redact:\n  - 'internal-key-[A-Za-z0-9]{8}'\n"
        )
        p = self.tmp / "r.toml"
        p.write_text(
            '[bakery]\nname = "redact-e2e"\nbackend = "fixture"\nmax_parallel = 1\n\n'
            '[[agents]]\nname = "a"\ndelay = 0\n'
            'report = "deploy with internal-key-ABCDEFGH now"\n'
        )
        from bakery import report

        out = report.bake_report(str(p), output=str(self.tmp / "m.md"))
        doc = Path(out).read_text()
        self.assertNotIn("internal-key-ABCDEFGH", doc)
        self.assertIn("[REDACTED]", doc)


class InitConfigTest(IsolatedCwdTest):
    def test_init_config_writes_valid_starter(self):
        from bakery.__main__ import main

        main(["init", "--config"])
        dest = self.tmp / "bake.yaml"
        self.assertTrue(dest.is_file())
        cfg = parse_config(dest.read_text(), source=str(dest))
        self.assertEqual(cfg.timeout, 600)
        self.assertEqual(cfg.agents["slow-auditor"].timeout, 1800)

    def test_init_recipe_still_default(self):
        from bakery.__main__ import main

        main(["init"])
        self.assertTrue((self.tmp / "hello.toml").is_file())
        self.assertFalse((self.tmp / "bake.yaml").exists())

    def test_init_config_refuses_to_overwrite(self):
        (self.tmp / "bake.yaml").write_text("timeout: 1\n")
        from bakery.__main__ import main

        with self.assertRaises(SystemExit):
            main(["init", "--config"])
