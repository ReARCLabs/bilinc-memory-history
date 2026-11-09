"""Local, append-only version journal for Claude memory files.

Layout under the store (default ``~/.bilinc/memory-history/claude-code``)::

    objects/<sha256>              content, written temp -> rename -> sha re-verified
    projects/<id>/manifest.json   project manifest
    projects/<id>/log.jsonl       one JSON entry per version, monotonic ``seq``
    projects/<id>/log.lock        exclusive lock for appends and manifest edits
    projects/<id>/stat_cache.json reconcile cache
    slugs.json                    slug -> project id (the global file is id ``global``)
    store.lock                    store-wide lock (slugs.json, object writes, purge)

Lock order, to stay deadlock free: store lock, then a project lock. Nothing takes
the store lock while holding a project lock, except that update_manifest callbacks
must not call back into Journal methods that take other locks.

Everything read from disk is treated as data. Python 3.9+, standard library only.
"""
from __future__ import annotations

import contextlib
import datetime
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import paths
from .paths import UnsafeStore, Target  # noqa: F401  (re-exported for callers)

if os.name == "nt":  # pragma: no cover - exercised only on Windows
    import msvcrt
    fcntl = None
else:
    import fcntl
    msvcrt = None

OPS = ("write", "delete", "baseline")
# "claude" is the pre-multi-agent name of "agent"; it is still accepted.
SOURCES = ("agent", "claude", "external", "undo", "pull", "baseline")
DEFAULT_MAX_VERSIONS = 200
LOCK_TIMEOUT = 8.0  # seconds; hooks have a 10 s budget
TEMP_MAX_AGE = 3600.0

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class JournalError(Exception):
    """Generic journal failure (corrupt data, unknown project, ...)."""


class JournalLockTimeout(JournalError):
    """Could not take a journal lock in time."""


class ObjectMissing(JournalError, FileNotFoundError):
    """The object file does not exist (never written, pruned or purged)."""


class ObjectCorrupt(JournalError):
    """The object content does not match its sha256 name."""


class Project(dict):
    """A project manifest (dict) with attribute shortcuts: ``.id``, ``.slug``, ..."""

    __slots__ = ()

    @property
    def id(self) -> str:
        return self["id"]

    @property
    def slug(self) -> Optional[str]:
        return self.get("slug")

    @property
    def labels(self) -> dict:
        return self.get("labels") or {}

    @property
    def cloud(self) -> dict:
        return self.get("cloud") or {}

    @property
    def manifest(self) -> dict:
        return self


# --------------------------------------------------------------------------
# Locking
# --------------------------------------------------------------------------

_tls = threading.local()


def _held() -> set:
    held = getattr(_tls, "held", None)
    if held is None:
        held = _tls.held = set()
    return held


def _try_lock(fd: int) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return False
            raise
    # Windows: lock one byte at offset 0.
    os.lseek(fd, 0, os.SEEK_SET)  # pragma: no cover
    try:  # pragma: no cover
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return True
    except OSError:  # pragma: no cover
        return False


