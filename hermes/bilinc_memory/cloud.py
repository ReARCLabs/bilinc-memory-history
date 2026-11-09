"""Bilinc Cloud sync for the journal: push, list projects, pull.

Design rules:
- push is strictly in `seq` order and stops at the first failure;
- first version of a key = commit(ifAbsent), later ones = revise(expectedVersion);
- a local secret hit is skipped and reported (the text is never echoed); a
  scanner error stops the push (fail closed);
- a server `secret_redacted` warning counts as skipped, and the Cloud version
  is recorded so pull can exclude it;
- every value that comes back from the Cloud is data: relpaths are validated,
  foreign writers are flagged, nothing is written without journaling it.
"""

import contextlib
import copy
import hashlib
import json
import os
import platform
import re
import time
import uuid
from datetime import datetime, timezone

from . import http, paths, secrets_scan
from .http import CloudError, api_key  # noqa: F401  (re-exported for callers)

# Cloud is wired for Claude Code only so far (agent CLOUD_SUPPORTED); its key
# prefix and source stay fixed so existing Cloud values keep matching.
SOURCE = "claude-code-plugin"
KEY_PREFIX = "claude-code:"
CARD_NAME = "_project"
PUSH_BUDGET_SECONDS = 8.0
CALL_TIMEOUT_SECONDS = 15.0
MAX_LIST_PAGES = 200
PREVIEW_CHARS = 400
_CPID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_ZERO_SHA16 = "0" * 16


