"""Path safety helpers for Bilinc memory history.

Everything that turns a path string (from a hook payload, a Cloud value or the
command line) into a file operation goes through this module. Values are data,
never instructions: nothing here trusts a path until it has been validated.

Python 3.9+, standard library only.
"""
from __future__ import annotations

import contextlib
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import importlib

MAX_FILE_BYTES = 1_048_576
DISPLAY_MAX = 80
RELPATH_MAX = 1024

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_HTML_COMMENT_RE = re.compile(r"<!--(.*?)(?:-->|\Z)", re.DOTALL)

# Unicode general categories that are invisible or that change how text renders.
# Cc control, Cf format (zero-width, bidi, BOM, tag chars), Zl/Zp line and
# paragraph separators, Cs surrogates.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})


class UnsafeStore(Exception):
    """The Bilinc store directory is missing safety properties (owner, type, mode)."""


class UnsafeTarget(OSError, ValueError):
    """A write target is a symlink or not a regular file."""


@dataclass(frozen=True)
class Target:
    scope: str  # "project" | "global"
    slug: Optional[str]
    root: Path
    relpath: str


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if value:
        return Path(os.path.abspath(os.path.expanduser(value)))
    return default


# --------------------------------------------------------------------------
# Agent adapter
# --------------------------------------------------------------------------

AGENTS = {"claude-code": "agent_claude_code", "hermes": "agent_hermes"}


def agent():
    """The adapter module for `BILINC_MEMORY_AGENT` (default `claude-code`).

    Everything agent-specific (where memory lives, which files count, locks)
    comes from this module; see agent_claude_code.py for the contract.
    """
    name = os.environ.get("BILINC_MEMORY_AGENT") or "claude-code"
    mod = AGENTS.get(name)
    if mod is None:
        raise ValueError("unknown agent: %r" % name)
    return importlib.import_module("." + mod, __package__)


def claude_dir() -> Path:
    """The agent's own config folder (name kept from the Claude-only days)."""
    return agent().agent_dir()


def memory_roots() -> List[Path]:
    return agent().memory_roots()


def global_claude_md() -> Optional[Path]:
    """The agent's user-level memory file, or None when the agent has none."""
    return agent().global_memory_file()


def store_dir() -> Path:
    """`BILINC_MEMORY_HOME` or `~/.bilinc/memory-history/<agent>`."""
    return _env_path("BILINC_MEMORY_HOME", Path.home() / ".bilinc" / "memory-history" / agent().AGENT)


