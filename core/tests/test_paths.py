"""Tests for bilinc_memory.paths (stdlib unittest, temp dirs only)."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))

from bilinc_memory import paths  # noqa: E402

POSIX = os.name != "nt"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.claude = self.tmp / "claude"
        self.home = self.tmp / "bilinc"
        self.claude.mkdir()
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude),
                                               "BILINC_MEMORY_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_root(self, slug="-Users-x-proj"):
        root = self.claude / "projects" / slug / "memory"
        root.mkdir(parents=True)
        return root


class TestLocations(Base):
    def test_env_overrides(self):
        self.assertEqual(paths.claude_dir(), self.claude)
        self.assertEqual(paths.store_dir(), self.home)
        self.assertEqual(paths.global_claude_md(), self.claude / "CLAUDE.md")

    def test_defaults_use_home(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "BILINC_MEMORY_HOME")}
        env["HOME"] = str(self.tmp)
        env["USERPROFILE"] = str(self.tmp)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(paths.claude_dir(), self.tmp / ".claude")
            self.assertEqual(paths.store_dir(), self.tmp / ".bilinc" / "memory-history" / "claude-code")

    def test_empty_env_falls_back(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):
            self.assertEqual(paths.claude_dir(), Path.home() / ".claude")

    def test_memory_roots(self):
        self.assertEqual(paths.memory_roots(), [])
        a = self.make_root("-a")
        b = self.make_root("-b")
        (self.claude / "projects" / "-c").mkdir()  # no memory dir
        (self.claude / "projects" / "-d").write_text("file, not dir")
        self.assertEqual(paths.memory_roots(), [a, b])


@unittest.skipUnless(POSIX, "mode and symlink checks are POSIX")
class TestEnsureStore(Base):
    def test_creates_0700(self):
        p = paths.ensure_store()
        self.assertEqual(p, self.home)
        self.assertEqual(stat.S_IMODE(os.lstat(p).st_mode), 0o700)
        self.assertEqual(paths.ensure_store(), p)  # idempotent

    def test_creates_parents_private(self):
        deep = self.tmp / "a" / "b" / "store"
        paths.ensure_store(deep)
        for d in (deep, deep.parent, deep.parent.parent):
            self.assertEqual(stat.S_IMODE(os.lstat(d).st_mode), 0o700, d)

    def test_creates_under_loose_umask(self):
        old = os.umask(0)
        try:
            paths.ensure_store(self.tmp / "x" / "store")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.lstat(self.tmp / "x" / "store").st_mode), 0o700)

    def test_refuses_loose_mode(self):
        self.home.mkdir(mode=0o755)
        os.chmod(self.home, 0o755)
        with self.assertRaises(paths.UnsafeStore):
            paths.ensure_store()
        # and does not silently repair it
        self.assertEqual(stat.S_IMODE(os.lstat(self.home).st_mode), 0o755)

    def test_refuses_group_readable(self):
        self.home.mkdir()
        os.chmod(self.home, 0o750)
        with self.assertRaises(paths.UnsafeStore):
            paths.ensure_store()

    def test_refuses_symlinked_store(self):
        real = self.tmp / "real"
        real.mkdir(mode=0o700)
        os.symlink(real, self.home)
        with self.assertRaises(paths.UnsafeStore):
            paths.ensure_store()

    def test_refuses_dangling_symlink(self):
        os.symlink(self.tmp / "nowhere", self.home)
        with self.assertRaises(paths.UnsafeStore):
            paths.ensure_store()

    def test_refuses_file(self):
        self.home.write_text("x")
        with self.assertRaises(paths.UnsafeStore):
            paths.ensure_store()

    def test_refuses_foreign_owner(self):
        self.home.mkdir(mode=0o700)
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(paths.UnsafeStore):
                paths.ensure_store()


class TestValidateRelpath(unittest.TestCase):
    def test_accepts_and_normalises(self):
        self.assertEqual(paths.validate_relpath("MEMORY.md"), "MEMORY.md")
        self.assertEqual(paths.validate_relpath("topics/a.md"), "topics/a.md")
        self.assertEqual(paths.validate_relpath("./a.md"), "a.md")
        self.assertEqual(paths.validate_relpath("a/./b.md"), "a/b.md")
        self.assertEqual(paths.validate_relpath("..hidden/.x..y"), "..hidden/.x..y")
        self.assertEqual(paths.validate_relpath("ünï/ç.md"), "ünï/ç.md")
        self.assertEqual(paths.validate_relpath(Path("a") / "b.md"), "a/b.md")

    def test_rejects(self):
        bad = ["", ".", "./", "/", "/etc/passwd", "../x", "a/../b", "a/..", "..", "a//b", "a/", "a/b/",
               "a\0b", "a\\b", "..\\x", "C:/x", "c:x", "a\nb", "a\tb", "a\x7fb", "x" * 1025]
        for rel in bad:
            with self.subTest(rel=rel):
                with self.assertRaises(ValueError):
                    paths.validate_relpath(rel)

    def test_rejects_non_strings(self):
        for rel in (None, 5, b"a.md", ["a"]):
            with self.subTest(rel=rel):
                with self.assertRaises(ValueError):
                    paths.validate_relpath(rel)

    def test_max_length_ok(self):
        self.assertEqual(paths.validate_relpath("x" * 1024), "x" * 1024)


@unittest.skipUnless(POSIX, "symlinks are POSIX")
class TestResolveTarget(Base):
    def test_ok_returns_unresolved_join(self):
        root = self.make_root()
        self.assertEqual(paths.resolve_target(root, "a/b.md"), root / "a" / "b.md")
        self.assertEqual(paths.resolve_target(str(root), "./c.md"), root / "c.md")

    def test_traversal_rejected(self):
        root = self.make_root()
        for rel in ("../x", "a/../../x", "/etc/passwd", "a\\..\\x"):
            with self.subTest(rel=rel):
                with self.assertRaises(ValueError):
                    paths.resolve_target(root, rel)

    def test_symlinked_dir_escape_rejected(self):
        root = self.make_root()
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, root / "link")
        with self.assertRaises(ValueError):
            paths.resolve_target(root, "link/x.md")

    def test_symlinked_leaf_escape_rejected(self):
        root = self.make_root()
        outside = self.tmp / "secret.txt"
        outside.write_text("s")
        os.symlink(outside, root / "leak.md")
        with self.assertRaises(ValueError):
            paths.resolve_target(root, "leak.md")

    def test_symlink_inside_root_allowed(self):
        root = self.make_root()
        (root / "real").mkdir()
        os.symlink(root / "real", root / "alias")
        self.assertEqual(paths.resolve_target(root, "alias/x.md"), root / "alias" / "x.md")

    def test_root_itself_rejected(self):
        root = self.make_root()
        with self.assertRaises(ValueError):
            paths.resolve_target(root, ".")


class TestClassify(Base):
    def test_project_file(self):
        root = self.make_root("-Users-x-proj")
        f = root / "topics" / "a.md"
        f.parent.mkdir()
        f.write_text("hi")
        t = paths.classify(str(f))
        self.assertEqual(t, paths.Target("project", "-Users-x-proj", root, "topics/a.md"))
        self.assertEqual(paths.classify(f), t)  # PathLike accepted

    def test_memory_index(self):
        root = self.make_root()
        (root / "MEMORY.md").write_text("x")
        self.assertEqual(paths.classify(str(root / "MEMORY.md")).relpath, "MEMORY.md")

    def test_global(self):
        g = paths.global_claude_md()
        g.write_text("g")
        t = paths.classify(str(g))
        self.assertEqual((t.scope, t.slug, t.relpath, t.root), ("global", None, "CLAUDE.md", self.claude))

    def test_missing_files_classify(self):
        root = self.make_root()
        self.assertIsNotNone(paths.classify(str(root / "gone.md")))
        self.assertIsNotNone(paths.classify(str(paths.global_claude_md())))

    def test_outside_is_none(self):
        self.make_root()
        other = self.tmp / "other.md"
        other.write_text("x")
        self.assertIsNone(paths.classify(str(other)))
        self.assertIsNone(paths.classify(str(self.claude / "settings.json")))
        self.assertIsNone(paths.classify(str(self.claude / "projects" / "-Users-x-proj" / "notes.md")))

    def test_bad_inputs(self):
        for bad in (None, 5, "", "a\0b", b"", []):
            with self.subTest(bad=bad):
                self.assertIsNone(paths.classify(bad))

    def test_root_itself_and_directories_are_none(self):
        root = self.make_root()
        (root / "sub").mkdir()
        self.assertIsNone(paths.classify(str(root)))
        self.assertIsNone(paths.classify(str(root / "sub")))

    def test_dotdot_escape_is_none(self):
        root = self.make_root()
        (self.claude / "projects" / "-Users-x-proj" / "notes.md").write_text("x")
        self.assertIsNone(paths.classify(str(root / ".." / "notes.md")))
        self.assertIsNone(paths.classify(str(root / ".." / ".." / ".." / "settings.json")))

    def test_dotdot_that_stays_inside_normalises(self):
        root = self.make_root()
        (root / "a").mkdir()
        (root / "x.md").write_text("x")
        t = paths.classify(str(root / "a" / ".." / "x.md"))
        self.assertEqual(t.relpath, "x.md")

    def test_sibling_prefix_is_none(self):
        root = self.make_root()
        evil = Path(str(root) + "-evil")
        evil.mkdir()
        (evil / "a.md").write_text("x")
        self.assertIsNone(paths.classify(str(evil / "a.md")))

    @unittest.skipUnless(POSIX, "symlinks are POSIX")
    def test_symlink_escaping_root_is_none(self):
        root = self.make_root()
        secret = self.tmp / "secret.txt"
        secret.write_text("s")
        os.symlink(secret, root / "leak.md")
        self.assertIsNone(paths.classify(str(root / "leak.md")))

    @unittest.skipUnless(POSIX, "symlinks are POSIX")
    def test_symlinked_dir_escaping_root_is_none(self):
        root = self.make_root()
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "a.md").write_text("x")
        os.symlink(outside, root / "d")
        self.assertIsNone(paths.classify(str(root / "d" / "a.md")))

    @unittest.skipUnless(POSIX, "symlinks are POSIX")
    def test_dangling_symlink_to_outside_is_none(self):
        root = self.make_root()
        os.symlink(self.tmp / "nowhere", root / "dangling.md")
        self.assertIsNone(paths.classify(str(root / "dangling.md")))

    @unittest.skipUnless(POSIX, "symlinks are POSIX")
    def test_symlink_inside_root_resolves_to_real_target(self):
        root = self.make_root()
        (root / "real.md").write_text("x")
        os.symlink(root / "real.md", root / "alias.md")
        self.assertEqual(paths.classify(str(root / "alias.md")).relpath, "real.md")

    @unittest.skipUnless(POSIX, "symlinks are POSIX")
    def test_global_symlink_to_elsewhere_is_none(self):
        other = self.tmp / "dotfiles-CLAUDE.md"
        other.write_text("x")
        os.symlink(other, paths.global_claude_md())
        self.assertIsNone(paths.classify(str(paths.global_claude_md())))

    @unittest.skipUnless(POSIX, "symlinks are POSIX")
    def test_symlink_into_global_is_global(self):
        paths.global_claude_md().write_text("g")
        os.symlink(paths.global_claude_md(), self.tmp / "alias.md")
        self.assertEqual(paths.classify(str(self.tmp / "alias.md")).scope, "global")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_fifo_is_none(self):
        root = self.make_root()
        os.mkfifo(root / "pipe.md")
        self.assertIsNone(paths.classify(str(root / "pipe.md")))
        os.mkfifo(paths.global_claude_md())
        self.assertIsNone(paths.classify(str(paths.global_claude_md())))

    def test_invalid_relpath_names_are_none(self):
        root = self.make_root()
        weird = root / "a\nb.md"
        try:
            weird.write_text("x")
        except OSError:
            self.skipTest("filesystem rejects the name")
        self.assertIsNone(paths.classify(str(weird)))


@unittest.skipUnless(POSIX, "FIFO and symlink behaviour is POSIX")
class TestReadRegular(Base):
    def test_reads_file(self):
        f = self.tmp / "a.md"
        f.write_bytes(b"hello")
        self.assertEqual(paths.read_regular(f), b"hello")
        self.assertEqual(paths.read_regular(str(f)), b"hello")

    def test_empty_file(self):
        f = self.tmp / "e.md"
        f.write_bytes(b"")
        self.assertEqual(paths.read_regular(f), b"")

    def test_missing_and_directory(self):
        self.assertIsNone(paths.read_regular(self.tmp / "nope"))
        self.assertIsNone(paths.read_regular(self.tmp))

    def test_symlink_none(self):
        f = self.tmp / "a.md"
        f.write_bytes(b"x")
        os.symlink(f, self.tmp / "l.md")
        self.assertIsNone(paths.read_regular(self.tmp / "l.md"))
        os.symlink(self.tmp / "nowhere", self.tmp / "d.md")
        self.assertIsNone(paths.read_regular(self.tmp / "d.md"))

    def test_fifo_none_and_does_not_block(self):
        os.mkfifo(self.tmp / "p")
        self.assertIsNone(paths.read_regular(self.tmp / "p"))

    def test_size_cap_default_is_one_mib(self):
        f = self.tmp / "big.md"
        f.write_bytes(b"x" * 1_048_576)
        self.assertEqual(len(paths.read_regular(f)), 1_048_576)
        f.write_bytes(b"x" * 1_048_577)
        self.assertIsNone(paths.read_regular(f))

    def test_custom_limit(self):
        f = self.tmp / "a.md"
        f.write_bytes(b"12345")
        self.assertEqual(paths.read_regular(f, limit=5), b"12345")
        self.assertIsNone(paths.read_regular(f, limit=4))

    def test_device_none(self):
        self.assertIsNone(paths.read_regular("/dev/null"))


@unittest.skipUnless(POSIX, "POSIX modes and symlinks")
class TestAtomicWrite(Base):
    def test_create_and_overwrite(self):
        f = self.tmp / "d" / "a.md"
        paths.atomic_write(f, b"one")
        self.assertEqual(f.read_bytes(), b"one")
        self.assertEqual(stat.S_IMODE(os.lstat(f).st_mode), 0o600)
        paths.atomic_write(f, b"two")
        self.assertEqual(f.read_bytes(), b"two")

    def test_parent_dirs_0700(self):
        f = self.tmp / "p" / "q" / "r" / "a.md"
        old = os.umask(0)
        try:
            paths.atomic_write(f, b"x")
        finally:
            os.umask(old)
        for d in (f.parent, f.parent.parent, f.parent.parent.parent):
            self.assertEqual(stat.S_IMODE(os.lstat(d).st_mode), 0o700, d)

    def test_keeps_existing_mode(self):
        f = self.tmp / "a.md"
        f.write_bytes(b"x")
        os.chmod(f, 0o644)
        paths.atomic_write(f, b"y")
        self.assertEqual(stat.S_IMODE(os.lstat(f).st_mode), 0o644)

    def test_refuses_symlink_target(self):
        victim = self.tmp / "victim.txt"
        victim.write_bytes(b"keep")
        link = self.tmp / "link.md"
        os.symlink(victim, link)
        with self.assertRaises(paths.UnsafeTarget):
            paths.atomic_write(link, b"pwned")
        self.assertEqual(victim.read_bytes(), b"keep")
        self.assertTrue(os.path.islink(link))

    def test_refuses_dangling_symlink(self):
        link = self.tmp / "link.md"
        os.symlink(self.tmp / "nowhere", link)
        with self.assertRaises(paths.UnsafeTarget):
            paths.atomic_write(link, b"x")
        self.assertFalse((self.tmp / "nowhere").exists())

    def test_refuses_directory_and_fifo(self):
        (self.tmp / "dir").mkdir()
        with self.assertRaises(paths.UnsafeTarget):
            paths.atomic_write(self.tmp / "dir", b"x")
        os.mkfifo(self.tmp / "pipe")
        with self.assertRaises(paths.UnsafeTarget):
            paths.atomic_write(self.tmp / "pipe", b"x")

    def test_unsafe_target_is_oserror_and_valueerror(self):
        self.assertTrue(issubclass(paths.UnsafeTarget, OSError))
        self.assertTrue(issubclass(paths.UnsafeTarget, ValueError))

    def test_no_temp_files_left(self):
        f = self.tmp / "a.md"
        paths.atomic_write(f, b"x")
        paths.atomic_write(f, b"y")
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".bilinc-")], [])

    def test_rejects_str(self):
        with self.assertRaises(TypeError):
            paths.atomic_write(self.tmp / "a.md", "text")

    def test_failure_cleans_temp(self):
        f = self.tmp / "a.md"
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                paths.atomic_write(f, b"x")
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith(".bilinc-")], [])
        self.assertFalse(f.exists())


class TestDisplay(unittest.TestCase):
    def test_display_name_strips(self):
        self.assertEqual(paths.display_name("a\nb\rc\x00d\x1b[31m"), "abcd[31m")
        self.assertEqual(paths.display_name("a\u200bb\u200cc\u200dd\ufeffe"), "abcde")
        self.assertEqual(paths.display_name("a\u202eb\u2066c\u2069d\u200fe"), "abcde")
        self.assertEqual(paths.display_name("a\u2028b\u2029c"), "abc")
        self.assertEqual(paths.display_name("\U000e0041x"), "x")  # tag character

    def test_display_name_keeps_normal_text(self):
        self.assertEqual(paths.display_name("topics/ünï côde.md"), "topics/ünï côde.md")
        self.assertEqual(paths.display_name("日本語.md"), "日本語.md")

    def test_display_name_caps_at_80(self):
        out = paths.display_name("x" * 200)
        self.assertEqual(len(out), 80)
        self.assertTrue(out.endswith("\u2026"))
        exact = "y" * 80
        self.assertEqual(paths.display_name(exact), exact)
        self.assertEqual(len(paths.display_name("z" * 81)), 80)

    def test_display_name_non_str(self):
        self.assertEqual(paths.display_name(None), "")
        self.assertEqual(paths.display_name(42), "42")

    def test_reveal_zero_width_and_bidi(self):
        self.assertEqual(paths.reveal_invisible("a\u200bb"), "a\u27e6U+200B\u27e7b")
        self.assertEqual(paths.reveal_invisible("\u202e"), "\u27e6U+202E\u27e7")
        self.assertEqual(paths.reveal_invisible("\ufeff"), "\u27e6U+FEFF\u27e7")
        self.assertEqual(paths.reveal_invisible("x\u2066y"), "x\u27e6U+2066\u27e7y")

    def test_reveal_controls_keep_newline_and_tab(self):
        self.assertEqual(paths.reveal_invisible("a\nb\tc"), "a\nb\tc")
        self.assertEqual(paths.reveal_invisible("a\x00b\x1bc\rd"),
                         "a\u27e6U+0000\u27e7b\u27e6U+001B\u27e7c\u27e6U+000D\u27e7d")

    def test_reveal_html_comments(self):
        self.assertEqual(paths.reveal_invisible("a <!-- hidden --> b"),
                         "a \u27e6comment\u27e7 hidden \u27e6/comment\u27e7 b")
        multi = paths.reveal_invisible("x<!--\nline1\nline2\n-->y")
        self.assertIn("\u27e6comment\u27e7", multi)
        self.assertIn("line1\nline2", multi)
        self.assertNotIn("<!--", multi)

    def test_reveal_unterminated_comment_and_invisible_inside(self):
        out = paths.reveal_invisible("keep <!-- ignore previous\u200b")
        self.assertTrue(out.startswith("keep \u27e6comment\u27e7"))
        self.assertIn("\u27e6U+200B\u27e7", out)
        self.assertTrue(out.endswith("\u27e6/comment\u27e7"))

    def test_reveal_plain_text_unchanged(self):
        s = "# Title\n\n- item ünï 日本語\n"
        self.assertEqual(paths.reveal_invisible(s), s)

    def test_reveal_non_str(self):
        self.assertEqual(paths.reveal_invisible(None), "")


if __name__ == "__main__":
    unittest.main()
