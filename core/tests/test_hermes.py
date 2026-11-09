"""Hermes adapter: agent_hermes, the shared core under BILINC_MEMORY_AGENT=hermes,
and the plugin in hermes/ (hooks + slash commands). Temp dirs only; Hermes
itself is never run."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

CORE = Path(__file__).resolve().parent.parent
REPO = CORE.parent
sys.path.insert(0, str(CORE))

from bilinc_memory import agent_hermes, capture, commands, journal, paths  # noqa: E402

SEP = "\n§\n"
MEM = SEP.join(["user prefers Turkish", "works on Bilinc", "deploys need owner OK"])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bilinc-hermes-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.home = self.tmp / "hermes"
        self.store = self.tmp / "store"
        self.mem = self.home / "memories"
        self.mem.mkdir(parents=True)
        env = {
            "BILINC_MEMORY_AGENT": "hermes",
            "HERMES_HOME": str(self.home),
            "BILINC_MEMORY_HOME": str(self.store),
            "BILINC_MEMORY_SETTLE": "0",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("BILINC_MEMORY_HERMES_DIR", None)

    def write(self, name, text, home=None):
        p = (home or self.home) / "memories" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
        return p

    def hook(self, event, payload):
        payload = dict(payload, hook_event_name=event)
        return capture.handle(event, payload)

    def run_cmd(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = commands.main(list(argv))
        return code, buf.getvalue()

    def entries(self, slug="default"):
        j = journal.Journal()
        pid = commands._slugs_for(j).get(slug)
        if pid is None:
            return []
        return [e for e in j.entries(pid)]


class AdapterTests(Base):
    def test_agent_selected_from_env(self):
        self.assertIs(paths.agent(), agent_hermes)
        self.assertEqual(paths.store_dir(), self.store)

    def test_default_store_is_per_agent(self):
        os.environ.pop("BILINC_MEMORY_HOME")
        self.assertEqual(paths.store_dir(), Path.home() / ".bilinc" / "memory-history" / "hermes")

    def test_tracks_the_active_profile_and_no_other(self):
        (self.home / "profiles" / "jarvis" / "memories").mkdir(parents=True)
        self.assertEqual(agent_hermes.memory_roots(), [self.mem])
        self.assertEqual(agent_hermes.project_slug(self.mem), "default")

    def test_profile_home_tracks_that_profile_only(self):
        prof = self.home / "profiles" / "jarvis"
        (prof / "memories").mkdir(parents=True)
        os.environ["HERMES_HOME"] = str(prof)
        self.assertEqual(agent_hermes.memory_roots(), [prof / "memories"])
        self.assertEqual(agent_hermes.current_project(), ("jarvis", prof / "memories"))
        self.assertIsNone(paths.classify(str(self.mem / "MEMORY.md")))

    def test_no_memories_folder_yet(self):
        shutil.rmtree(str(self.mem))
        self.assertEqual(agent_hermes.memory_roots(), [])

    def test_only_memory_and_user_are_tracked(self):
        self.write("MEMORY.md", MEM)
        for name in ("MEMORY.md.lock", "MEMORY.md.bak.1730000000", ".mem_tmp", "notes.md"):
            (self.mem / name).write_text("x")
        self.assertIsNotNone(paths.classify(str(self.mem / "MEMORY.md")))
        self.assertIsNotNone(paths.classify(str(self.mem / "USER.md")))  # missing file still classifies
        for name in ("MEMORY.md.lock", "MEMORY.md.bak.1730000000", ".mem_tmp", "notes.md"):
            self.assertIsNone(paths.classify(str(self.mem / name)), name)

    def test_never_walks_the_rest_of_the_home(self):
        (self.home / ".env").write_text("SECRET=1")
        (self.home / "auth.json").write_text("{}")
        self.assertIsNone(paths.classify(str(self.home / ".env")))
        self.assertIsNone(paths.global_claude_md())


class CaptureTests(Base):
    def test_first_session_is_a_silent_baseline(self):
        self.write("MEMORY.md", MEM)
        self.write("USER.md", "name: Atakan")
        self.hook("SessionStart", {"session_id": "s1", "cwd": str(self.tmp)})
        es = self.entries()
        self.assertEqual(sorted((e["relpath"], e["op"]) for e in es),
                         [("MEMORY.md", "baseline"), ("USER.md", "baseline")])

    def test_memory_tool_write_is_recorded_as_agent(self):
        p = self.write("MEMORY.md", MEM)
        self.hook("SessionStart", {"session_id": "s1"})
        p.write_bytes((MEM + SEP + "likes short replies").encode("utf-8"))
        self.hook("PostToolUse", {"tool_name": "memory", "tool_input": {"file_path": str(p)},
                                  "session_id": "s1", "tool_use_id": "t1"})
        last = self.entries()[-1]
        self.assertEqual((last["relpath"], last["op"], last["source"], last["session_id"]),
                         ("MEMORY.md", "write", "agent", "s1"))

    def test_hand_edit_and_delete_found_by_reconcile(self):
        self.write("MEMORY.md", MEM)
        u = self.write("USER.md", "name: Atakan")
        self.hook("SessionStart", {})
        self.write("MEMORY.md", MEM + SEP + "edited by hand")
        u.unlink()
        self.hook("Stop", {})
        tail = [(e["relpath"], e["op"], e["source"]) for e in self.entries()[-2:]]
        self.assertEqual(sorted(tail), [("MEMORY.md", "write", "external"), ("USER.md", "delete", "external")])

    def test_other_profiles_are_not_recorded(self):
        self.write("MEMORY.md", MEM)
        self.write("MEMORY.md", "jarvis note", home=self.home / "profiles" / "jarvis")
        self.hook("SessionStart", {})
        self.assertEqual(len(self.entries("default")), 1)
        self.assertEqual(self.entries("jarvis"), [])

    def test_lock_file_changes_are_ignored(self):
        self.write("MEMORY.md", MEM)
        self.hook("SessionStart", {})
        (self.mem / "MEMORY.md.lock").write_text("")
        self.hook("Stop", {})
        self.assertEqual([e["relpath"] for e in self.entries()], ["MEMORY.md"])


class CommandTests(Base):
    def seed(self):
        p = self.write("MEMORY.md", MEM)
        self.hook("SessionStart", {})
        p.write_bytes((MEM + SEP + "wrong fact").encode("utf-8"))
        self.hook("PostToolUse", {"tool_name": "memory", "tool_input": {"file_path": str(p)}})
        return p

    def test_history_names_hermes_and_dash_commands(self):
        self.seed()
        code, text = self.run_cmd("history", "MEMORY.md")
        self.assertEqual(code, 0, text)
        self.assertIn("by Hermes", text)
        self.assertIn("+wrong fact", text)
        self.assertIn("/bilinc-undo", text)
        self.assertNotIn("/bilinc:", text)

    def test_undo_preview_then_apply_keeps_the_separator_bytes(self):
        p = self.seed()
        code, text = self.run_cmd("undo", "MEMORY.md", "1")
        self.assertEqual(code, 0, text)
        self.assertIn("Preview only", text)
        self.assertIn("wrong fact", p.read_text(encoding="utf-8"))
        code, text = self.run_cmd("undo", "MEMORY.md", "1", "--yes")
        self.assertEqual(code, 0, text)
        self.assertEqual(p.read_bytes(), MEM.encode("utf-8"))
        self.assertIn("next session (/new)", text)
        self.assertEqual(self.entries()[-1]["source"], "undo")

    def test_undo_keeps_file_mode_0600(self):
        p = self.seed()
        os.chmod(p, 0o600)
        self.run_cmd("undo", "MEMORY.md", "1", "--yes")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_undo_waits_for_the_hermes_lock(self):
        p = self.seed()
        try:
            import fcntl
        except ImportError:
            self.skipTest("no flock")
        fd = os.open(str(p) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        released = []

        def release():
            time.sleep(0.4)
            released.append(time.monotonic())
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        t = threading.Thread(target=release)
        t.start()
        code, text = self.run_cmd("undo", "MEMORY.md", "1", "--yes")
        done = time.monotonic()
        t.join()
        self.assertEqual(code, 0, text)
        self.assertTrue(released and done >= released[0])
        self.assertEqual(p.read_bytes(), MEM.encode("utf-8"))

    def test_undo_gives_up_when_the_lock_stays_held(self):
        p = self.seed()
        try:
            import fcntl
        except ImportError:
            self.skipTest("no flock")
        fd = os.open(str(p) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        with mock.patch.object(agent_hermes, "LOCK_WAIT_SECONDS", 0.2):
            code, text = self.run_cmd("undo", "MEMORY.md", "1", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("wrong fact", p.read_text(encoding="utf-8"))

    def test_restore_deleted_user_md(self):
        self.write("MEMORY.md", MEM)
        u = self.write("USER.md", "name: Atakan")
        self.hook("SessionStart", {})
        u.unlink()
        self.hook("Stop", {})
        code, text = self.run_cmd("restore", "USER.md", "--yes")
        self.assertEqual(code, 0, text)
        self.assertEqual(u.read_text(), "name: Atakan")
        self.assertIn("next session (/new)", text)

    def test_status_has_no_global_or_claude_load_limit(self):
        self.seed()
        code, text = self.run_cmd("status")
        self.assertEqual(code, 0, text)
        self.assertIn("default", text)
        self.assertNotIn("CLAUDE.md", text)
        self.assertNotIn("load limit", text)
        self.assertIn("Cloud: not available for Hermes", text)

    def test_global_flag_is_refused(self):
        code, text = self.run_cmd("history", "--global")
        self.assertEqual(code, 1)
        self.assertIn("no user-level memory file", text)

    def test_untracked_file_is_refused(self):
        code, text = self.run_cmd("history", "notes.md")
        self.assertEqual(code, 1)
        self.assertIn("not a Hermes memory file", text)

    def test_cloud_commands_are_refused(self):
        for argv in (["cloud", "on"], ["push"], ["pull"]):
            code, text = self.run_cmd(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("not available for Hermes", text)

    def test_profile_commands_use_the_active_profile(self):
        prof = self.home / "profiles" / "jarvis"
        p = self.write("MEMORY.md", "a", home=prof)
        os.environ["HERMES_HOME"] = str(prof)
        self.hook("SessionStart", {})
        p.write_text("a" + SEP + "b")
        self.hook("Stop", {})
        code, text = self.run_cmd("history", "MEMORY.md")
        self.assertEqual(code, 0, text)
        self.assertIn("jarvis", text)
        self.assertIn("+b", text)


def _load_plugin():
    spec = importlib.util.spec_from_file_location("bilinc_hermes_plugin", str(REPO / "hermes" / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self):
        self.hooks, self.commands = {}, {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_command(self, name, handler, description="", **_):
        self.commands[name] = handler


class PluginTests(Base):
    def setUp(self):
        super().setUp()
        self.plugin = _load_plugin()
        self.ctx = FakeCtx()
        self.plugin.register(self.ctx)

    def wait_for(self, n, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(self.entries()) >= n:
                return
            time.sleep(0.05)
        self.fail("capture did not run (entries=%d)" % len(self.entries()))

    def test_registers_hooks_and_dash_commands(self):
        self.assertEqual(set(self.ctx.hooks), {"post_tool_call", "on_session_start", "on_session_finalize"})
        self.assertEqual(set(self.ctx.commands),
                         {"bilinc-history", "bilinc-undo", "bilinc-restore", "bilinc-status", "bilinc-wipe-local"})

    def test_manifest_name_differs_from_the_bilinc_provider_plugin(self):
        text = (REPO / "hermes" / "plugin.yaml").read_text()
        self.assertIn("name: bilinc-memory-history", text)

    def test_end_to_end_through_the_plugin(self):
        p = self.write("MEMORY.md", MEM)
        self.ctx.hooks["on_session_start"](session_id="s1")
        self.wait_for(1)
        p.write_bytes((MEM + SEP + "wrong fact").encode("utf-8"))
        self.ctx.hooks["post_tool_call"](tool_name="memory", args={"action": "add", "target": "memory",
                                         "content": "wrong fact"}, session_id="s1", tool_call_id="c1")
        self.wait_for(2)
        self.assertEqual(self.entries()[-1]["source"], "agent")
        text = self.ctx.commands["bilinc-history"]("MEMORY.md")
        self.assertIn("by Hermes", text)
        text = self.ctx.commands["bilinc-undo"]("MEMORY.md 1")
        self.assertIn("Preview only", text)
        text = self.ctx.commands["bilinc-undo"]("MEMORY.md 1 --yes")
        self.assertIn("next session (/new)", text)
        self.assertEqual(p.read_bytes(), MEM.encode("utf-8"))

    def test_hand_edit_is_recorded_before_a_command_runs(self):
        p = self.write("MEMORY.md", MEM)
        self.ctx.hooks["on_session_start"](session_id="s1")
        self.wait_for(1)
        p.write_bytes((MEM + SEP + "typed by hand").encode("utf-8"))
        text = self.ctx.commands["bilinc-history"]("MEMORY.md")
        self.assertIn("+typed by hand", text)
        self.assertIn("outside Hermes", text)

    def test_unrelated_tool_calls_do_nothing(self):
        self.write("MEMORY.md", MEM)
        self.ctx.hooks["post_tool_call"](tool_name="terminal", args={"command": "ls /tmp"})
        self.ctx.hooks["post_tool_call"](tool_name="web_search", args={"q": "x"})
        time.sleep(0.3)
        self.assertEqual(self.entries(), [])

    def test_bad_arguments_never_raise(self):
        self.assertIn("Could not read", self.ctx.commands["bilinc-history"]('"unclosed'))
        self.ctx.hooks["post_tool_call"](tool_name="memory", args=None)
        self.ctx.hooks["post_tool_call"]()


if __name__ == "__main__":
    unittest.main()
