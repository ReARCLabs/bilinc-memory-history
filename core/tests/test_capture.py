"""Tests for lib/bilinc_memory/capture.py and hooks/hook.py.

Run alone:  python3 -m unittest tests.test_capture   (from the repo root)

Uses the real bilinc_memory.paths / bilinc_memory.journal; only Cloud is replaced by a
recording fake, so no test ever touches the network.
"""
import collections
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(os.path.dirname(CORE), "claude-code")  # the Claude Code plugin
LIB = CORE
HOOK_PY = os.path.join(ROOT, "hooks", "hook.py")
RUN_SH = os.path.join(ROOT, "hooks", "run.sh")
if LIB not in sys.path:
    sys.path.insert(0, LIB)


from bilinc_memory import capture, journal, paths  # noqa: E402


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class Base(unittest.TestCase):
    SLUG = "-tmp-projA"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bilinc-cap-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.claude = os.path.join(self.tmp, "claude")
        self.home = os.path.join(self.tmp, "bilinc-home")
        self.mem = os.path.join(self.claude, "projects", self.SLUG, "memory")
        os.makedirs(self.mem)
        env = mock.patch.dict(
            os.environ,
            {"CLAUDE_CONFIG_DIR": self.claude, "BILINC_MEMORY_HOME": self.home, "BILINC_MEMORY_SETTLE": "0"},
        )
        env.start()
        self.addCleanup(env.stop)
        # Never talk to a real Cloud: record maybe_push calls instead.
        self.pushes = []
        fake = types.ModuleType("bilinc_memory.cloud")
        fake.maybe_push = lambda jr, pid: self.pushes.append(pid)
        patcher = mock.patch.dict(sys.modules, {"bilinc_memory.cloud": fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    # helpers -------------------------------------------------------------
    def f(self, name):
        return os.path.join(self.mem, name)

    def journal(self):
        return journal.Journal(paths.store_dir())

    def project_id(self):
        j = self.journal()
        t = paths.classify(self.f("MEMORY.md")) or paths.classify(self.f("a.md"))
        proj = j.project_for(t)
        return proj["id"] if isinstance(proj, dict) else proj.id

    def entries(self, relpath=None):
        return self.journal().entries(self.project_id(), relpath)

    def hook(self, name, **fields):
        payload = {"hook_event_name": name, "session_id": "sess-1", "cwd": "/tmp/projA"}
        payload.update(fields)
        return capture.handle(name, payload)

    def session_start(self):
        return self.hook("SessionStart", source="startup")

    @staticmethod
    def ctx(out):
        return (out or {}).get("hookSpecificOutput", {}).get("additionalContext")

    @staticmethod
    def bump(path, text):
        """Rewrite a file so its (mtime_ns, size) pair is guaranteed to differ."""
        _write(path, text)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


class PostToolUseTests(Base):
    def test_write_to_memory_is_journaled_as_claude(self):
        _write(self.f("a.md"), "hello\n")
        out = self.hook(
            "PostToolUse",
            tool_name="Write",
            tool_use_id="toolu_1",
            transcript_path="/x/t.jsonl",
            tool_input={"file_path": self.f("a.md"), "content": "hello\n"},
        )
        self.assertIsNone(out)  # PostToolUse can not set watchPaths
        es = self.entries("a.md")
        self.assertEqual(len(es), 1)
        e = es[0]
        self.assertEqual((e["op"], e["source"]), ("write", "agent"))
        self.assertEqual(e["session_id"], "sess-1")
        self.assertEqual(e["tool_use_id"], "toolu_1")
        self.assertEqual(e["transcript_path"], "/x/t.jsonl")
        self.assertEqual(self.journal().get_object(e["sha"]), b"hello\n")

    def test_write_uses_payload_content_not_disk(self):
        _write(self.f("a.md"), "on disk later\n")
        self.hook("PostToolUse", tool_name="Write",
                  tool_input={"file_path": self.f("a.md"), "content": "from payload\n"})
        e = self.entries("a.md")[0]
        self.assertEqual(self.journal().get_object(e["sha"]), b"from payload\n")

    def test_edit_reads_file_from_disk(self):
        _write(self.f("a.md"), "edited text\n")
        self.hook("PostToolUse", tool_name="Edit",
                  tool_input={"file_path": self.f("a.md"), "old_string": "x", "new_string": "y"})
        e = self.entries("a.md")[0]
        self.assertEqual((e["source"], self.journal().get_object(e["sha"])), ("agent", b"edited text\n"))

    def test_notebook_path_key_is_accepted(self):
        _write(self.f("n.md"), "nb\n")
        self.hook("PostToolUse", tool_name="NotebookEdit", tool_input={"notebook_path": self.f("n.md")})
        self.assertEqual(len(self.entries("n.md")), 1)

    def test_write_outside_roots_is_ignored(self):
        other = os.path.join(self.tmp, "elsewhere", "x.md")
        _write(other, "nope\n")
        self.assertIsNone(self.hook("PostToolUse", tool_name="Write",
                                    tool_input={"file_path": other, "content": "nope\n"}))
        self.assertFalse(os.path.isdir(os.path.join(self.home, "projects")) and
                         any(os.listdir(os.path.join(self.home, "projects"))))

    def test_symlink_escaping_a_root_is_ignored(self):
        secret = os.path.join(self.tmp, "secret.md")
        _write(secret, "top secret\n")
        os.symlink(secret, self.f("link.md"))
        self.hook("PostToolUse", tool_name="Write",
                  tool_input={"file_path": self.f("link.md"), "content": "top secret\n"})
        self.assertFalse(os.path.isdir(os.path.join(self.home, "objects")) and
                         any(os.listdir(os.path.join(self.home, "objects"))))

    def test_global_claude_md_is_journaled(self):
        g = os.path.join(self.claude, "CLAUDE.md")
        _write(g, "global rules\n")
        self.hook("PostToolUse", tool_name="Write", tool_input={"file_path": g, "content": "global rules\n"})
        j = self.journal()
        es = j.entries("global")
        self.assertEqual([(e["relpath"], e["source"]) for e in es], [("CLAUDE.md", "agent")])

    def test_bad_payload_shapes_are_ignored(self):
        for ti in (None, "str", [], {}, {"file_path": 5}, {"file_path": "a\0b"}, {"file_path": "x" * 9000}):
            self.assertIsNone(self.hook("PostToolUse", tool_name="Write", tool_input=ti))
        self.assertIsNone(capture.handle("PostToolUse", "not a dict"))
        self.assertIsNone(capture.handle("Bogus", {}))

    def test_cloud_push_is_called_only_when_enabled_and_changed(self):
        _write(self.f("a.md"), "v1\n")
        pl = dict(tool_name="Write", tool_input={"file_path": self.f("a.md"), "content": "v1\n"})
        self.hook("PostToolUse", **pl)
        self.assertEqual(self.pushes, [])  # Cloud off by default
        pid = self.project_id()
        self.journal().update_manifest(pid, lambda m: m["cloud"].update(enabled=True))
        _write(self.f("a.md"), "v2\n")
        self.hook("PostToolUse", tool_name="Write", tool_input={"file_path": self.f("a.md"), "content": "v2\n"})
        self.assertEqual(self.pushes, [pid])
        self.hook("PostToolUse", tool_name="Write", tool_input={"file_path": self.f("a.md"), "content": "v2\n"})
        self.assertEqual(self.pushes, [pid])  # identical content: no new version, no push

    def test_cloud_import_failure_does_not_break_capture(self):
        pid = None
        _write(self.f("a.md"), "v1\n")
        self.hook("PostToolUse", tool_name="Write", tool_input={"file_path": self.f("a.md"), "content": "v1\n"})
        pid = self.project_id()
        self.journal().update_manifest(pid, lambda m: m["cloud"].update(enabled=True))
        with mock.patch.dict(sys.modules, {"bilinc_memory.cloud": None}):  # import raises ImportError
            _write(self.f("a.md"), "v2\n")
            self.hook("PostToolUse", tool_name="Write",
                      tool_input={"file_path": self.f("a.md"), "content": "v2\n"})
        self.assertEqual(len(self.entries("a.md")), 2)

    def test_unsafe_store_means_no_capture_and_no_exception(self):
        _write(self.f("a.md"), "v1\n")
        with mock.patch.object(paths, "ensure_store", side_effect=RuntimeError("unsafe")):
            out = self.hook("PostToolUse", tool_name="Write",
                            tool_input={"file_path": self.f("a.md"), "content": "v1\n"})
        self.assertIsNone(out)
        self.assertFalse(os.path.exists(os.path.join(self.home, "projects")))


class FileChangedTests(Base):
    def _fc(self, event, name):
        return self.hook("FileChanged", file_path=self.f(name), event=event)

    def test_add_change_unlink(self):
        _write(self.f("a.md"), "one\n")
        out = self._fc("add", "a.md")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "FileChanged")
        watch = out["hookSpecificOutput"]["watchPaths"]
        self.assertIn(self.f("a.md"), watch)
        self.assertIn(os.path.join(self.claude, "CLAUDE.md"), watch)
        self.assertTrue(all(os.path.isabs(w) for w in watch))

        self.bump(self.f("a.md"), "two\n")
        self.assertIsNone(self._fc("change", "a.md"))

        os.unlink(self.f("a.md"))
        self.assertIsNone(self._fc("unlink", "a.md"))

        es = self.entries("a.md")
        self.assertEqual([(e["op"], e["source"]) for e in es],
                         [("write", "external"), ("write", "external"), ("delete", "external")])
        self.assertIsNone(es[2]["sha"])

    def test_watchpaths_is_the_complete_list(self):
        _write(self.f("a.md"), "1\n")
        _write(self.f("b.md"), "2\n")
        out = self._fc("add", "b.md")
        watch = out["hookSpecificOutput"]["watchPaths"]
        self.assertIn(self.f("a.md"), watch)  # replaces the list, so a.md must stay in it
        self.assertIn(self.f("b.md"), watch)

    def test_unknown_event_and_outside_paths_ignored(self):
        _write(self.f("a.md"), "x\n")
        self.assertIsNone(self.hook("FileChanged", file_path=self.f("a.md"), event="rename"))
        self.assertIsNone(self.hook("FileChanged", file_path="/etc/hosts", event="change"))
        self.assertIsNone(self.hook("FileChanged", file_path=None, event="change"))

    def test_duplicate_of_a_claude_write_keeps_source_claude(self):
        _write(self.f("a.md"), "same\n")
        self.hook("PostToolUse", tool_name="Write", tool_input={"file_path": self.f("a.md"), "content": "same\n"})
        self._fc("change", "a.md")
        es = self.entries("a.md")
        self.assertEqual([(e["source"]) for e in es], ["agent"])


class SessionStartTests(Base):
    def setUp(self):
        super().setUp()
        _write(self.f("MEMORY.md"), "# index\n- [a](a.md)\n")
        _write(self.f("a.md"), "alpha\n")
        _write(self.f("c.md"), "gamma\n")
        _write(os.path.join(self.claude, "CLAUDE.md"), "global\n")

    def test_first_run_is_a_silent_baseline(self):
        # Even a MEMORY.md over the limit must not produce a notice on the first run.
        _write(self.f("MEMORY.md"), "".join("line %d\n" % i for i in range(190)))
        out = self.session_start()
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        self.assertNotIn("additionalContext", hso)
        self.assertIn(self.f("a.md"), hso["watchPaths"])
        ops = {(e["relpath"], e["op"], e["source"]) for e in self.entries()}
        self.assertEqual(ops, {("MEMORY.md", "baseline", "baseline"), ("a.md", "baseline", "baseline"),
                               ("c.md", "baseline", "baseline")})
        self.assertEqual([e["op"] for e in self.journal().entries("global")], ["baseline"])

    def test_nothing_new_means_no_context(self):
        self.session_start()
        out = self.session_start()
        self.assertNotIn("additionalContext", out["hookSpecificOutput"])
        self.assertEqual(len(self.entries()), 3)  # no duplicate baselines

    def test_reconcile_catches_edit_new_file_and_delete_made_while_closed(self):
        self.session_start()
        self.bump(self.f("a.md"), "alpha edited while closed\n")
        _write(self.f("b.md"), "brand new\n")
        os.unlink(self.f("c.md"))
        out = self.session_start()
        text = self.ctx(out)
        self.assertIsNotNone(text)
        self.assertLessEqual(len(text.splitlines()), 2)
        self.assertIn("3 memory file(s)", text)
        for n in ("'a.md'", "'b.md'", "'c.md'"):
            self.assertIn(n, text)
        by = {}
        for e in self.entries():
            by.setdefault(e["relpath"], []).append((e["op"], e["source"]))
        self.assertEqual(by["a.md"], [("baseline", "baseline"), ("write", "external")])
        self.assertEqual(by["b.md"], [("write", "external")])
        self.assertEqual(by["c.md"], [("baseline", "baseline"), ("delete", "external")])
        self.assertIn(self.f("b.md"), out["hookSpecificOutput"]["watchPaths"])
        self.assertNotIn(self.f("c.md"), out["hookSpecificOutput"]["watchPaths"])
        # A third start with no further changes is quiet again.
        self.assertIsNone(self.ctx(self.session_start()))

    def test_edit_that_restores_identical_content_is_not_news(self):
        self.session_start()
        st = os.stat(self.f("a.md"))
        os.utime(self.f("a.md"), ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))  # touch only
        out = self.session_start()
        self.assertIsNone(self.ctx(out))
        self.assertEqual(len(self.entries("a.md")), 1)

    def test_stat_first_unchanged_files_are_not_rehashed(self):
        self.session_start()
        self.session_start()
        sha_calls = []
        real_sha = capture._sha
        reads = []
        real_read = paths.read_regular

        def counting_sha(data):
            sha_calls.append(1)
            return real_sha(data)

        def counting_read(path, *a, **k):
            reads.append(str(path))
            return real_read(path, *a, **k)

        with mock.patch.object(capture, "_sha", counting_sha), \
                mock.patch.object(paths, "read_regular", counting_read):
            self.session_start()
            self.assertEqual(sha_calls, [])
            self.assertEqual([r for r in reads if not r.endswith(("MEMORY.md", "config.json"))], [])
            # Change one file: exactly that one is hashed.
            self.bump(self.f("a.md"), "alpha v2\n")
            self.session_start()
        self.assertEqual(len(sha_calls), 1)

    def test_memory_md_near_limit_notice_from_second_run_on(self):
        _write(self.f("MEMORY.md"), "".join("l%d\n" % i for i in range(170)))  # 85% of 200
        self.session_start()  # baseline run: silent
        text = self.ctx(self.session_start())
        self.assertIsNotNone(text)
        self.assertIn("MEMORY.md is at 170 of 200 lines", text)
        self.assertLessEqual(len(text.splitlines()), 2)

    def test_memory_md_below_threshold_is_quiet(self):
        _write(self.f("MEMORY.md"), "".join("l%d\n" % i for i in range(100)))
        self.session_start()
        self.assertIsNone(self.ctx(self.session_start()))

    def test_memory_md_size_threshold_and_config_override(self):
        _write(self.f("MEMORY.md"), "x" * 22_000 + "\n")  # ~21.5 KB: 86% of 25 KB, 1 line
        self.session_start()
        self.assertIn("KB", self.ctx(self.session_start()))
        os.makedirs(self.home, exist_ok=True)
        _write(os.path.join(self.home, "config.json"), json.dumps({"memory_md_max_kb": 100}))
        self.assertIsNone(self.ctx(self.session_start()))

    def test_context_is_at_most_two_lines_and_names_are_cleaned(self):
        _write(self.f("MEMORY.md"), "".join("l%d\n" % i for i in range(190)))
        self.session_start()
        self.bump(self.f("a.md"), "changed\n")
        _write(self.f("evil‮​.md"), "x\n")
        text = self.ctx(self.session_start())
        self.assertLessEqual(len(text.splitlines()), 2)
        self.assertNotIn("‮", text)
        self.assertNotIn("​", text)

    def test_pending_cloud_upload_is_news(self):
        self.session_start()
        pid = self.project_id()
        self.journal().update_manifest(pid, lambda m: m["cloud"].update(enabled=True, pushed_seq=1))
        text = self.ctx(self.session_start())
        self.assertIn("waiting to upload", text)

    def test_other_projects_changes_are_not_news_for_this_session(self):
        other = os.path.join(self.claude, "projects", "-tmp-other", "memory")
        _write(os.path.join(other, "o.md"), "o\n")
        self.session_start()
        self.bump(os.path.join(other, "o.md"), "o2\n")
        self.assertIsNone(self.ctx(self.session_start()))
        # ...but it was journaled.
        j = self.journal()
        t = paths.classify(os.path.join(other, "o.md"))
        proj = j.project_for(t)
        pid = proj["id"] if isinstance(proj, dict) else proj.id
        self.assertEqual(len(j.entries(pid, "o.md")), 2)

    def test_incomplete_scan_never_becomes_mass_delete(self):
        self.session_start()
        real_scan = capture._scan_unit

        def broken(unit):
            real_scan(unit)
            if unit.scope == "project":
                unit.files.clear()
                unit.complete = False

        with mock.patch.object(capture, "_scan_unit", broken):
            self.session_start()
        self.assertEqual({e["op"] for e in self.entries()}, {"baseline"})

    def test_symlinks_and_fifos_in_memory_dir_are_skipped(self):
        os.symlink(os.path.join(self.tmp, "nowhere"), self.f("dangling.md"))
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.f("pipe.md"))
        self.session_start()
        self.assertEqual(sorted({e["relpath"] for e in self.entries()}), ["MEMORY.md", "a.md", "c.md"])


