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
        cfg = parse_config("timeout: 600\nagents:\n  a:\n    timeout: 900\n")
        self.assertEqual(cfg.timeout_for("a", 60, True, cli_timeout=10), 10)

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
