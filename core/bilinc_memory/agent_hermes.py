"""Hermes Agent (Nous Research) specifics. Same contract as agent_claude_code.

Hermes memory (verified against hermes-agent bf0203a, 2026-11-09):
- ``$HERMES_HOME/memories/MEMORY.md`` (agent notes) and ``USER.md`` (user
  profile); default home ``~/.hermes``, each profile has its own home
  ``~/.hermes/profiles/<name>/`` (tools/memory_tool.py:38-40). The plugin is
  enabled per profile, so only the active profile (``HERMES_HOME``) is tracked.
- Entries are joined by exactly ``"\\n§\\n"`` (tools/memory_tool_store.py:25).
  Undo and restore write stored bytes back unchanged, so the format survives.
- Hermes writes under an exclusive ``flock`` on ``<file>.lock`` and replaces the
  file atomically (memory_tool_store.py:257-296); ``write_lock`` takes the same
  lock so a restore never interleaves with a Hermes write.
- A running session keeps a frozen copy of memory in its prompt, so a restored
  file takes effect in the next session.

Python 3.9+, standard library only.
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import List, Optional

AGENT = "hermes"
DISPLAY_NAME = "Hermes"
CLOUD_SUPPORTED = False
CLOUD_SOURCE = "hermes-plugin"
CLOUD_KEY_PREFIX = AGENT + ":"
MEMORY_MD_LOAD_LIMIT = False
AFTER_RESTORE_NOTE = ("Hermes keeps a copy of memory for the running session. "
                      "This change takes effect in the next session (/new).")
COMMAND_PREFIX = "/bilinc-"

TRACKED = ("MEMORY.md", "USER.md")
LOCK_WAIT_SECONDS = 5.0


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value:
        return Path(os.path.abspath(os.path.expanduser(value)))
    return default


def agent_dir() -> Path:
    """`BILINC_MEMORY_HERMES_DIR` (tests), else `HERMES_HOME`, else `~/.hermes`.

    This is the active profile's home when Hermes runs a profile.
    """
    if os.environ.get("BILINC_MEMORY_HERMES_DIR"):
        return _env_path("BILINC_MEMORY_HERMES_DIR", Path.home() / ".hermes")
    return _env_path("HERMES_HOME", Path.home() / ".hermes")


def _is_profile_home(home: Path) -> bool:
    return home.parent.name == "profiles"


def memory_roots() -> List[Path]:
    """The active profile's `memories/` folder, if it exists.

    The plugin is enabled per profile, so it tracks the profile it runs in and
    no other. Only this folder is listed; the rest of the Hermes home (which
    holds `.env`, `auth.json` and backups) is never walked.
    """
    root = agent_dir() / "memories"
    return [root] if root.is_dir() else []


def global_memory_file() -> Optional[Path]:
    """Hermes has no user-level file outside a profile."""
    return None


def project_slug(root) -> str:
    """`<home>/memories` -> `default`; `<home>/profiles/<name>/memories` -> `<name>`."""
    home = Path(root).parent
    return home.name if _is_profile_home(home) else "default"


def tracked(relpath: str) -> bool:
    """Exactly MEMORY.md and USER.md; lock files and `.bak.<ts>` copies are not memory."""
    return relpath in TRACKED


def current_project(project_dir=None):
    """The active profile: `(slug, <home>/memories)`. `project_dir` does not apply."""
    home = agent_dir()
    root = home / "memories"
    return project_slug(root), root


@contextlib.contextmanager
def write_lock(path):
    """Hold Hermes's own lock (`<file>.lock`, exclusive flock) while writing."""
    try:
        import fcntl
    except ImportError:  # Windows: Hermes uses the same name, no flock here
        yield
        return
    lock_path = str(path) + ".lock"
    old = os.umask(0o077)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    finally:
        os.umask(old)
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Hermes is writing this memory file; try again in a moment.")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