class PullItem(dict):
    """A dict that also allows attribute access (`item.relpath`)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


# --- small helpers -----------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _machine():
    return (platform.node() or "unknown")[:64]


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _check_cpid(cloud_project_id):
    if not isinstance(cloud_project_id, str) or not _CPID_RE.match(cloud_project_id):
        raise ValueError("invalid cloud project id")
    return cloud_project_id


def _key(cloud_project_id, relpath):
    return "%s%s:%s" % (KEY_PREFIX, cloud_project_id, relpath)


def _cloud_section(manifest):
    cloud = manifest.setdefault("cloud", {})
    cloud.setdefault("enabled", False)
    cloud.setdefault("cloud_project_id", None)
    cloud.setdefault("pushed_seq", 0)
    if not isinstance(cloud.get("versions"), dict):
        cloud["versions"] = {}
    if not isinstance(cloud.get("skipped"), list):
        cloud["skipped"] = []
    return cloud


def _manifest(journal, project_id):
    """A deep copy of the project manifest, using only the A interface."""
    getter = getattr(journal, "manifest", None)
    if callable(getter):
        try:
            return copy.deepcopy(getter(project_id))
        except Exception:
            pass
    box = {}

    def grab(manifest):
        box["m"] = copy.deepcopy(manifest)
        return manifest

    journal.update_manifest(project_id, grab)
    return box["m"]


def _update(journal, project_id, mutate):
    """Run `mutate(cloud_section, manifest)` under the manifest lock."""

    def fn(manifest):
        mutate(_cloud_section(manifest), manifest)
        return manifest

    journal.update_manifest(project_id, fn)


def _record_error(journal, project_id, code, message, retryable=False):
    def mutate(cloud, _manifest):
        cloud["last_error"] = {
            "time": _now_iso(),
            "code": str(code)[:64],
            "message": str(message)[:200],
            "retryable": bool(retryable),
        }

    try:
        _update(journal, project_id, mutate)
    except Exception:
        pass


def _store_path(journal):
    for attr in ("store", "root", "path"):
        value = getattr(journal, attr, None)
        if value:
            return value
    return paths.store_dir()


@contextlib.contextmanager
def _push_lock(journal, project_id):
    """Non-blocking per-project lock: async hooks must not push concurrently."""
    try:
        import fcntl
    except ImportError:  # Windows: best effort, no lock
        yield True
        return
    from pathlib import Path

    directory = Path(str(_store_path(journal))) / "projects" / project_id
    if not directory.is_dir():
        yield True
        return
    try:
        fd = os.open(str(directory / "push.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        yield True
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)  # closing releases the flock


# --- client ------------------------------------------------------------------

class Client:
    """Real Cloud client. Anything with `.call(path, payload, idempotency_key=None)` works instead."""

    def __init__(self, key=None, base=None, timeout=CALL_TIMEOUT_SECONDS, deadline=None):
        self._key = key
        self._base = base
        self.timeout = timeout
        self.deadline = deadline  # time.monotonic() value; None = unbounded

    def call(self, path, payload, idempotency_key=None):
        key = self._key or http.api_key()
        if not key:
            raise CloudError("missing_api_key", 401, False, "No Bilinc API key configured")
        timeout = self.timeout
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0.05:
                raise CloudError("timeout", None, True, "Time budget used up")
            timeout = min(timeout, remaining)
        headers = {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "bilinc-memory-history/0.1.2",
            "X-Bilinc-Client": SOURCE,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        url = (self._base or http.base_url()).rstrip("/") + path
        return http.request("POST", url, headers, payload, timeout)


def _first(mapping, *names):
    for name in names:
        if mapping.get(name) is not None:
            return mapping[name]
    return None


def _entry_version(response):
    if not isinstance(response, dict):
        return None
    version = _first(response, "entry_version", "entryVersion")
    if version is None and isinstance(response.get("entry"), dict):
        version = _first(response["entry"], "entry_version", "entryVersion")
    return version if isinstance(version, str) and version else None


def _warnings(response):
    out = []
    if isinstance(response, dict):
        raw = response.get("warnings")
        if isinstance(raw, list):
            out = [w for w in raw if isinstance(w, dict)]
    return out


def _page_entries(page):
    value = page.get("entries") if isinstance(page, dict) else None
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _iter_entries(client, prefix):
    cursor = None
    seen = set()
    for _ in range(MAX_LIST_PAGES):
        payload = {"prefix": prefix, "limit": 100, "values": "full"}
        if cursor:
            payload["cursor"] = cursor
        page = client.call("/api/cloud/memory/list", payload)
        for entry in _page_entries(page):
            yield entry
        cursor = _first(page, "nextCursor", "next_cursor") if isinstance(page, dict) else None
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            return
        seen.add(cursor)


def _parse_value(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _current_entry(client, key):
    for entry in _iter_entries(client, key):
        if entry.get("key") == key:
            return entry
    return None


# --- project card, enable, disable --------------------------------------------

_URL_USERINFO = re.compile(r"(?<=://)[^/@\s]+@")


def _safe_labels(labels):
    out = {}
    for name, value in (labels or {}).items():
        if not isinstance(value, str) or not value:
            continue
        value = _URL_USERINFO.sub("", value)
        try:
            if secrets_scan.scan(value):
                continue
        except Exception:
            continue
        out[str(name)] = value[:300]
    return out


def _ensure_card(journal, project_id, client, cloud_project_id):
    manifest = _manifest(journal, project_id)
    card = {
        "labels": _safe_labels(manifest.get("labels")),
        "machine": _machine(),
        "created": manifest.get("created") or _now_iso(),
    }
    payload = {
        "key": _key(cloud_project_id, CARD_NAME),
        "value": card,
        "memoryType": "semantic",
        "importance": 0.5,
        "metadata": {},
        "source": SOURCE,
        "ifAbsent": True,
    }
    try:
        # Keyed by payload: another machine's card differs, and must not hit idempotency_conflict.
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        client.call("/api/cloud/memory/commit", payload, idempotency_key="cc:%s:card:%s" % (cloud_project_id, digest))
    except CloudError as exc:
        if exc.code != "memory_exists":
            raise
    _update(journal, project_id, lambda cloud, _m: cloud.__setitem__("card_written", True))


def _reset_sync_state(cloud):
    cloud["pushed_seq"] = 0
    cloud["versions"] = {}
    cloud["skipped"] = []
    cloud["card_written"] = False


def enable(journal, project_id, cloud_project_id=None, *, client=None):
    """Turn Cloud on for a project. Mints a cloud_project_id unless one exists or is given.

    The local history is not sent here: the next push backfills it, oldest first.
    Returns a copy of the manifest's cloud section, plus `card_error` if the
    project card could not be written yet (push retries it).
    """
    if cloud_project_id is not None:
        _check_cpid(cloud_project_id)
    existing = _cloud_section(_manifest(journal, project_id)).get("cloud_project_id")
    chosen = cloud_project_id or existing or ("global" if project_id == "global" else str(uuid.uuid4()))

    def mutate(cloud, _manifest_):
        if cloud.get("cloud_project_id") != chosen:
            _reset_sync_state(cloud)
        cloud["cloud_project_id"] = chosen
        cloud["enabled"] = True

    _update(journal, project_id, mutate)
    card_error = None
    try:
        _ensure_card(journal, project_id, client or Client(), chosen)
    except CloudError as exc:
        card_error = {"code": exc.code, "message": exc.message}
        _record_error(journal, project_id, exc.code, exc.message, exc.retryable)
    result = copy.deepcopy(_cloud_section(_manifest(journal, project_id)))
    if card_error:
        result["card_error"] = card_error
    return result


def disable(journal, project_id):
    """Turn Cloud off. Local history and the sync bookkeeping stay; Cloud data is untouched."""
    _update(journal, project_id, lambda cloud, _m: cloud.__setitem__("enabled", False))
    return copy.deepcopy(_cloud_section(_manifest(journal, project_id)))


def status(journal, project_id):
    """What `/bilinc:status` needs: state, pending count, skipped versions, last error."""
    cloud = _cloud_section(_manifest(journal, project_id))
    pushed = int(cloud.get("pushed_seq") or 0)
    pending = len([e for e in journal.entries(project_id) if e.get("seq", 0) > pushed])
    return {
        "enabled": bool(cloud.get("enabled")),
        "cloud_project_id": cloud.get("cloud_project_id"),
        "pushed_seq": pushed,
        "pending": pending,
        "skipped": copy.deepcopy(cloud.get("skipped") or []),
        "last_error": copy.deepcopy(cloud.get("last_error")),
        "last_push": cloud.get("last_push"),
    }


# --- push ----------------------------------------------------------------------

def _value_for(entry, content, machine):
    sha = entry.get("sha")
    return {
        "v": 1,
        "relpath": entry["relpath"],
        "sha": sha,
        "op": entry.get("op"),
        "time": entry.get("time"),
        "source": entry.get("source"),
        "session_id": entry.get("session_id"),
        "machine": machine,
        "content": content,
    }


def _send(client, key, value, has_version, version, seq, sha, recreate=False):
    """One CAS write. Returns the Cloud response; raises CloudError."""
    # seq is per machine, so the key also carries the machine and relpath: two
    # machines (or two files) at the same seq must never share a receipt.
    scope = hashlib.sha256(("%s\0%s" % (_machine(), key)).encode("utf-8")).hexdigest()[:12]
    idem = "cc:%s:%d:%s:%s" % (key.split(":", 2)[1], seq, (sha or _ZERO_SHA16)[:16], scope)
    if not has_version and recreate:
        idem += ":c"
    if not has_version:
        return client.call(
            "/api/cloud/memory/commit",
            {
                "key": key,
                "value": value,
                "memoryType": "semantic",
                "importance": 0.5,
                "metadata": {},
                "source": SOURCE,
                "ifAbsent": True,
            },
            idempotency_key=idem,
        )
    return client.call(
        "/api/cloud/memory/revise",
        {
            "key": key,
            "value": value,
            "importance": 0.5,
            "strategy": "recency",
            "reason": "claude-code history seq %d" % seq,
            "expectedVersion": version,
        },
        idempotency_key=idem,
    )


def _result(pushed, skipped, conflict, pending, error=None, timeboxed=False):
    return {
        "pushed": pushed,
        "skipped": skipped,
        "conflict": conflict,
        "pending": pending,
        "error": error,
        "timeboxed": timeboxed,
    }


def push(journal, project_id, *, client=None, deadline=None):
    """Upload pending journal versions in seq order. Stops at the first failure.

    Returns `{pushed, skipped:[{seq, relpath, reason}], conflict:{relpath}|None,
    pending, error:{code,message}|None, timeboxed}`. `deadline` is a
    time.monotonic() value after which no further version is started.
    """
    manifest = _manifest(journal, project_id)
    cloud = _cloud_section(manifest)
    cpid = cloud.get("cloud_project_id")
    if not cpid:
        return _result(0, [], None, 0, {"code": "not_linked", "message": "No Cloud project linked"})
    with _push_lock(journal, project_id) as acquired:
        if not acquired:
            return _result(0, [], None, 0, {"code": "busy", "message": "Another push is running"})
        return _push_locked(journal, project_id, cpid, client, deadline)


def _push_locked(journal, project_id, cpid, client, deadline):
    try:
        client = client or Client(deadline=deadline)
    except CloudError as exc:  # pragma: no cover - Client() does not raise
        return _result(0, [], None, 0, {"code": exc.code, "message": exc.message})
    manifest = _manifest(journal, project_id)
    cloud = _cloud_section(manifest)
    pushed_seq = int(cloud.get("pushed_seq") or 0)
    versions = copy.deepcopy(cloud["versions"])
    entries = sorted(
        (e for e in journal.entries(project_id) if isinstance(e.get("seq"), int) and e["seq"] > pushed_seq),
        key=lambda e: e["seq"],
    )
    pushed = 0
    skipped = []
    machine = _machine()

    def remaining(index):
        return len(entries) - index

    def fail(index, code, message, retryable=False, conflict=None):
        _record_error(journal, project_id, code, message, retryable)
        return _result(pushed, skipped, conflict, remaining(index), {"code": code, "message": message[:200]})

    def advance(seq, mutate=None):
        def apply(cl, _m):
            cl["pushed_seq"] = max(int(cl.get("pushed_seq") or 0), seq)
            if mutate:
                mutate(cl)

        _update(journal, project_id, apply)

    def skip(entry, reason, cloud_version=None, set_version=None):
        record = {"seq": entry["seq"], "reason": reason}
        if entry.get("relpath"):
            record["relpath"] = entry["relpath"]
        if cloud_version:
            record["cloud_version"] = cloud_version

        def mutate(cl):
            cl["skipped"].append(dict(record))
            if set_version:
                cl["versions"][entry["relpath"]] = set_version

        advance(entry["seq"], mutate)
        skipped.append({"seq": entry["seq"], "relpath": entry.get("relpath"), "reason": reason})

    if entries and not cloud.get("card_written"):
        try:
            _ensure_card(journal, project_id, client, cpid)
        except CloudError as exc:
            return fail(0, exc.code, exc.message, exc.retryable)

    for index, entry in enumerate(entries):
        if deadline is not None and time.monotonic() > deadline:
            result = _result(pushed, skipped, None, remaining(index), None, True)
            return result
        seq = entry["seq"]
        try:
            relpath = paths.validate_relpath(entry.get("relpath"))
        except (ValueError, TypeError):
            skip({"seq": seq, "relpath": None}, "invalid_relpath")
            continue
        entry = dict(entry, relpath=relpath)
        synced = versions.get(relpath)
        if synced and int(synced.get("seq", 0)) >= seq:
            advance(seq)  # already in the Cloud, or superseded by a pull/link
            continue
        if entry.get("purged"):
            skip(entry, "purged")
            continue
        op = entry.get("op")
        sha = entry.get("sha")
        content = None
        if op != "delete":
            if not sha:
                skip(entry, "no_content")
                continue
            try:
                data = journal.get_object(sha)
            except Exception as exc:
                return fail(index, "object_unreadable", "Journal object for seq %d is unreadable (%s)" % (seq, type(exc).__name__))
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                skip(entry, "not_utf8")
                continue
            try:
                hits = secrets_scan.scan(content)
            except Exception:
                return fail(index, "scanner_error", "Secret scan failed; upload stopped (fail closed)")
            if hits:
                skip(entry, "secret_scan:" + ",".join(hits))
                continue

        key = _key(cpid, relpath)
        value = _value_for(entry, content, machine)
        has_version = bool(synced and synced.get("cloud_version"))
        try:
            try:
                response = _send(client, key, value, has_version, (synced or {}).get("cloud_version"), seq, sha)
            except CloudError as exc:
                if exc.code == "memory_not_found" and has_version:
                    # The key was removed from the Cloud: start it again.
                    response = _send(client, key, value, False, None, seq, sha, recreate=True)
                else:
                    raise
        except CloudError as exc:
            if exc.code in ("version_conflict", "memory_exists"):
                try:
                    current = _current_entry(client, key)
                except CloudError as exc2:
                    return fail(index, exc2.code, exc2.message, exc2.retryable)
                cloud_value = _parse_value(current.get("value")) if current else None
                version = _first(current, "entryVersion", "entry_version") if current else None
                if (
                    cloud_value
                    and isinstance(version, str)
                    and cloud_value.get("sha") == sha
                    and cloud_value.get("op") == op
                ):
                    versions[relpath] = {"seq": seq, "cloud_version": version}
                    advance(seq, lambda cl, r=relpath, s=seq, v=version: cl["versions"].__setitem__(r, {"seq": s, "cloud_version": v}))
                    pushed += 1
                    continue
                _record_error(journal, project_id, "conflict", "Another machine changed this file in the Cloud; pull first")
                return _result(
                    pushed, skipped, {"relpath": relpath, "seq": seq, "code": exc.code},
                    remaining(index), {"code": exc.code, "message": "Cloud copy differs; pull to resolve"},
                )
            if exc.code == "secret_detected":
                skip(entry, "secret_rejected_by_server")
                continue
            return fail(index, exc.code, exc.message, exc.retryable)

        version = _entry_version(response)
        if version is None:
            try:
                current = _current_entry(client, key)
            except CloudError as exc:
                return fail(index, exc.code, exc.message, exc.retryable)
            version = _first(current, "entryVersion", "entry_version") if current else None
        if not isinstance(version, str) or not version:
            return fail(index, "invalid_response", "Cloud did not return an entry version for seq %d" % seq)
        versions[relpath] = {"seq": seq, "cloud_version": version}
        if any(w.get("code") == "secret_redacted" for w in _warnings(response)):
            skip(entry, "secret_redacted", cloud_version=version, set_version={"seq": seq, "cloud_version": version})
            continue

        def done(cl, r=relpath, s=seq, v=version):
            cl["versions"][r] = {"seq": s, "cloud_version": v}
            cl["last_push"] = _now_iso()
            cl.pop("last_error", None)

        advance(seq, done)
        pushed += 1

    if pushed or entries:
        _update(journal, project_id, lambda cl, _m: (cl.pop("last_error", None), cl.__setitem__("last_push", _now_iso())))
    return _result(pushed, skipped, None, 0)


def maybe_push(journal, project_id=None, *, client=None, budget=PUSH_BUDGET_SECONDS):
    """Best-effort push for hooks: a no-op unless Cloud is on, time-boxed, never raises.

    Network errors are recorded in the manifest (`cloud.last_error`) for `status`.
    Also accepts a project object/dict as the first argument (`maybe_push(project)`).
    """
    try:
        if project_id is None:
            from . import journal as journal_mod

            project_id = journal if isinstance(journal, str) else (
                journal.get("id") if isinstance(journal, dict) else getattr(journal, "id", None)
            )
            journal = journal_mod.Journal()
        if not project_id:
            return None
        cloud = _cloud_section(_manifest(journal, project_id))
        if not cloud.get("enabled") or not cloud.get("cloud_project_id"):
            return None
        deadline = time.monotonic() + float(budget)
        result = push(journal, project_id, client=client, deadline=deadline)
        return result
    except CloudError as exc:
        _record_error(journal, project_id, exc.code, exc.message, exc.retryable)
    except Exception as exc:  # hooks must never fail a session
        try:
            _record_error(journal, project_id, "error", type(exc).__name__)
        except Exception:
            pass
    return None


# --- listing projects ------------------------------------------------------------

def list_projects(client=None):
    """Cloud projects visible to the account, newest first.

    `[{cloud_project_id, labels, machine, last_writer, updated, files}]`, built from a
    `claude-code:` prefix list grouped by id, with `_project` cards read for labels.
    """
    client = client or Client()
    groups = {}
    for entry in _iter_entries(client, KEY_PREFIX):
        key = entry.get("key")
        if not isinstance(key, str) or not key.startswith(KEY_PREFIX):
            continue
        parts = key[len(KEY_PREFIX):].split(":", 1)
        if len(parts) != 2 or not _CPID_RE.match(parts[0]) or not parts[1]:
            continue
        cpid, rest = parts
        group = groups.setdefault(
            cpid, {"cloud_project_id": cpid, "labels": {}, "machine": None, "last_writer": None, "updated": None, "files": 0}
        )
        value = _parse_value(entry.get("value")) or {}
        updated = entry.get("updatedAt") if isinstance(entry.get("updatedAt"), str) else None
        if rest == CARD_NAME:
            if isinstance(value.get("labels"), dict):
                group["labels"] = {str(k): v for k, v in value["labels"].items() if isinstance(v, str)}
            if isinstance(value.get("machine"), str):
                group["machine"] = value["machine"]
            continue
        group["files"] += 1
        if updated and (group["updated"] is None or updated > group["updated"]):
            group["updated"] = updated
            if isinstance(value.get("machine"), str):
                group["last_writer"] = value["machine"]
    return sorted(groups.values(), key=lambda g: g["updated"] or "", reverse=True)


# --- pull ------------------------------------------------------------------------

def _local_view(journal, project_id, root, relpath):
    """(exists, sha, text, journal_entry, disk_unjournaled) for the local copy."""
    current = journal.current(project_id, relpath)
    j_exists = bool(current) and current.get("op") != "delete" and bool(current.get("sha"))
    j_sha = current.get("sha") if j_exists else None
    exists, sha, unjournaled = j_exists, j_sha, False
    data = None
    if root is not None:
        try:
            data = paths.read_regular(paths.resolve_target(root, relpath))
        except (OSError, ValueError):
            data = None
        exists = data is not None
        sha = _sha256(data) if data is not None else None
        unjournaled = sha != j_sha
    elif j_exists:
        try:
            data = journal.get_object(j_sha)
        except Exception:
            data = None
    text = data.decode("utf-8", "replace") if data is not None else None
    return exists, sha, text, current, unjournaled


def pull_plan(journal, local_project_id, cloud_project_id, client=None, *, root=None):
    """Describe what a pull would do, without touching anything.

    Each item: `{relpath, cloud_version, content, source, local_state, preview, foreign, ...}`.
    `local_state` is one of same | absent | differs | changed_since_sync. Items that
    are `foreign` (not written by this plugin, a skipped/redacted version, an invalid
    path, or a value that fails the secret scan) are shown but must never be written.
    Pass `root` (the memory dir) to compare against the real file instead of the journal.
    """
    _check_cpid(cloud_project_id)
    client = client or Client()
    manifest = _manifest(journal, local_project_id)
    cloud = _cloud_section(manifest)
    linked = cloud.get("cloud_project_id") == cloud_project_id
    skipped_versions = {s.get("cloud_version") for s in cloud["skipped"] if s.get("cloud_version")} if linked else set()
    synced_versions = cloud["versions"] if linked else {}
    prefix = _key(cloud_project_id, "")
    items = []
    for entry in _iter_entries(client, prefix):
        key = entry.get("key")
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        raw_rel = key[len(prefix):]
        if raw_rel == CARD_NAME:
            continue
        version = _first(entry, "entryVersion", "entry_version")
        value = _parse_value(entry.get("value"))
        source = entry.get("source") if isinstance(entry.get("source"), str) else None
        item = PullItem(
            relpath=None,
            key=key,
            cloud_project_id=cloud_project_id,
            cloud_version=version,
            content=None,
            op="write",
            sha=None,
            source=source,
            machine=None,
            time=None,
            local_state="absent",
            preview="",
            foreign=False,
            excluded=None,
            warnings=[],
        )
        try:
            relpath = paths.validate_relpath(raw_rel)
        except (ValueError, TypeError):
            item.update(relpath=paths.display_name(raw_rel), foreign=True, excluded="invalid_path")
            items.append(item)
            continue
        item["relpath"] = relpath
        reasons = []
        if source != SOURCE:
            reasons.append("foreign_source")
        if value is None or value.get("v") != 1:
            reasons.append("foreign_value")
            value = {}
        elif value.get("relpath") != relpath:
            reasons.append("relpath_mismatch")
        if version in skipped_versions:
            reasons.append("skipped_version")
        content = value.get("content")
        op = value.get("op") if value.get("op") in ("write", "baseline", "delete") else "write"
        if op == "delete":
            content = None
        elif not isinstance(content, str):
            if "foreign_value" not in reasons:
                reasons.append("no_content")
            content = None
        if content is not None:
            try:
                if secrets_scan.scan(content):
                    reasons.append("secret_in_value")
            except Exception:
                reasons.append("scan_error")
            if "[REDACTED" in content:
                item["warnings"].append("redaction_marker")
        sha = value.get("sha") if isinstance(value.get("sha"), str) else None
        if content is not None and sha != _sha256(content.encode("utf-8")):
            sha = _sha256(content.encode("utf-8"))  # never trust the claimed hash
        item.update(
            content=content,
            op=op,
            sha=None if op == "delete" else sha,
            machine=value.get("machine") if isinstance(value.get("machine"), str) else None,
            time=value.get("time") if isinstance(value.get("time"), str) else None,
            preview=(content or "")[:PREVIEW_CHARS],
            foreign=bool(reasons),
            excluded=reasons[0] if reasons else None,
        )
        if reasons:
            item["reasons"] = reasons
        exists, local_sha, local_text, current, unjournaled = _local_view(journal, local_project_id, root, relpath)
        if op == "delete":
            state = "same" if not exists else "differs"
        elif not exists:
            state = "absent"
        elif local_sha == item["sha"]:
            state = "same"
        else:
            state = "differs"
        if state == "differs":
            synced = synced_versions.get(relpath)
            cur_seq = (current or {}).get("seq", 0)
            if synced and (cur_seq > int(synced.get("seq", 0)) or unjournaled):
                state = "changed_since_sync"
        item["local_state"] = state
        if exists:
            item["local_content"] = local_text
            item["local_sha"] = local_sha
        items.append(item)
    items.sort(key=lambda i: (i["relpath"] or ""))
    return items


def _journal_existing(journal, project_id, relpath, data):
    """Journal the file as it is on disk right now if the journal doesn't have it yet."""
    sha = journal.put_object(data)
    current = journal.current(project_id, relpath)
    if current is None or current.get("op") == "delete" or current.get("sha") != sha:
        journal.append(project_id, relpath=relpath, sha=sha, op="write", source="external")


