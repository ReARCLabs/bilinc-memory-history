"""Everything that is specific to Claude Code lives in this module.

The rest of the package (journal, undo, restore, secret scan, Cloud upload)
does not know which agent's memory it is tracking. A future adapter for
another agent (Codex, Hermes, ...) provides the same names:

- ``AGENT``: short id; it names the local store folder and the Cloud key prefix.
- ``CLOUD_SOURCE``: the ``source`` written on Cloud values; pull shows only
  versions with this source by default.
- ``agent_dir()``: the agent's own config folder.
- ``memory_roots()``: each memory folder that exists.
- ``global_memory_file()``: the one user-level file outside any project, or None.
- ``project_slug(root)``: the project name for a memory root.
- ``tracked(relpath)``: whether a file under a root is memory.
- ``write_lock(path)``: context manager held while undo/restore writes a file.
- ``current_project(project_dir)``: ``(slug, root)`` for commands, or None to
  use the Claude Code rule (slug from the working directory).
- ``DISPLAY_NAME``, ``CLOUD_SUPPORTED``, ``AFTER_RESTORE_NOTE``,
  ``MEMORY_MD_LOAD_LIMIT`` (status shows MEMORY.md against Claude's load limit),
  ``COMMAND_PREFIX`` (how users type the commands).

Python 3.9+, standard library only.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import List, Optional

AGENT = "claude-code"
DISPLAY_NAME = "Claude"
CLOUD_SUPPORTED = True
AFTER_RESTORE_NOTE = ""
MEMORY_MD_LOAD_LIMIT = True
COMMAND_PREFIX = "/bilinc:"
CLOUD_SOURCE = "claude-code-plugin"
CLOUD_KEY_PREFIX = AGENT + ":"


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value:
        return Path(os.path.abspath(os.path.expanduser(value)))
    return default


def agent_dir() -> Path:
    """`BILINC_MEMORY_CLAUDE_DIR` (plugin-only override, used by tests), else
    `CLAUDE_CONFIG_DIR`, else `~/.claude`."""
    if os.environ.get("BILINC_MEMORY_CLAUDE_DIR"):
        return _env_path("BILINC_MEMORY_CLAUDE_DIR", Path.home() / ".claude")
    return _env_path("CLAUDE_CONFIG_DIR", Path.home() / ".claude")


def memory_roots() -> List[Path]:
    """Each `agent_dir()/projects/*/memory` that exists."""
    base = agent_dir() / "projects"
    roots: List[Path] = []
    try:
        names = sorted(os.listdir(str(base)))
    except OSError:
        return roots
    for name in names:
        candidate = base / name / "memory"
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def global_memory_file() -> Path:
    """The user-level `CLAUDE.md`."""
    return agent_dir() / "CLAUDE.md"


def project_slug(root) -> str:
    """`<agent_dir>/projects/<slug>/memory` -> `<slug>`."""
    return Path(root).parent.name


def tracked(relpath: str) -> bool:
    """Every file in a Claude Code memory folder is memory."""
    return True


def write_lock(path):
    """Claude Code takes no lock on its memory files."""
    return contextlib.nullcontext()


def current_project(project_dir=None) -> Optional[tuple]:
    """None: commands use the Claude Code slug rule for the working directory."""
    return None
