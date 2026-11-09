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
