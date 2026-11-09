"""Every agent plugin folder must carry an exact copy of core/bilinc_memory."""
import os
import sys
import unittest

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CORE)
import sync  # noqa: E402


class TestVendoredCopies(unittest.TestCase):
    def test_copies_match_core(self):
        diff = sync.differences()
        self.assertEqual(diff, [], "run: python3 core/sync.py\n" + "\n".join(diff))

    def test_claude_code_copy_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(sync.REPO, "claude-code", "lib", "bilinc_memory", "journal.py")))


if __name__ == "__main__":
    unittest.main()


class TestVersionsMatch(unittest.TestCase):
    """Claude Code and Hermes update a plugin only when its version changes, so every
    place that names the version must move together."""

    def test_one_version_everywhere(self):
        import json
        import re
        from bilinc_memory import __version__
        with open(os.path.join(sync.REPO, "claude-code", ".claude-plugin", "plugin.json")) as f:
            claude = json.load(f)["version"]
        with open(os.path.join(sync.REPO, "hermes", "plugin.yaml")) as f:
            hermes = re.search(r"^version:\s*(\S+)", f.read(), re.M).group(1)
        with open(os.path.join(CORE, "bilinc_memory", "cloud.py")) as f:
            agent = re.search(r"bilinc-memory-history/([0-9.]+)", f.read()).group(1)
        self.assertEqual({claude, hermes, agent, __version__}, {__version__})