class StopTests(Base):
    def test_stop_reconciles_silently(self):
        _write(self.f("a.md"), "a\n")
        self.session_start()
        _write(self.f("new.md"), "made by Bash\n")
        self.bump(self.f("a.md"), "a2\n")
        self.assertIsNone(self.hook("Stop", stop_hook_active=False))
        es = self.entries()
        self.assertEqual(sorted((e["relpath"], e["op"], e["source"]) for e in es),
                         [("a.md", "baseline", "baseline"), ("a.md", "write", "external"),
                          ("new.md", "write", "external")])

    def test_stop_retries_cloud_backlog(self):
        _write(self.f("a.md"), "a\n")
        self.session_start()
        pid = self.project_id()
        self.journal().update_manifest(pid, lambda m: m["cloud"].update(enabled=True))
        self.hook("Stop")
        self.assertIn(pid, self.pushes)


class HookScriptTests(Base):
    """hook.py and run.sh as real subprocesses (python -I), in temp dirs."""

    def env(self):
        e = dict(os.environ)
        e.update({"CLAUDE_CONFIG_DIR": self.claude, "BILINC_MEMORY_HOME": self.home, "BILINC_MEMORY_SETTLE": "0",
                  "HOME": self.tmp})
        e.pop("PYTHONPATH", None)
        return e

    def run_hook(self, event, stdin, via_sh=False):
        cmd = ["sh", RUN_SH, event] if via_sh else [sys.executable, "-I", HOOK_PY, event]
        env = self.env()
        env["CLAUDE_PLUGIN_ROOT"] = ROOT
        return subprocess.run(cmd, input=stdin, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)

    def test_malformed_stdin_exits_zero_silently(self):
        big = b"{" + b"a" * (5 * 1024 * 1024)
        for event in ("PostToolUse", "FileChanged", "SessionStart", "Stop"):
            for junk in (b"", b"not json", b"[1,2]", b'"str"', b"null", b"{\xff\xfe}", big):
                for via_sh in (False, True):
                    r = self.run_hook(event, junk, via_sh)
                    self.assertEqual(r.returncode, 0, (event, junk[:20], r.stderr))
                    self.assertEqual(r.stdout, b"", (event, junk[:20]))
                    self.assertLessEqual(len(r.stderr.splitlines()), 1)

    def test_unknown_event_and_mismatched_event_name_are_ignored(self):
        good = json.dumps({"hook_event_name": "Stop"}).encode()
        self.assertEqual(self.run_hook("Nope", good).stdout, b"")
        r = self.run_hook("SessionStart", good)  # payload says Stop
        self.assertEqual((r.returncode, r.stdout), (0, b""))

    def test_session_start_end_to_end_prints_one_line_of_json(self):
        _write(self.f("a.md"), "alpha\n")
        payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "s", "cwd": "/tmp/projA",
                              "source": "startup"}).encode()
        for via_sh in (False, True):
            r = self.run_hook("SessionStart", payload, via_sh)
            self.assertEqual(r.returncode, 0, r.stderr)
            lines = r.stdout.decode().splitlines()
            self.assertEqual(len(lines), 1)
            out = json.loads(lines[0])
            self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn(self.f("a.md"), out["hookSpecificOutput"]["watchPaths"])

    def test_post_tool_use_end_to_end_journals(self):
        _write(self.f("a.md"), "hi\n")
        payload = json.dumps({"hook_event_name": "PostToolUse", "session_id": "s9", "tool_name": "Write",
                              "tool_use_id": "t9",
                              "tool_input": {"file_path": self.f("a.md"), "content": "hi\n"}}).encode()
        r = self.run_hook("PostToolUse", payload)
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        e = self.entries("a.md")[0]
        self.assertEqual((e["source"], e["session_id"]), ("agent", "s9"))

    def test_unsafe_store_in_subprocess_exits_zero(self):
        os.makedirs(self.home)
        os.chmod(self.home, 0o755)  # real paths.ensure_store() rejects non-0700; the stub accepts
        payload = json.dumps({"hook_event_name": "Stop"}).encode()
        r = self.run_hook("Stop", payload)
        self.assertEqual((r.returncode, r.stdout), (0, b""))