@contextlib.contextmanager
def umask_077():
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def ensure_store(path=None) -> Path:
    """Create the store under umask 077 and verify it with lstat.

    The store must be a real directory (not a symlink), owned by the current
    uid, with mode 0700. Raises UnsafeStore otherwise. `path` is an optional
    override (used by Journal(store=...)); the default is store_dir().
    """
    p = Path(path) if path is not None else store_dir()
    with umask_077():
        try:
            os.makedirs(str(p.parent), mode=0o700, exist_ok=True)
            os.mkdir(str(p), 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise UnsafeStore("cannot create store: %s" % exc.strerror) from None
    try:
        st = os.lstat(str(p))
    except OSError as exc:
        raise UnsafeStore("cannot stat store: %s" % exc.strerror) from None
    if stat.S_ISLNK(st.st_mode):
        raise UnsafeStore("store is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeStore("store is not a directory")
    if os.name != "nt":
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise UnsafeStore("store is not owned by the current user")
        if stat.S_IMODE(st.st_mode) != 0o700:
            raise UnsafeStore("store mode is %04o, expected 0700" % stat.S_IMODE(st.st_mode))
    return p


# --------------------------------------------------------------------------
# Relative paths
# --------------------------------------------------------------------------

def validate_relpath(relpath) -> str:
    """Return the normalised POSIX relpath or raise ValueError.

    Rejects: non-strings, empty, absolute, drive letters, `..`, NUL and other
    control characters, backslashes, empty segments. `.` segments are dropped.
    """
    if isinstance(relpath, os.PathLike):
        relpath = os.fspath(relpath)
    if not isinstance(relpath, str):
        raise ValueError("relpath must be a string")
    if not relpath:
        raise ValueError("relpath is empty")
    if len(relpath) > RELPATH_MAX:
        raise ValueError("relpath is too long")
    if "\0" in relpath:
        raise ValueError("relpath contains NUL")
    if "\\" in relpath:
        raise ValueError("relpath contains a backslash")
    for ch in relpath:
        if ord(ch) < 32 or ord(ch) == 127:
            raise ValueError("relpath contains a control character")
    if relpath.startswith("/") or _DRIVE_RE.match(relpath):
        raise ValueError("relpath is absolute")
    parts: List[str] = []
    for seg in relpath.split("/"):
        if seg == "":
            raise ValueError("relpath has an empty segment")
        if seg == "..":
            raise ValueError("relpath contains '..'")
        if seg == ".":
            continue
        parts.append(seg)
    if not parts:
        raise ValueError("relpath is empty")
    return "/".join(parts)


def _inside(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def resolve_target(root, relpath) -> Path:
    """Validate `relpath`, then check that it resolves inside `root`.

    Returns `Path(root) / relpath` (not the resolved form, so the caller keeps
    the leaf and atomic_write can still refuse a symlink there). Raises
    ValueError for traversal, including through symlinked directories.
    """
    rel = validate_relpath(relpath)
    root_path = Path(root)
    candidate = root_path.joinpath(*rel.split("/"))
    real_root = os.path.realpath(str(root_path))
    real_candidate = os.path.realpath(str(candidate))
    if real_candidate == real_root or not _inside(real_candidate, real_root):
        raise ValueError("path escapes the root")
    return candidate


def classify(path) -> Optional[Target]:
    """Map an absolute file path to a Target, or None.

    The realpath must be a regular-file path inside a memory root, or exactly
    the global CLAUDE.md. A path that does not exist yet is accepted (so deletes
    can be journaled), but anything that exists and is not a regular file
    (directory, FIFO, device) is refused, as is a symlink that resolves outside
    a root.
    """
    try:
        if isinstance(path, os.PathLike):
            path = os.fspath(path)
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, str) or not path or "\0" in path:
            return None
        real = os.path.realpath(path)
        try:
            st = os.lstat(real)
        except FileNotFoundError:
            st = None
        if st is not None and not stat.S_ISREG(st.st_mode):
            return None

        ag = agent()
        gm = ag.global_memory_file()
        if gm is not None:
            real_gm = os.path.join(os.path.realpath(str(gm.parent)), gm.name)
            if real == real_gm:
                return Target("global", None, gm.parent, gm.name)

        for root in ag.memory_roots():
            real_root = os.path.realpath(str(root))
            if real != real_root and _inside(real, real_root):
                rel = real[len(real_root.rstrip(os.sep)) + 1:]
                if os.sep != "/":
                    rel = rel.replace(os.sep, "/")
                try:
                    rel = validate_relpath(rel)
                except ValueError:
                    return None
                if not ag.tracked(rel):
                    return None
                return Target("project", ag.project_slug(root), root, rel)
        return None
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# Safe file IO
# --------------------------------------------------------------------------

def read_regular(path, limit: int = MAX_FILE_BYTES) -> Optional[bytes]:
    """Read a regular file without following a final symlink.

    Returns None for a missing file, symlink, FIFO, device, directory, or a file
    larger than `limit`. Never blocks on a FIFO (O_NONBLOCK).
    """
    p = os.fspath(path)
    try:
        if stat.S_ISLNK(os.lstat(p).st_mode):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(p, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 262144))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            return None
        return data
    except OSError:
        return None
    finally:
        os.close(fd)


def atomic_write(path, data: bytes) -> None:
    """Write `data` to `path` via a temp file in the same directory.

    fsyncs the temp file, renames it over the target, creates parent dirs at
    0700. Refuses (UnsafeTarget) when the target is a symlink or exists and is
    not a regular file. The mode of an existing file is kept; new files are 0600.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes")
    target = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(target))
    mode = None
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            raise UnsafeTarget("refusing to write through a symlink")
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeTarget("target exists and is not a regular file")
        mode = stat.S_IMODE(st.st_mode)
    with umask_077():
        os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".bilinc-", suffix=".tmp")
    try:
        try:
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            view = memoryview(bytes(data))
            while len(view):
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if os.name != "nt":
        try:
            dfd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------

def display_name(s) -> str:
    """Make a name safe to show: no control, newline, bidi or zero-width chars; max 80."""
    text = "" if s is None else str(s)
    cleaned = "".join(ch for ch in text if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES)
    if len(cleaned) > DISPLAY_MAX:
        cleaned = cleaned[: DISPLAY_MAX - 1] + "…"
    return cleaned


def _marker(ch: str) -> str:
    return "⟦U+%04X⟧" % ord(ch)


def reveal_invisible(text) -> str:
    """Render invisible characters and HTML comments visibly.

    Zero-width, bidi and control characters become `⟦U+200B⟧`; newline and tab
    stay as they are. `<!-- x -->` becomes `⟦comment⟧ x ⟦/comment⟧` (the
    delimiters are replaced, the content is kept and itself revealed).
    """
    text = "" if text is None else str(text)
    text = _HTML_COMMENT_RE.sub(lambda m: "⟦comment⟧" + m.group(1) + "⟦/comment⟧", text)
    out = []
    for ch in text:
        if ch in ("\n", "\t"):
            out.append(ch)
        elif unicodedata.category(ch) in _INVISIBLE_CATEGORIES:
            out.append(_marker(ch))
        else:
            out.append(ch)
    return "".join(out)
