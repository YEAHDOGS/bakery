"""Tests for the agent sandbox (bakery/sandbox.py)."""

import os
import unittest

from bakery import sandbox
from bakery.sandbox import looks_secret, sandbox_env


class SecretDetectionTest(unittest.TestCase):
    def test_obvious_secrets(self):
        for name in ["API_KEY", "api_key", "MY_SECRET", "DB_PASSWORD", "AUTH_TOKEN",
                     "BEARER_HEADER", "aws_access_key_id", "PRIVATE_KEY", "gh_token"]:
            self.assertTrue(looks_secret(name), name)

    def test_benign_names_pass(self):
        for name in ["PATH", "HOME", "TASK_NAME", "AGENT_TIMEOUT", "FOO"]:
            self.assertFalse(looks_secret(name), name)


class SandboxEnvTest(unittest.TestCase):
    def test_secret_env_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            sandbox_env({"OPENAI_API_KEY": "sk-abc"})
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_inherited_credentials_stripped(self):
        os.environ["BAKERY_TEST_SECRET_TOKEN"] = "leaked-value"
        try:
            env = sandbox_env()
        finally:
            del os.environ["BAKERY_TEST_SECRET_TOKEN"]
        self.assertNotIn("BAKERY_TEST_SECRET_TOKEN", env)
        self.assertNotIn("leaked-value", " ".join(env.values()))

    def test_allowlist_kept(self):
        env = sandbox_env()
        if "PATH" in os.environ:
            self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_recipe_env_passes_through(self):
        env = sandbox_env({"TASK": "demo", "AGENT_NUM": "3"})
        self.assertEqual(env["TASK"], "demo")
        self.assertEqual(env["AGENT_NUM"], "3")

    def test_unlisted_keys_dropped(self):
        os.environ["BAKERY_TEST_RANDOM_XYZ"] = "hi"
        try:
            self.assertNotIn("BAKERY_TEST_RANDOM_XYZ", sandbox_env())
        finally:
            del os.environ["BAKERY_TEST_RANDOM_XYZ"]


if __name__ == "__main__":
    unittest.main()