if __name__ == "__main__":
    unittest.main()


class RetentionTests(Base):
    """README promises the newest N versions per file are kept: SessionStart applies it."""

    def _versions(self, n):
        for i in range(n):
            self.bump(self.f("MEMORY.md"), "v%d\n" % i)
            self.hook("Stop")

    def test_session_start_prunes_to_the_configured_cap(self):
        _write(self.f("MEMORY.md"), "v-start\n")
        self.session_start()
        self._versions(6)
        self.assertEqual(len(self.entries("MEMORY.md")), 7)
        with open(os.path.join(self.home, "config.json"), "w") as fh:
            json.dump({"max_versions_per_file": 3}, fh)
        with mock.patch.object(capture.time, "time", return_value=capture.time.time() + capture.PRUNE_EVERY_SECONDS + 60):
            self.session_start()
        es = self.entries("MEMORY.md")
        self.assertEqual(len(es), 3)
        self.assertEqual(self.journal().get_object(es[-1]["sha"]), b"v5\n")  # newest kept

    def test_prune_runs_at_most_once_a_day(self):
        _write(self.f("MEMORY.md"), "v-start\n")
        self.session_start()  # first run prunes and writes the marker
        calls = []
        real = journal.Journal.prune
        with mock.patch.object(journal.Journal, "prune", lambda s, *a, **k: calls.append(1) or real(s, *a, **k)):
            self.session_start()
            self.assertEqual(calls, [])
            marker = os.path.join(self.home, "last-prune")
            old = os.stat(marker).st_mtime - capture.PRUNE_EVERY_SECONDS - 5
            os.utime(marker, (old, old))
            self.session_start()
        self.assertEqual(calls, [1])