def pull_apply(journal, local_project_id, items, root, *, link=True, allow_foreign=False):
    """Write chosen pull items into `root` and journal each as `source: pull`.

    Existing local content that the journal doesn't hold yet is journaled first, so
    every overwrite stays undoable. With `link`, the manifest adopts the Cloud
    project id and records each item's Cloud version, so a later push continues the
    same history instead of forking it. Returns `{written, deleted, unchanged, refused, linked}`.
    """
    written, deleted, unchanged, refused = [], [], [], []
    synced = {}
    cpids = set()
    for item in items:
        relpath = item.get("relpath")
        try:
            relpath = paths.validate_relpath(relpath)
            target = paths.resolve_target(root, relpath)
        except (ValueError, TypeError, OSError):
            refused.append({"relpath": paths.display_name(str(relpath)), "reason": "invalid_path"})
            continue
        reasons = set(item.get("reasons") or ([item["excluded"]] if item.get("excluded") else []))
        if "invalid_path" in reasons or (item.get("foreign") and not (allow_foreign and reasons <= {"foreign_source"})):
            refused.append({"relpath": relpath, "reason": item.get("excluded") or "foreign"})
            continue
        if item.get("cloud_project_id"):
            cpids.add(item["cloud_project_id"])
        op = item.get("op")
        content = item.get("content")
        try:
            existing = paths.read_regular(target)
        except (OSError, ValueError):
            existing = None
        if op == "delete":
            if existing is None:
                unchanged.append(relpath)
            else:
                if target.is_symlink():
                    refused.append({"relpath": relpath, "reason": "symlink"})
                    continue
                _journal_existing(journal, local_project_id, relpath, existing)
                try:
                    os.unlink(str(target))
                except OSError:
                    refused.append({"relpath": relpath, "reason": "unsafe_target"})
                    continue
                entry = journal.append(local_project_id, relpath=relpath, sha=None, op="delete", source="pull")
                deleted.append(relpath)
                synced[relpath] = {"seq": entry["seq"], "cloud_version": item.get("cloud_version")}
            continue
        if not isinstance(content, str):
            refused.append({"relpath": relpath, "reason": "no_content"})
            continue
        data = content.encode("utf-8")
        if existing is not None and existing == data:
            current = journal.current(local_project_id, relpath)
            if current is None or current.get("sha") != _sha256(data) or current.get("op") == "delete":
                _journal_existing(journal, local_project_id, relpath, existing)
                current = journal.current(local_project_id, relpath)
            unchanged.append(relpath)
            synced[relpath] = {"seq": (current or {}).get("seq", 0), "cloud_version": item.get("cloud_version")}
            continue
        if existing is not None:
            _journal_existing(journal, local_project_id, relpath, existing)
        try:
            paths.atomic_write(target, data)
        except (OSError, ValueError):
            refused.append({"relpath": relpath, "reason": "unsafe_target"})
            continue
        sha = journal.put_object(data)
        entry = journal.append(local_project_id, relpath=relpath, sha=sha, op="write", source="pull")
        written.append(relpath)
        synced[relpath] = {"seq": entry["seq"], "cloud_version": item.get("cloud_version")}

    linked = None
    if link and len(cpids) == 1 and synced:
        linked = next(iter(cpids))

        def mutate(cloud, _manifest_):
            if cloud.get("cloud_project_id") != linked:
                _reset_sync_state(cloud)
                cloud["cloud_project_id"] = linked
            for relpath, record in synced.items():
                if record.get("cloud_version"):
                    cloud["versions"][relpath] = record

        _update(journal, local_project_id, mutate)
    return {"written": written, "deleted": deleted, "unchanged": unchanged, "refused": refused, "linked": linked}


def link(journal, local_project_id, cloud_project_id, *, client=None, root=None):
    """Point a local project at an existing Cloud project without pulling anything.

    Files whose local content already equals the Cloud head are marked as synced, so
    the next push does not replay their older history. Everything else is pushed
    normally (and a differing Cloud copy is reported as a conflict, never overwritten).
    """
    _check_cpid(cloud_project_id)
    plan = pull_plan(journal, local_project_id, cloud_project_id, client, root=root)
    same = {}
    for item in plan:
        if item["foreign"] or item["local_state"] != "same" or not item.get("cloud_version"):
            continue
        current = journal.current(local_project_id, item["relpath"])
        if current:
            same[item["relpath"]] = {"seq": current["seq"], "cloud_version": item["cloud_version"]}

    def mutate(cloud, _manifest_):
        if cloud.get("cloud_project_id") != cloud_project_id:
            _reset_sync_state(cloud)
            cloud["cloud_project_id"] = cloud_project_id
        cloud["versions"].update(same)

    _update(journal, local_project_id, mutate)
    return {"cloud_project_id": cloud_project_id, "synced": sorted(same)}
