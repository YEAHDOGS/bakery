"""Tests for secret redaction (bakery/redact.py).

All fixtures use obviously fake secrets — no real credentials anywhere.
"""

import unittest

from bakery.redact import redact


class RedactTest(unittest.TestCase):
    def test_aws_key(self):
        self.assertIn(
            "[REDACTED-AWS-KEY]", redact("key=AKIAIOSFODNN7EXAMPLE")
        )
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", redact("key=AKIAIOSFODNN7EXAMPLE"))

    def test_github_tokens(self):
        for tok in (
            "ghp_" + "a" * 30,
            "github_pat_" + "b" * 40,
            "gho_" + "c" * 30,
        ):
            out = redact(f"export TOKEN={tok}")
            self.assertNotIn(tok, out, tok)
            self.assertIn("[REDACTED]", out)

    def test_openai_and_slack(self):
        self.assertNotIn(
            "sk-fakefakefakefake1234", redact("key: sk-fakefakefakefake1234")
        )
        self.assertNotIn(
            "xoxb-fakefakefake-123456",
            redact("hook: xoxb-fakefakefake-123456"),
        )

    def test_bearer(self):
        out = redact("Authorization: Bearer faketoken12345abcdef")
        self.assertNotIn("faketoken12345abcdef", out)
        self.assertIn("Bearer [REDACTED]", out)

    def test_private_key_block(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "ZmFrZXZhcnlhY2FyZnJ2cHJhbg==\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact(f"config:\n{pem}\n")
        self.assertNotIn("ZmFrZXZhcnlhY2FyZnJ2cHJhbg==", out)
        self.assertIn("[REDACTED-PRIVATE-KEY]", out)

    def test_key_value_assignments(self):
        cases = [
            ("password=hunter2hunter", "password=[REDACTED]"),
            ("api_key: fakekey123", "api_key=[REDACTED]"),
            ("AUTH_TOKEN=faketok", "AUTH_TOKEN=[REDACTED]"),
            ('client_secret = "fakesecret"', "client_secret=[REDACTED]"),
            ("db passwd: hunter2hunter", "passwd=[REDACTED]"),
        ]
        for src, expected in cases:
            with self.subTest(src=src):
                out = redact(src)
                self.assertIn(expected, out)
                for secret in ("hunter2hunter", "fakekey123", "faketok", "fakesecret"):
                    self.assertNotIn(secret, out)

    def test_benign_text_untouched(self):
        for src in (
            "the secret plan is go",
            "passwords are important",
            "sk- short",
            "bearer of good news",
            "AKIA is not a key",
        ):
            self.assertEqual(redact(src), src, src)

    def test_idempotent(self):
        src = "password=hunter2hunter key=AKIAIOSFODNN7EXAMPLE"
        self.assertEqual(redact(redact(src)), redact(src))

    def test_multiline(self):
        src = "line one\napi_key=fakekey\nline three\n"
        out = redact(src)
        self.assertNotIn("fakekey", out)
        self.assertTrue(out.startswith("line one\n"))


if __name__ == "__main__":
    unittest.main()
