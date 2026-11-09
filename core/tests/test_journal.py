"""Tests for bilinc_memory.journal (stdlib unittest, temp dirs only)."""
import hashlib
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

LIB = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
sys.path.insert(0, LIB)

from bilinc_memory import journal, paths  # noqa: E402

POSIX = os.name != "nt"


# -- multiprocessing workers (module level so spawn can import them) ---------

def _append_worker(args):
    store, pid, wid, n = args
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    from bilinc_memory import journal as jr
    j = jr.Journal(Path(store))
    seqs = []
    for i in range(n):
        sha = j.put_object(("worker %d item %d" % (wid, i)).encode())
        # half the writes hit one shared relpath, half a per-worker relpath
        rel = "shared.md" if i % 2 == 0 else "w%d.md" % wid
        seqs.append(j.append(pid, relpath=rel, sha=sha, op="write", source="agent",
                             session_id="s%d" % wid)["seq"])
    return seqs


def _manifest_worker(args):
    store, pid, n = args
    if LIB not in sys.path:
        sys.path.insert(0, LIB)
    from bilinc_memory import journal as jr
    j = jr.Journal(Path(store))

    def bump(m):
        m["cloud"]["pushed_seq"] = m["cloud"]["pushed_seq"] + 1

    for _ in range(n):
        j.update_manifest(pid, bump)
    return n


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.claude = self.tmp / "claude"
        self.claude.mkdir()
        self.store = self.tmp / "store"
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude),
                                               "BILINC_MEMORY_HOME": str(self.store)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.j = journal.Journal(self.store)
        self.target = paths.Target("project", "-Users-x-proj", self.claude / "projects" / "-Users-x-proj" / "memory",
                                   "a.md")
        self.p = self.j.project_for(self.target)
        self.pid = self.p.id

    def put_append(self, rel, text, op="write", source="agent", **kw):
        sha = self.j.put_object(text.encode()) if text is not None else None
        return self.j.append(self.pid, relpath=rel, sha=sha, op=op, source=source, **kw)

    def log_path(self, pid=None):
        return self.store / "projects" / (pid or self.pid) / "log.jsonl"


class TestProjects(Base):
    def test_manifest_shape_and_uuid(self):
        import uuid
        m = self.p
        self.assertEqual(str(uuid.UUID(m["id"], version=4)), m["id"])
        self.assertEqual(m["slug"], "-Users-x-proj")
        self.assertEqual(m["labels"], {})
        self.assertEqual(m["cloud"], {"enabled": False, "cloud_project_id": None, "pushed_seq": 0,
                                      "versions": {}, "skipped": []})
        self.assertRegex(m["created"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(m.id, m["id"])
        self.assertEqual(m.slug, "-Users-x-proj")
        on_disk = json.loads((self.store / "projects" / self.pid / "manifest.json").read_text())
        self.assertEqual(on_disk, dict(m))

    def test_stable_and_distinct(self):
        again = self.j.project_for(self.target)
        self.assertEqual(again.id, self.pid)
        other = self.j.project_for(paths.Target("project", "-Users-x-other", Path("/x"), "a.md"))
        self.assertNotEqual(other.id, self.pid)
        slugs = json.loads((self.store / "slugs.json").read_text())
        self.assertEqual(slugs, {"-Users-x-proj": self.pid, "-Users-x-other": other.id})

    def test_global_project(self):
        g = self.j.project_for(paths.Target("global", None, self.claude, "CLAUDE.md"))
        self.assertEqual(g.id, "global")
        self.assertIsNone(g.slug)
        self.assertEqual(self.j.project_for(paths.Target("global", None, self.claude, "CLAUDE.md")).id, "global")
        self.assertIn("global", self.j.projects())

    def test_project_target_needs_slug(self):
        with self.assertRaises(ValueError):
            self.j.project_for(paths.Target("project", None, Path("/x"), "a.md"))

    def test_survives_new_journal_instance(self):
        again = journal.Journal(self.store).project_for(self.target)
        self.assertEqual(again.id, self.pid)

    def test_slugs_rebuilt_when_corrupt_or_missing(self):
        (self.store / "slugs.json").write_text("{not json")
        self.assertEqual(self.j.project_for(self.target).id, self.pid)
        os.unlink(self.store / "slugs.json")
        self.assertEqual(self.j.project_for(self.target).id, self.pid)
        self.assertEqual(json.loads((self.store / "slugs.json").read_text()), {"-Users-x-proj": self.pid})

    def test_labels_added_not_overwritten(self):
        m = self.j.project_for(self.target, labels={"cwd": "/a", "git_remote": None})
        self.assertEqual(m.labels, {"cwd": "/a"})
        m = self.j.project_for(self.target, labels={"cwd": "/b", "git_remote": "git@x:y"})
        self.assertEqual(m.labels, {"cwd": "/a", "git_remote": "git@x:y"})

    def test_manifest_read(self):
        self.assertEqual(dict(self.j.manifest(self.pid)), dict(self.p))
        with self.assertRaises(journal.JournalError):
            self.j.manifest("nope")

    @unittest.skipUnless(POSIX, "modes are POSIX")
    def test_permissions_private(self):
        self.put_append("a.md", "x")
        self.j.save_stat_cache(self.pid, {"a.md": [1, 2, "s"]})
        for d in (self.store, self.store / "objects", self.store / "projects", self.store / "projects" / self.pid):
            self.assertEqual(stat.S_IMODE(os.lstat(d).st_mode), 0o700, d)
        for dirpath, _d, files in os.walk(self.store):
            for name in files:
                self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(dirpath, name)).st_mode), 0o600, name)

    def test_bad_project_ids(self):
        for bad in ("../x", "a/b", "", "..", ".hidden", None, 5, "x" * 65):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.j.entries(bad)

    def test_unknown_project(self):
        with self.assertRaises(journal.JournalError):
            self.j.entries("does-not-exist")
        with self.assertRaises(journal.JournalError):
            self.j.append("does-not-exist", relpath="a.md", sha=None, op="delete", source="agent")
        self.assertFalse((self.store / "projects" / "does-not-exist").exists())


