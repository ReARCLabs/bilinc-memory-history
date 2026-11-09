#!/usr/bin/env python3
"""Copy core/bilinc_memory into every agent plugin folder.

Each agent installs only its own folder (Claude Code copies the plugin dir into
its cache; `hermes plugins install` copies only the given subfolder), so every
plugin carries its own copy of the core. Edit core/bilinc_memory only, then run:

    python3 core/sync.py          # write the copies
    python3 core/sync.py --check  # exit 1 if any copy differs (used by the tests)
"""
import filecmp
import os
import shutil
import sys

CORE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(CORE)
SRC = os.path.join(CORE, "bilinc_memory")
# (agent folder, copy location inside it)
TARGETS = [
    (os.path.join(REPO, "claude-code"), os.path.join(REPO, "claude-code", "lib", "bilinc_memory")),
    (os.path.join(REPO, "hermes"), os.path.join(REPO, "hermes", "bilinc_memory")),
]


def _files(root):
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".py"):
                out.add(os.path.relpath(os.path.join(dirpath, name), root))
    return out


def differences():
    """List of human-readable differences between the core and each copy."""
    want = _files(SRC)
    problems = []
    for agent_dir, target in TARGETS:
        if not os.path.isdir(agent_dir):
            continue  # that agent folder does not exist yet
        have = _files(target) if os.path.isdir(target) else set()
        for rel in sorted(want - have):
            problems.append("%s: missing %s" % (os.path.relpath(target, REPO), rel))
        for rel in sorted(have - want):
            problems.append("%s: extra %s" % (os.path.relpath(target, REPO), rel))
        for rel in sorted(want & have):
            if not filecmp.cmp(os.path.join(SRC, rel), os.path.join(target, rel), shallow=False):
                problems.append("%s: %s differs from core" % (os.path.relpath(target, REPO), rel))
    return problems


def sync():
    for agent_dir, target in TARGETS:
        if not os.path.isdir(agent_dir):
            continue
        if os.path.isdir(target):
            shutil.rmtree(target)
        shutil.copytree(SRC, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        print("synced %s" % os.path.relpath(target, REPO))


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        diff = differences()
        for line in diff:
            print(line)
        sys.exit(1 if diff else 0)
    sync()