def _unlock(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:  # pragma: no cover
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextlib.contextmanager
def _file_lock(path, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """Exclusive inter-process lock on ``path`` (re-entrant within a thread)."""
    key = os.fspath(path)
    held = _held()
    if key in held:
        yield
        return
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(key, flags, 0o600)
    try:
        if msvcrt is not None and os.fstat(fd).st_size == 0:  # pragma: no cover
            os.write(fd, b"\0")
        deadline = time.monotonic() + timeout
        delay = 0.0005
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise JournalLockTimeout("timed out waiting for %s" % os.path.basename(key))
            time.sleep(delay)
            delay = min(delay * 2, 0.02)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
            _unlock(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Small file helpers
# --------------------------------------------------------------------------

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_sha(sha) -> bool:
    return isinstance(sha, str) and bool(_SHA_RE.match(sha))


def _check_id(project_id) -> str:
    if not isinstance(project_id, str) or not _ID_RE.match(project_id):
        raise ValueError("invalid project id")
    return project_id


def _file_sha(path: str) -> str:
    h = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        while True:
            chunk = os.read(fd, 262144)
            if not chunk:
                break
            h.update(chunk)
    finally:
        os.close(fd)
    return h.hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while len(view):
        written = os.write(fd, view)
        view = view[written:]


def _atomic_bytes(path: str, data: bytes) -> None:
    """temp file in the same dir (0600), fsync, rename over ``path``."""
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tmp-")
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _atomic_json(path: str, obj: Any) -> None:
    _atomic_bytes(path, (json.dumps(obj, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("ascii"))


def _read_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        raise JournalError("unreadable %s" % os.path.basename(path)) from None


def _parse_line(raw: bytes) -> Optional[dict]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or isinstance(obj.get("seq"), bool) or not isinstance(obj.get("seq"), int):
        return None
    return obj


def _read_entries(log: str) -> List[dict]:
    try:
        with open(log, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return []
    out = []
    for raw in data.split(b"\n"):
        entry = _parse_line(raw)
        if entry is not None:
            out.append(entry)
    return out


def _reverse_lines(log: str, chunk: int = 65536) -> Iterator[bytes]:
    """Yield raw lines of ``log`` from the end towards the start."""
    try:
        fh = open(log, "rb")
    except FileNotFoundError:
        return
    with fh:
        pos = fh.seek(0, os.SEEK_END)
        buf = b""
        while pos > 0:
            size = min(chunk, pos)
            pos -= size
            fh.seek(pos)
            data = fh.read(size) + buf
            lines = data.split(b"\n")
            buf = lines[0]
            for line in reversed(lines[1:]):
                yield line
        yield buf


def _ends_with_newline(log: str) -> bool:
    try:
        with open(log, "rb") as fh:
            if fh.seek(0, os.SEEK_END) == 0:
                return True
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"
    except FileNotFoundError:
        return True


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------

class Journal:
    def __init__(self, store: Optional[Path] = None):
        self.store = Path(store) if store is not None else paths.store_dir()
        self._ready()

    # -- layout ---------------------------------------------------------

    def _ready(self) -> None:
        """Verify (and if needed create) the store; raises UnsafeStore."""
        paths.ensure_store(self.store)
        with paths.umask_077():
            for sub in ("objects", "projects"):
                d = self.store / sub
                if not d.is_dir():
                    os.makedirs(str(d), mode=0o700, exist_ok=True)

    def _objects(self) -> str:
        return str(self.store / "objects")

    def _obj_path(self, sha: str) -> str:
        if not _is_sha(sha):
            raise ValueError("invalid object sha")
        return os.path.join(self._objects(), sha)

    def _pdir(self, project_id: str, must_exist: bool = True) -> str:
        pid = _check_id(project_id)
        d = str(self.store / "projects" / pid)
        if must_exist and not os.path.isdir(d):
            raise JournalError("unknown project")
        return d

    def _store_lock(self):
        return _file_lock(str(self.store / "store.lock"))

    def _plock(self, pdir: str):
        return _file_lock(os.path.join(pdir, "log.lock"))

    # -- projects -------------------------------------------------------

    def projects(self) -> List[str]:
        """Ids of all projects that have a manifest."""
        self._ready()
        base = self.store / "projects"
        out = []
        for name in sorted(os.listdir(str(base))):
            if _ID_RE.match(name) and os.path.isfile(str(base / name / "manifest.json")):
                out.append(name)
        return out

    def manifest(self, project_id: str) -> Project:
        self._ready()
        pdir = self._pdir(project_id)
        data = _read_json(os.path.join(pdir, "manifest.json"))
        if not isinstance(data, dict):
            raise JournalError("manifest is missing or invalid")
        return Project(data)

    def _load_slugs(self) -> Dict[str, str]:
        """slugs.json, rebuilt from the manifests if missing or corrupt."""
        path = str(self.store / "slugs.json")
        try:
            data = _read_json(path)
        except JournalError:
            data = None
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and _ID_RE.match(v)}
        rebuilt: Dict[str, str] = {}
        base = self.store / "projects"
        for name in sorted(os.listdir(str(base))):
            try:
                m = _read_json(str(base / name / "manifest.json"))
            except JournalError:
                continue
            if isinstance(m, dict) and m.get("slug") and _ID_RE.match(name):
                rebuilt[str(m["slug"])] = name
        if rebuilt:
            _atomic_json(path, rebuilt)
        return rebuilt

    def project_for(self, target: Target, labels: Optional[dict] = None) -> Project:
        """Get or create the project for a Target (random uuid4 per slug).

        The global CLAUDE.md always uses project id ``global``. ``labels`` is an
        optional extra: keys missing from the manifest labels are added.
        """
        self._ready()
        if target.scope == "global":
            pid, slug = "global", None
        else:
            slug = target.slug
            if not isinstance(slug, str) or not slug:
                raise ValueError("project target needs a slug")
            pid = None
        with self._store_lock():
            slugs = self._load_slugs()
            if pid is None:
                pid = slugs.get(slug) or str(uuid.uuid4())
            pdir = self._pdir(pid, must_exist=False)
            mpath = os.path.join(pdir, "manifest.json")
            manifest = _read_json(mpath) if os.path.isfile(mpath) else None
            if not isinstance(manifest, dict):
                with paths.umask_077():
                    os.makedirs(pdir, mode=0o700, exist_ok=True)
                manifest = {
                    "id": pid,
                    "slug": slug,
                    "labels": {},
                    "cloud": {
                        "enabled": False,
                        "cloud_project_id": None,
                        "pushed_seq": 0,
                        "versions": {},
                        "skipped": [],
                    },
                    "created": _now(),
                }
                _atomic_json(mpath, manifest)
            if slug is not None and slugs.get(slug) != pid:
                slugs[slug] = pid
                _atomic_json(str(self.store / "slugs.json"), slugs)
        if labels:
            clean = {str(k): v for k, v in labels.items() if v is not None}
            if any(k not in (manifest.get("labels") or {}) for k in clean):
                def _merge(m: dict) -> None:
                    cur = m.setdefault("labels", {})
                    for k, v in clean.items():
                        cur.setdefault(k, v)
                manifest = self.update_manifest(pid, _merge)
        return Project(manifest)

    def update_manifest(self, project_id: str, fn: Callable[[dict], Any]) -> Project:
        """Run ``fn(manifest)`` under the project lock, then write atomically.

        ``fn`` may mutate the dict in place or return a replacement dict. It must
        not call other Journal methods that take a different lock. Returns the
        stored manifest.
        """
        self._ready()
        pdir = self._pdir(project_id)
        with self._plock(pdir):
            mpath = os.path.join(pdir, "manifest.json")
            manifest = _read_json(mpath)
            if not isinstance(manifest, dict):
                raise JournalError("manifest is missing or invalid")
            result = fn(manifest)
            if isinstance(result, dict):
                manifest = result
            _atomic_json(mpath, manifest)
        return Project(manifest)

    # -- objects --------------------------------------------------------

    def put_object(self, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes")
        data = bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        self._ready()
        final = self._obj_path(sha)
        with self._store_lock():
            try:
                st = os.lstat(final)
            except FileNotFoundError:
                st = None
            if st is not None and stat.S_ISREG(st.st_mode):
                try:
                    if _file_sha(final) == sha:
                        os.utime(final, None)  # keeps prune's grace window honest
                        return sha
                except OSError:
                    pass
            fd, tmp = tempfile.mkstemp(dir=self._objects(), prefix=".tmp-")
            try:
                try:
                    _write_all(fd, data)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, final)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            try:
                ok = _file_sha(final) == sha
            except OSError:
                ok = False
            if not ok:
                with contextlib.suppress(OSError):
                    os.unlink(final)
                raise ObjectCorrupt("object failed sha verification after write")
        return sha

    def has_object(self, sha: str) -> bool:
        try:
            return stat.S_ISREG(os.lstat(self._obj_path(sha)).st_mode)
        except (OSError, ValueError):
            return False

    def get_object(self, sha: str) -> bytes:
        self._ready()
        path = self._obj_path(sha)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            raise ObjectMissing("object %s is not in the journal" % sha[:12]) from None
        except OSError as exc:
            raise ObjectCorrupt("object %s is unreadable: %s" % (sha[:12], exc.strerror)) from None
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 262144)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != sha:
            raise ObjectCorrupt("object %s does not match its sha256" % sha[:12])
        return data

    # -- log ------------------------------------------------------------

    def append(self, project_id: str, *, relpath: str, sha: Optional[str], op: str, source: str,
               session_id: Optional[str] = None, tool_use_id: Optional[str] = None,
               transcript_path: Optional[str] = None) -> dict:
        """Append one version under the exclusive lock; seq is read under the lock.

        Returns the new entry, or the last entry for ``relpath`` when it already
        has the same sha and op ("no change": nothing is written).
        """
        if op not in OPS:
            raise ValueError("invalid op")
        if source not in SOURCES:
            raise ValueError("invalid source")
        rel = paths.validate_relpath(relpath)
        if op == "delete":
            if sha is not None:
                raise ValueError("a delete entry has no sha")
        elif not _is_sha(sha):
            raise ValueError("write and baseline entries need a sha256")
        self._ready()
        pdir = self._pdir(project_id)
        log = os.path.join(pdir, "log.jsonl")
        with self._plock(pdir):
            last_seq = None
            last_for = None
            for raw in _reverse_lines(log):
                entry = _parse_line(raw)
                if entry is None:
                    continue
                if last_seq is None:
                    last_seq = entry["seq"]
                if last_for is None and entry.get("relpath") == rel:
                    last_for = entry
                if last_seq is not None and last_for is not None:
                    break
            if (last_for is not None and not last_for.get("purged")
                    and last_for.get("sha") == sha and last_for.get("op") == op):
                return last_for
            if sha is not None and not self.has_object(sha):
                raise JournalError("object %s is not in the journal" % sha[:12])
            entry = {
                "seq": (last_seq or 0) + 1,
                "time": _now(),
                "relpath": rel,
                "sha": sha,
                "op": op,
                "source": source,
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "transcript_path": transcript_path,
            }
            line = (json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            if not _ends_with_newline(log):
                line = b"\n" + line  # fence off a torn last line
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(log, flags, 0o600)
            try:
                _write_all(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            return entry

    def entries(self, project_id: str, relpath: Optional[str] = None) -> List[dict]:
        """All entries in seq order; torn or invalid lines are skipped."""
        self._ready()
        pdir = self._pdir(project_id)
        out = _read_entries(os.path.join(pdir, "log.jsonl"))
        out.sort(key=lambda e: e["seq"])
        if relpath is not None:
            out = [e for e in out if e.get("relpath") == relpath]
        return out

    def current(self, project_id: str, relpath: str) -> Optional[dict]:
        """The last entry for ``relpath`` (may be a delete or purged entry)."""
        self._ready()
        pdir = self._pdir(project_id)
        best = None
        for raw in _reverse_lines(os.path.join(pdir, "log.jsonl")):
            entry = _parse_line(raw)
            if entry is not None and entry.get("relpath") == relpath:
                best = entry
                break
        return best

    def _last_ops(self, project_id: str) -> Dict[str, dict]:
        last: Dict[str, dict] = {}
        for entry in self.entries(project_id):
            rel = entry.get("relpath")
            if isinstance(rel, str):
                last[rel] = entry
        return last

    def relpaths(self, project_id: str) -> List[str]:
        """Relpaths whose last op is not ``delete``."""
        return sorted(r for r, e in self._last_ops(project_id).items() if e.get("op") != "delete")

    def deleted(self, project_id: str) -> List[str]:
        return sorted(r for r, e in self._last_ops(project_id).items() if e.get("op") == "delete")

    # -- reconcile cache ------------------------------------------------

    def stat_cache(self, project_id: str) -> dict:
        self._ready()
        pdir = self._pdir(project_id)
        try:
            data = _read_json(os.path.join(pdir, "stat_cache.json"), {})
        except JournalError:
            return {}
        return data if isinstance(data, dict) else {}

    def save_stat_cache(self, project_id: str, cache: dict) -> None:
        self._ready()
        pdir = self._pdir(project_id)
        _atomic_json(os.path.join(pdir, "stat_cache.json"), cache)

    # -- housekeeping ---------------------------------------------------

    def size_bytes(self) -> int:
        """Total size of every file in the store."""
        self._ready()
        total = 0
        for dirpath, _dirs, files in os.walk(str(self.store), followlinks=False):
            for name in files:
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
        return total

    def _referenced_shas(self) -> set:
        refs = set()
        for pid in self.projects():
            for entry in _read_entries(os.path.join(self._pdir(pid), "log.jsonl")):
                if _is_sha(entry.get("sha")) and not entry.get("purged"):
                    refs.add(entry["sha"])
        return refs

    def _rewrite_log(self, log: str, entries: List[dict]) -> None:
        data = "".join(json.dumps(e, ensure_ascii=True, separators=(",", ":")) + "\n" for e in entries)
        _atomic_bytes(log, data.encode("ascii"))

    def prune(self, max_versions_per_file: int = DEFAULT_MAX_VERSIONS, *, grace_seconds: float = 60.0) -> dict:
        """Retention cap: keep the newest ``max_versions_per_file`` entries per relpath.

        Never drops the newest entry of a relpath, an entry the manifest still
        points at (``cloud.versions``), or, while Cloud is enabled, an entry not
        yet pushed. Then removes objects no entry references. Objects younger
        than ``grace_seconds`` are kept, so an object written moments ago whose
        entry is not appended yet is never swept. Returns counts.
        """
        keep_n = max(1, int(max_versions_per_file))
        self._ready()
        dropped = 0
        for pid in self.projects():
            pdir = self._pdir(pid)
            log = os.path.join(pdir, "log.jsonl")
            with self._plock(pdir):
                entries = _read_entries(log)
                try:
                    cloud = (_read_json(os.path.join(pdir, "manifest.json"), {}) or {}).get("cloud") or {}
                except (JournalError, AttributeError):
                    cloud = {}
                protected = set()
                for info in (cloud.get("versions") or {}).values():
                    if isinstance(info, dict) and isinstance(info.get("seq"), int):
                        protected.add(info["seq"])
                pushed = cloud.get("pushed_seq") if cloud.get("enabled") else None
                by_rel: Dict[Any, List[dict]] = {}
                for e in entries:
                    by_rel.setdefault(e.get("relpath"), []).append(e)
                drop = set()
                for items in by_rel.values():
                    for e in items[:-keep_n]:
                        if e["seq"] in protected:
                            continue
                        if isinstance(pushed, int) and e["seq"] > pushed:
                            continue
                        drop.add(e["seq"])
                if drop:
                    self._rewrite_log(log, [e for e in entries if e["seq"] not in drop])
                    dropped += len(drop)
        removed = freed = 0
        now = time.time()
        with self._store_lock():
            refs = self._referenced_shas()
            objdir = self._objects()
            for name in os.listdir(objdir):
                full = os.path.join(objdir, name)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                age = now - st.st_mtime
                if _is_sha(name):
                    if name in refs or age < grace_seconds:
                        continue
                elif name.startswith(".tmp-") and age > TEMP_MAX_AGE:
                    pass
                else:
                    continue
                with contextlib.suppress(OSError):
                    os.unlink(full)
                    removed += 1
                    freed += st.st_size
        return {"entries_dropped": dropped, "objects_removed": removed, "bytes_freed": freed}

    def purge_object(self, sha: str) -> int:
        """Remove one object and mark matching log entries ``purged: true``.

        Logs are rewritten under each project's lock first, the object file goes
        last. Returns the number of entries marked.
        """
        path = self._obj_path(sha)
        self._ready()
        marked = 0
        with self._store_lock():
            for pid in self.projects():
                pdir = self._pdir(pid)
                log = os.path.join(pdir, "log.jsonl")
                with self._plock(pdir):
                    entries = _read_entries(log)
                    hits = 0
                    for e in entries:
                        if e.get("sha") == sha and not e.get("purged"):
                            e["purged"] = True
                            hits += 1
                    if hits:
                        self._rewrite_log(log, entries)
                        marked += hits
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
        return marked

    def wipe(self) -> None:
        """Delete the journal: objects, projects, slugs.json and lock files.

        Unknown files in the store directory (for example ``config.json``) are
        left alone, and the directory itself is removed only if it ends up empty.
        """
        import shutil

        paths.ensure_store(self.store)  # refuse to wipe an unsafe store
        for name in ("objects", "projects"):
            shutil.rmtree(str(self.store / name), ignore_errors=True)
        for name in ("slugs.json", "store.lock"):
            with contextlib.suppress(OSError):
                os.unlink(str(self.store / name))
        with contextlib.suppress(OSError):
            os.rmdir(str(self.store))