class TestObjects(Base):
    def test_roundtrip_and_dedupe(self):
        sha = self.j.put_object(b"hello")
        self.assertEqual(sha, sha_of(b"hello"))
        self.assertEqual(self.j.get_object(sha), b"hello")
        self.assertEqual(self.j.put_object(b"hello"), sha)
        self.assertEqual(os.listdir(self.store / "objects"), [sha])
        self.assertEqual(self.j.get_object(self.j.put_object(b"")), b"")

    def test_large_object(self):
        data = os.urandom(1_048_576)
        self.assertEqual(self.j.get_object(self.j.put_object(data)), data)

    def test_no_temp_files_left(self):
        self.j.put_object(b"a")
        self.j.put_object(b"b")
        self.assertTrue(all(len(n) == 64 for n in os.listdir(self.store / "objects")))

    def test_get_verifies_sha(self):
        sha = self.j.put_object(b"original")
        with open(self.store / "objects" / sha, "wb") as fh:
            fh.write(b"tampered")
        with self.assertRaises(journal.ObjectCorrupt):
            self.j.get_object(sha)

    def test_put_repairs_corrupt_object(self):
        sha = self.j.put_object(b"original")
        with open(self.store / "objects" / sha, "wb") as fh:
            fh.write(b"tampered")
        self.assertEqual(self.j.put_object(b"original"), sha)
        self.assertEqual(self.j.get_object(sha), b"original")

    def test_put_verifies_after_rename(self):
        with mock.patch.object(journal, "_file_sha", return_value="0" * 64):
            with self.assertRaises(journal.ObjectCorrupt):
                self.j.put_object(b"data")
        self.assertEqual(os.listdir(self.store / "objects"), [])

    def test_missing_object(self):
        with self.assertRaises(journal.ObjectMissing):
            self.j.get_object("a" * 64)
        with self.assertRaises(FileNotFoundError):
            self.j.get_object("a" * 64)

    def test_invalid_sha_rejected(self):
        for bad in ("../../etc/passwd", "abc", "A" * 64, "g" * 64, "", None):
            with self.subTest(bad=bad):
                with self.assertRaises((ValueError, TypeError)):
                    self.j.get_object(bad)

    def test_put_rejects_str(self):
        with self.assertRaises(TypeError):
            self.j.put_object("text")

    @unittest.skipUnless(POSIX, "symlinks are POSIX")
    def test_symlinked_object_not_followed(self):
        secret = self.tmp / "secret"
        secret.write_bytes(b"s")
        sha = sha_of(b"s")
        os.symlink(secret, self.store / "objects" / sha)
        with self.assertRaises(journal.ObjectCorrupt):
            self.j.get_object(sha)
        # put replaces the link with a real object, the target is untouched
        self.assertEqual(self.j.put_object(b"s"), sha)
        self.assertFalse(os.path.islink(self.store / "objects" / sha))
        self.assertEqual(secret.read_bytes(), b"s")


