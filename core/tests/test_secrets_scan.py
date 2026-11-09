import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bilinc_memory import secrets_scan  # noqa: E402

# Every example is assembled at runtime so no literal credential sits in this file.
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GH_PAT = "github_pat_" + "11ABCDEFG0abcdefghijkl_" + "mnopqrstuvwxyz0123456789"
SLACK = "xoxb-" + "1234567890-" + "abcdefghijkl"
STRIPE = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
OPENAI = "sk-" + "proj-" + "Ab12Cd34Ef56Gh78Ij90Kl12"
ANTHROPIC = "sk-ant-" + "api03-" + "Ab12Cd34Ef56Gh78Ij90Kl12Mn34"
BILINC = "bil_live_" + "q8Zx3Nw5Vb7Hk2Lm"
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIB\n-----END " + "RSA PRIVATE KEY-----"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r"


class ScanRules(unittest.TestCase):
    def assertHit(self, text, rule):
        hits = secrets_scan.scan(text)
        self.assertIn(rule, hits, hits)

    def test_aws(self):
        self.assertHit("key " + AWS, "aws_access_key")
        self.assertHit('aws_secret_access_key = "' + "wJalrXUtnFEMI/K7MDENG/bPxRfiCY" + 'EXAMPLEKEY"', "aws_secret_key")

    def test_github(self):
        self.assertHit("token " + GH, "github_token")
        self.assertHit(GH_PAT, "github_token")

    def test_slack(self):
        self.assertHit(SLACK, "slack_token")

    def test_stripe(self):
        self.assertHit(STRIPE, "stripe_live_key")

    def test_openai_and_anthropic(self):
        self.assertHit(OPENAI, "openai_anthropic_key")
        self.assertHit(ANTHROPIC, "openai_anthropic_key")

    def test_bilinc(self):
        self.assertHit("use " + BILINC, "bilinc_key")

    def test_private_key_block(self):
        self.assertHit(PEM, "private_key_pem")
        self.assertHit("-----BEGIN " + "PRIVATE KEY-----", "private_key_pem")

    def test_jwt(self):
        self.assertHit("Authorization: " + JWT, "jwt")

    def test_generic_assignments(self):
        self.assertHit("password=" + "hunter2hunter2hunter2", "credential_assignment")
        self.assertHit("DB_SECRET=" + "s3cr3tvalue12345", "credential_assignment")
        self.assertHit("api_token = '" + "abcdef123456789xyz" + "'", "credential_assignment")
        self.assertHit('{"password": "' + "correct-horse-battery" + '"}', "credential_assignment")

    def test_generic_needs_twelve_characters(self):
        self.assertEqual(secrets_scan.scan("password=short1"), [])
        self.assertEqual(secrets_scan.scan("token=" + "abcdefghijk"), [])  # 11 chars

    def test_high_entropy_assignment(self):
        blob = "Zk9fQ2xW8mPq4Tn7Vb3Hd6Rj1Sy5Lc0Xg2Ue8Ao4Iw9Nz"  # 46 chars, mixed case + digits
        self.assertGreaterEqual(len(blob), 40)
        self.assertHit("seed = " + blob, "high_entropy_assignment")
        self.assertHit('"seed": "' + blob + '"', "high_entropy_assignment")

    def test_low_entropy_and_hex_strings_are_not_flagged(self):
        self.assertEqual(secrets_scan.scan("commit = " + "a" * 40), [])
        self.assertEqual(secrets_scan.scan("commit: " + "5a766cc0e1f2a3b4c5d6e7f8091a2b3c4d5e6f70"), [])
        self.assertEqual(secrets_scan.scan("path = /Users/someone/Downloads/Projeler/bilinc-memory-history/lib"), [])

    def test_clean_prose_is_clean(self):
        text = (
            "# Notes\nThe user prefers short replies. Deploy runs on Tuesdays.\n"
            "Task-based work: ask-first. Reminder: token budget is tracked per session.\n"
        )
        self.assertEqual(secrets_scan.scan(text), [])

    def test_placeholders_are_not_flagged(self):
        self.assertEqual(secrets_scan.scan("password=${DB_PASSWORD_FROM_ENV}"), [])
        self.assertEqual(secrets_scan.scan("api_key=<your-api-key-here>"), [])

    def test_never_returns_matched_text(self):
        text = "a %s b %s c %s" % (AWS, GH, "password=" + "hunter2hunter2hunter2")
        hits = secrets_scan.scan(text)
        self.assertTrue(hits)
        for hit in hits:
            self.assertRegex(hit, r"^[a-z_]+$")
            for secret in (AWS, GH, "hunter2hunter2hunter2"):
                self.assertNotIn(secret, hit)

    def test_unique_rule_names(self):
        hits = secrets_scan.scan(AWS + "\n" + AWS)
        self.assertEqual(len(hits), len(set(hits)))

    def test_scanner_errors_raise(self):
        for bad in (None, b"bytes", 123):
            with self.assertRaises(TypeError):
                secrets_scan.scan(bad)

    def test_large_input_is_fast(self):
        blob = ("Lorem ipsum dolor sit amet " * 40000) + ("A1b2" * 100000)
        start = time.monotonic()
        secrets_scan.scan(blob)
        self.assertLess(time.monotonic() - start, 5.0)


if __name__ == "__main__":
    unittest.main()
