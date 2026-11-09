#!/usr/bin/env python3
"""Bilinc commands for Hermes: ``python3 -I cli.py <command> [args]``."""
import os
import sys

os.environ["BILINC_MEMORY_AGENT"] = "hermes"
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bilinc_memory import commands  # noqa: E402

if __name__ == "__main__":
    sys.exit(commands.main())