class TestAppend(Base):
    def test_seq_and_fields(self):
        e1 = self.put_append("a.md", "one", session_id="s1", tool_use_id="t1", transcript_path="/t.jsonl")
        e2 = self.put_append("b.md", "two", source="external")
        self.assertEqual((e1["seq"], e2["seq"]), (1, 2))
        self.assertEqual(e1["relpath"], "a.md")
        self.assertEqual(e1["sha"], sha_of(b"one"))
        self.assertEqual((e1["op"], e1["source"]), ("write", "agent"))
        self.assertEqual((e1["session_id"], e1["tool_use_id"], e1["transcript_path"]), ("s1", "t1", "/t.jsonl"))
        self.assertIsNone(e2["session_id"])
        self.assertRegex(e1["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(self.j.entries(self.pid), [e1, e2])

    def test_relpath_normalised(self):
        e = self.put_append("./topics/a.md", "x")
        self.assertEqual(e["relpath"], "topics/a.md")

    def test_validation(self):
        sha = self.j.put_object(b"x")
        bad = [
            dict(relpath="../x", sha=sha, op="write", source="agent"),
            dict(relpath="/abs", sha=sha, op="write", source="agent"),
            dict(relpath="a.md", sha=sha, op="rename", source="agent"),
            dict(relpath="a.md", sha=sha, op="write", source="robot"),
            dict(relpath="a.md", sha=None, op="write", source="agent"),
            dict(relpath="a.md", sha="nothex", op="write", source="agent"),
            dict(relpath="a.md", sha=sha, op="delete", source="agent"),
            dict(relpath="a.md", sha="b" * 64, op="write", source="agent"),  # object not stored
        ]
        for kw in bad:
            with self.subTest(kw=kw):
                with self.assertRaises((ValueError, journal.JournalError)):
                    self.j.append(self.pid, **kw)
        self.assertEqual(self.j.entries(self.pid), [])

    def test_all_ops_and_sources_accepted(self):
        sha = self.j.put_object(b"x")
        for op, src in (("write", "claude"), ("baseline", "baseline"), ("write", "external"),
                        ("write", "undo"), ("write", "pull")):
            self.j.append(self.pid, relpath="%s-%s.md" % (op, src), sha=sha, op=op, source=src)
        self.j.append(self.pid, relpath="gone.md", sha=None, op="delete", source="external")
        self.assertEqual(len(self.j.entries(self.pid)), 6)

    def test_no_change_dedupe(self):
        e1 = self.put_append("a.md", "same")
        e2 = self.put_append("a.md", "same", source="external", session_id="other")
        self.assertEqual(e2, e1)
        self.assertEqual(len(self.j.entries(self.pid)), 1)
        self.assertEqual(self.log_path().read_text().count("\n"), 1)

    def test_dedupe_only_against_last_entry_of_same_relpath(self):
        self.put_append("a.md", "v1")
        self.put_append("b.md", "v1")  # same content, other file: not a duplicate
        self.put_append("a.md", "v2")
        e = self.put_append("a.md", "v1")  # back to v1: a real change
        self.assertEqual(e["seq"], 4)
        self.assertEqual(len(self.j.entries(self.pid)), 4)

    def test_dedupe_needs_same_op(self):
        base = self.put_append("a.md", "x", op="baseline", source="baseline")
        w = self.put_append("a.md", "x", op="write")
        self.assertEqual((base["seq"], w["seq"]), (1, 2))

    def test_delete_dedupe_and_recreate(self):
        self.put_append("a.md", "x")
        d1 = self.put_append("a.md", None, op="delete", source="external")
        d2 = self.put_append("a.md", None, op="delete", source="external")
        self.assertEqual(d1, d2)
        e = self.put_append("a.md", "x")
        self.assertEqual(e["seq"], 3)

    def test_dedupe_scans_past_other_relpaths(self):
        first = self.put_append("rare.md", "r")
        for i in range(50):
            self.put_append("busy.md", "v%d" % i)
        self.assertEqual(self.put_append("rare.md", "r"), first)

    def test_dedupe_skips_purged_entry(self):
        e = self.put_append("a.md", "secret")
        self.j.purge_object(e["sha"])
        e2 = self.put_append("a.md", "secret")
        self.assertEqual(e2["seq"], 2)
        self.assertEqual(self.j.get_object(e2["sha"]), b"secret")

    def test_seq_per_project(self):
        other = self.j.project_for(paths.Target("project", "-other", Path("/x"), "a.md"))
        self.put_append("a.md", "x")
        sha = self.j.put_object(b"y")
        e = self.j.append(other.id, relpath="a.md", sha=sha, op="write", source="agent")
        self.assertEqual(e["seq"], 1)

    def test_unicode_relpath_roundtrip(self):
        e = self.put_append("日本語/ünï\u2028x.md", "x")
        self.assertEqual(self.j.entries(self.pid)[0]["relpath"], e["relpath"])
        # one physical line per entry even with U+2028 in the name
        self.assertEqual(len(self.log_path().read_bytes().split(b"\n")), 2)


class TestTornLog(Base):
    def test_reader_skips_torn_last_line(self):
        self.put_append("a.md", "one")
        self.put_append("a.md", "two")
        with open(self.log_path(), "ab") as fh:
            fh.write(b'{"seq":3,"time":"2026-11-09T00:00:00.000Z","relp')
        got = self.j.entries(self.pid)
        self.assertEqual([e["seq"] for e in got], [1, 2])
        self.assertEqual(self.j.current(self.pid, "a.md")["seq"], 2)

    def test_append_after_torn_line(self):
        self.put_append("a.md", "one")
        with open(self.log_path(), "ab") as fh:
            fh.write(b'{"seq":2,"relpa')
        e = self.put_append("a.md", "two")
        self.assertEqual(e["seq"], 2)  # the torn line never counted
        got = self.j.entries(self.pid)
        self.assertEqual([x["seq"] for x in got], [1, 2])
        self.assertEqual(got[1]["sha"], sha_of(b"two"))
        e3 = self.put_append("b.md", "three")
        self.assertEqual(e3["seq"], 3)
        self.assertEqual(len(self.j.entries(self.pid)), 3)

    def test_valid_last_line_without_newline(self):
        self.put_append("a.md", "one")
        raw = self.log_path().read_bytes()
        self.log_path().write_bytes(raw.rstrip(b"\n"))
        e = self.put_append("a.md", "two")
        self.assertEqual(e["seq"], 2)
        self.assertEqual([x["seq"] for x in self.j.entries(self.pid)], [1, 2])

    def test_garbage_and_binary_lines_skipped(self):
        self.put_append("a.md", "one")
        with open(self.log_path(), "ab") as fh:
            fh.write(b'\xff\xfe\n[1,2]\n{"seq":"x"}\n{"seq":true}\n\n')
        self.put_append("a.md", "two")
        self.assertEqual([x["seq"] for x in self.j.entries(self.pid)], [1, 2])

    def test_torn_only_file(self):
        self.log_path().write_bytes(b'{"seq":1,"rel')
        self.assertEqual(self.j.entries(self.pid), [])
        self.assertIsNone(self.j.current(self.pid, "a.md"))
        self.assertEqual(self.put_append("a.md", "x")["seq"], 1)

    def test_empty_log(self):
        self.assertEqual(self.j.entries(self.pid), [])
        self.assertEqual(self.j.relpaths(self.pid), [])


class TestQueries(Base):
    def test_entries_filter_current_relpaths_deleted(self):
        self.put_append("a.md", "a1")
        self.put_append("b.md", "b1")
        self.put_append("a.md", "a2")
        self.put_append("c.md", "c1")
        self.put_append("b.md", None, op="delete", source="external")
        self.assertEqual([e["seq"] for e in self.j.entries(self.pid, "a.md")], [1, 3])
        self.assertEqual(self.j.entries(self.pid, "nope.md"), [])
        self.assertEqual(self.j.current(self.pid, "a.md")["sha"], sha_of(b"a2"))
        self.assertEqual(self.j.current(self.pid, "b.md")["op"], "delete")
        self.assertIsNone(self.j.current(self.pid, "zzz.md"))
        self.assertEqual(self.j.relpaths(self.pid), ["a.md", "c.md"])
        self.assertEqual(self.j.deleted(self.pid), ["b.md"])

    def test_deleted_then_restored(self):
        self.put_append("a.md", "x")
        self.put_append("a.md", None, op="delete", source="external")
        self.assertEqual(self.j.deleted(self.pid), ["a.md"])
        self.put_append("a.md", "x", source="undo")
        self.assertEqual(self.j.deleted(self.pid), [])
        self.assertEqual(self.j.relpaths(self.pid), ["a.md"])

    def test_stat_cache(self):
        self.assertEqual(self.j.stat_cache(self.pid), {})
        cache = {"a.md": [123456789, 10, sha_of(b"x")]}
        self.j.save_stat_cache(self.pid, cache)
        self.assertEqual(self.j.stat_cache(self.pid), cache)
        (self.store / "projects" / self.pid / "stat_cache.json").write_text("{broken")
        self.assertEqual(self.j.stat_cache(self.pid), {})

    def test_size_bytes(self):
        before = self.j.size_bytes()
        self.j.put_object(b"x" * 5000)
        self.assertGreaterEqual(self.j.size_bytes(), before + 5000)


class TestConcurrency(Base):
    def test_concurrent_appends_no_gaps_no_duplicates(self):
        workers, per = 8, 25
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(workers) as pool:
            res = pool.map(_append_worker, [(str(self.store), self.pid, w, per) for w in range(workers)])
        returned = sorted(s for r in res for s in r)
        total = workers * per
        self.assertEqual(returned, list(range(1, total + 1)))
        entries = self.j.entries(self.pid)
        self.assertEqual([e["seq"] for e in entries], list(range(1, total + 1)))
        # the file itself: one valid JSON line per entry, nothing interleaved
        lines = self.log_path().read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        self.assertEqual(len(lines) - 1, total)
        for line in lines[:-1]:
            json.loads(line)
        # every referenced object exists and verifies
        for e in entries:
            self.assertEqual(sha_of(self.j.get_object(e["sha"])), e["sha"])

    def test_concurrent_manifest_updates_do_not_lose_writes(self):
        workers, per = 8, 15
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(workers) as pool:
            pool.map(_manifest_worker, [(str(self.store), self.pid, per)] * workers)
        self.assertEqual(self.j.manifest(self.pid)["cloud"]["pushed_seq"], workers * per)

    def test_manifest_lock_is_the_append_lock(self):
        order = []

        def background():
            sha = self.j.put_object(b"bg")
            self.j.append(self.pid, relpath="a.md", sha=sha, op="write", source="agent")
            order.append("appended")

        def fn(m):
            t = threading.Thread(target=background)
            t.start()
            time.sleep(0.4)
            order.append("manifest-done")
            self.assertEqual(self.j.entries(self.pid), [])  # append is still waiting
            fn.thread = t
            m["labels"]["k"] = "v"

        self.j.update_manifest(self.pid, fn)
        fn.thread.join(10)
        self.assertEqual(order, ["manifest-done", "appended"])
        self.assertEqual(self.j.manifest(self.pid).labels, {"k": "v"})

    def test_update_manifest_semantics(self):
        out = self.j.update_manifest(self.pid, lambda m: m["cloud"].update(enabled=True))
        self.assertTrue(out["cloud"]["enabled"])
        replaced = self.j.update_manifest(self.pid, lambda m: dict(m, labels={"cwd": "/z"}))
        self.assertEqual(replaced.labels, {"cwd": "/z"})
        self.assertEqual(self.j.manifest(self.pid).labels, {"cwd": "/z"})
        self.assertEqual(self.j.manifest(self.pid).id, self.pid)

    def test_update_manifest_error_leaves_manifest(self):
        def boom(m):
            m["labels"]["x"] = 1
            raise RuntimeError("nope")
        with self.assertRaises(RuntimeError):
            self.j.update_manifest(self.pid, boom)
        self.assertEqual(self.j.manifest(self.pid).labels, {})
        # lock released: the next write works
        self.put_append("a.md", "x")

    def test_update_manifest_can_append_reentrantly(self):
        sha = self.j.put_object(b"x")
        self.j.update_manifest(self.pid, lambda m: self.j.append(
            self.pid, relpath="a.md", sha=sha, op="write", source="agent"))
        self.assertEqual(len(self.j.entries(self.pid)), 1)

    def test_lock_timeout(self):
        lock = str(self.store / "projects" / self.pid / "log.lock")
        got = []

        def contender():
            try:
                with journal._file_lock(lock, timeout=0.1):
                    got.append("acquired")
            except journal.JournalLockTimeout:
                got.append("timeout")

        with journal._file_lock(lock):
            t = threading.Thread(target=contender)
            t.start()
            t.join(5)
        self.assertEqual(got, ["timeout"])
        with journal._file_lock(lock, timeout=0.1):  # released afterwards
            pass


class TestPrune(Base):
    def versions(self, rel, n):
        return [self.put_append(rel, "%s v%d" % (rel, i)) for i in range(n)]

    def test_keeps_newest_n_per_file(self):
        self.versions("a.md", 10)
        self.versions("b.md", 3)
        res = self.j.prune(4, grace_seconds=0)
        self.assertEqual(res["entries_dropped"], 6)
        a = self.j.entries(self.pid, "a.md")
        self.assertEqual(len(a), 4)
        self.assertEqual(self.j.get_object(a[-1]["sha"]), b"a.md v9")
        self.assertEqual([e["sha"] for e in a], [sha_of(("a.md v%d" % i).encode()) for i in range(6, 10)])
        self.assertEqual(len(self.j.entries(self.pid, "b.md")), 3)

    def test_removes_unreferenced_objects_only(self):
        self.versions("a.md", 5)
        orphan = self.j.put_object(b"orphan")
        res = self.j.prune(2, grace_seconds=0)
        self.assertEqual(res["objects_removed"], 4)  # 3 dropped versions + the orphan
        self.assertFalse(self.j.has_object(orphan))
        for e in self.j.entries(self.pid):
            self.assertEqual(sha_of(self.j.get_object(e["sha"])), e["sha"])
        self.assertEqual(len(os.listdir(self.store / "objects")), 2)

    def test_never_drops_newest_version(self):
        self.versions("a.md", 5)
        for bad_cap in (1, 0, -3):
            self.j.prune(bad_cap, grace_seconds=0)
            es = self.j.entries(self.pid, "a.md")
            self.assertEqual(len(es), 1)
            self.assertEqual(self.j.get_object(es[0]["sha"]), b"a.md v4")

    def test_keeps_newest_delete_entry(self):
        self.versions("a.md", 3)
        self.put_append("a.md", None, op="delete", source="external")
        self.j.prune(1, grace_seconds=0)
        self.assertEqual(self.j.deleted(self.pid), ["a.md"])

    def test_shared_object_survives_while_referenced(self):
        other = self.j.project_for(paths.Target("project", "-other", Path("/x"), "a.md"))
        self.versions("a.md", 4)  # the newest content is "a.md v3"
        sha = self.j.put_object(b"a.md v0")  # also referenced by another project
        self.j.append(other.id, relpath="z.md", sha=sha, op="write", source="agent")
        self.j.prune(1, grace_seconds=0)
        self.assertEqual(self.j.get_object(sha), b"a.md v0")

    def test_same_object_in_two_files_survives(self):
        self.put_append("a.md", "shared")
        self.put_append("a.md", "newer")
        self.put_append("b.md", "shared")
        self.j.prune(1, grace_seconds=0)
        self.assertEqual(self.j.get_object(sha_of(b"shared")), b"shared")

    def test_grace_keeps_young_objects(self):
        young = self.j.put_object(b"young")
        self.versions("a.md", 3)
        self.j.prune(1)  # default grace: nothing written seconds ago is swept
        self.assertTrue(self.j.has_object(young))
        self.assertEqual(len(self.j.entries(self.pid, "a.md")), 1)
        old = time.time() - 3600
        os.utime(self.store / "objects" / young, (old, old))
        self.j.prune(1)
        self.assertFalse(self.j.has_object(young))

    def test_seq_stays_monotonic_after_prune(self):
        self.versions("a.md", 5)
        self.j.prune(1, grace_seconds=0)
        e = self.put_append("a.md", "after")
        self.assertEqual(e["seq"], 6)

    def test_protects_unpushed_and_synced_versions_when_cloud_enabled(self):
        self.versions("a.md", 6)  # seq 1..6

        def enable(m):
            m["cloud"].update(enabled=True, cloud_project_id="c", pushed_seq=3)
            m["cloud"]["versions"] = {"a.md": {"seq": 2, "cloud_version": 2}}
        self.j.update_manifest(self.pid, enable)
        self.j.prune(1, grace_seconds=0)
        self.assertEqual([e["seq"] for e in self.j.entries(self.pid)], [2, 4, 5, 6])
        # once everything is pushed, only the synced reference and newest stay
        self.j.update_manifest(self.pid, lambda m: m["cloud"].update(pushed_seq=6))
        self.j.prune(1, grace_seconds=0)
        self.assertEqual([e["seq"] for e in self.j.entries(self.pid)], [2, 6])

    def test_cloud_disabled_does_not_protect(self):
        self.versions("a.md", 4)
        self.j.update_manifest(self.pid, lambda m: m["cloud"].update(pushed_seq=0))
        self.j.prune(1, grace_seconds=0)
        self.assertEqual([e["seq"] for e in self.j.entries(self.pid)], [4])

    def test_idempotent_and_noop(self):
        self.versions("a.md", 3)
        first = self.j.prune(200, grace_seconds=0)
        self.assertEqual(first, {"entries_dropped": 0, "objects_removed": 0, "bytes_freed": 0})
        self.j.prune(1, grace_seconds=0)
        again = self.j.prune(1, grace_seconds=0)
        self.assertEqual(again["entries_dropped"], 0)

    def test_default_cap_is_200(self):
        self.assertEqual(journal.DEFAULT_MAX_VERSIONS, 200)
        self.versions("a.md", 5)
        self.assertEqual(self.j.prune()["entries_dropped"], 0)

    def test_removes_stale_temp_files(self):
        stale = self.store / "objects" / ".tmp-stale"
        stale.write_bytes(b"x")
        old = time.time() - 7200
        os.utime(stale, (old, old))
        fresh = self.store / "objects" / ".tmp-fresh"
        fresh.write_bytes(b"x")
        self.j.prune(1, grace_seconds=0)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_prune_rewrites_log_without_torn_line(self):
        self.versions("a.md", 3)
        with open(self.log_path(), "ab") as fh:
            fh.write(b'{"seq":9,"rel')
        self.j.prune(1, grace_seconds=0)
        self.assertTrue(self.log_path().read_bytes().endswith(b"\n"))
        self.assertEqual([e["seq"] for e in self.j.entries(self.pid)], [3])


class TestPurgeAndWipe(Base):
    def test_purge_object(self):
        bad = self.put_append("a.md", "api key here")
        good = self.put_append("a.md", "clean")
        self.put_append("b.md", "api key here")  # same object, other file
        n = self.j.purge_object(bad["sha"])
        self.assertEqual(n, 2)
        self.assertFalse(self.j.has_object(bad["sha"]))
        with self.assertRaises(journal.ObjectMissing):
            self.j.get_object(bad["sha"])
        entries = self.j.entries(self.pid)
        self.assertEqual([bool(e.get("purged")) for e in entries], [True, False, True])
        self.assertEqual(entries[1], good)  # untouched entry is byte-identical in content
        self.assertEqual(self.j.get_object(good["sha"]), b"clean")
        self.assertEqual([e["seq"] for e in entries], [1, 2, 3])  # seq preserved
        self.assertEqual(self.j.purge_object(bad["sha"]), 0)  # idempotent

    def test_purge_across_projects(self):
        other = self.j.project_for(paths.Target("project", "-other", Path("/x"), "a.md"))
        sha = self.j.put_object(b"leaked")
        self.j.append(self.pid, relpath="a.md", sha=sha, op="write", source="agent")
        self.j.append(other.id, relpath="a.md", sha=sha, op="write", source="agent")
        self.assertEqual(self.j.purge_object(sha), 2)
        self.assertTrue(self.j.entries(other.id)[0]["purged"])

    def test_purge_unknown_and_invalid(self):
        self.assertEqual(self.j.purge_object("c" * 64), 0)
        with self.assertRaises(ValueError):
            self.j.purge_object("../../x")

    def test_purge_then_append_continues(self):
        e = self.put_append("a.md", "bad")
        self.j.purge_object(e["sha"])
        self.assertEqual(self.put_append("a.md", "next")["seq"], 2)

    def test_purged_object_not_counted_as_reference_by_prune(self):
        e = self.put_append("a.md", "bad")
        self.j.purge_object(e["sha"])
        self.put_append("a.md", "good")
        self.j.prune(5, grace_seconds=0)
        self.assertEqual(len(self.j.entries(self.pid)), 2)

    def test_wipe(self):
        self.put_append("a.md", "x")
        (self.store / "config.json").write_text("{}")
        self.j.wipe()
        self.assertEqual(sorted(os.listdir(self.store)), ["config.json"])
        self.assertEqual(self.j.size_bytes(), 2)
        # still usable afterwards, with a fresh project
        p = self.j.project_for(self.target)
        self.assertNotEqual(p.id, self.pid)

    def test_wipe_removes_empty_store(self):
        self.put_append("a.md", "x")
        self.j.wipe()
        self.assertFalse(self.store.exists())
        self.assertEqual(self.j.size_bytes(), 0)  # recreated on demand

    @unittest.skipUnless(POSIX, "modes are POSIX")
    def test_wipe_refuses_unsafe_store(self):
        self.put_append("a.md", "x")
        os.chmod(self.store, 0o755)
        with self.assertRaises(paths.UnsafeStore):
            self.j.wipe()
        self.assertTrue((self.store / "projects").exists())


@unittest.skipUnless(POSIX, "modes and symlinks are POSIX")
class TestUnsafeStore(Base):
    def test_loose_mode_refused(self):
        bad = self.tmp / "loose"
        bad.mkdir()
        os.chmod(bad, 0o755)
        with self.assertRaises(paths.UnsafeStore):
            journal.Journal(bad)
        self.assertEqual(os.listdir(bad), [])  # nothing written into it

    def test_symlinked_store_refused(self):
        real = self.tmp / "real"
        real.mkdir(mode=0o700)
        link = self.tmp / "link"
        os.symlink(real, link)
        with self.assertRaises(paths.UnsafeStore):
            journal.Journal(link)
        self.assertEqual(os.listdir(real), [])

    def test_default_store_from_env(self):
        env_store = self.tmp / "envstore"
        with mock.patch.dict(os.environ, {"BILINC_MEMORY_HOME": str(env_store)}):
            j = journal.Journal()
            self.assertEqual(j.store, env_store)
        self.assertEqual(stat.S_IMODE(os.lstat(env_store).st_mode), 0o700)

    def test_env_store_unsafe(self):
        env_store = self.tmp / "envstore"
        env_store.mkdir()
        os.chmod(env_store, 0o777)
        with mock.patch.dict(os.environ, {"BILINC_MEMORY_HOME": str(env_store)}):
            with self.assertRaises(paths.UnsafeStore):
                journal.Journal()

    def test_store_tampered_after_open_is_refused_on_next_call(self):
        os.chmod(self.store, 0o755)
        for call in (lambda: self.j.put_object(b"x"), lambda: self.j.entries(self.pid),
                     lambda: self.j.project_for(self.target), lambda: self.j.size_bytes()):
            with self.assertRaises(paths.UnsafeStore):
                call()

    def test_store_swapped_for_symlink_is_refused(self):
        moved = self.tmp / "moved"
        os.rename(self.store, moved)
        os.symlink(moved, self.store)
        with self.assertRaises(paths.UnsafeStore):
            self.j.put_object(b"x")

    def test_log_symlink_not_followed(self):
        victim = self.tmp / "victim.txt"
        victim.write_text("keep")
        os.symlink(victim, self.log_path())
        sha = self.j.put_object(b"x")
        with self.assertRaises(OSError):
            self.j.append(self.pid, relpath="a.md", sha=sha, op="write", source="agent")
        self.assertEqual(victim.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
