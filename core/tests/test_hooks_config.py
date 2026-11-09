"""Static checks of hooks/hooks.json, .claude-plugin/plugin.json and hooks/run.sh.

Run alone:  python3 -m unittest tests.test_hooks_config   (from the repo root)
No network, no real ~/.claude, no real ~/.bilinc.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "claude-code")
HOOKS_JSON = os.path.join(ROOT, "hooks", "hooks.json")
PLUGIN_JSON = os.path.join(ROOT, ".claude-plugin", "plugin.json")
RUN_SH = os.path.join(ROOT, "hooks", "run.sh")
HOOK_PY = os.path.join(ROOT, "hooks", "hook.py")

# Handler fields documented at https://code.claude.com/docs/en/hooks (command hooks).
ALLOWED_HANDLER_KEYS = {"type", "command", "args", "timeout", "async", "asyncRewake", "shell", "statusMessage",
                        "once", "if"}
USERCONFIG_KEYS = {"type", "title", "description", "required", "default", "options", "multiple", "sensitive",
                   "min", "max"}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class HooksJsonTests(unittest.TestCase):
    def setUp(self):
        self.doc = _load(HOOKS_JSON)
        self.events = self.doc["hooks"]

    def test_top_level_wrapper(self):
        self.assertIn("hooks", self.doc)
        self.assertTrue(set(self.doc) <= {"description", "hooks"})
        self.assertEqual(set(self.events), {"SessionStart", "PostToolUse", "FileChanged", "Stop"})

    def test_every_hook_runs_run_sh_in_exec_form_with_its_event(self):
        for event, groups in self.events.items():
            self.assertIsInstance(groups, list)
            for group in groups:
                self.assertTrue(set(group) <= {"matcher", "hooks"})
                for h in group["hooks"]:
                    self.assertEqual(h["type"], "command")
                    self.assertEqual(h["command"], "sh")  # exec form: no shell parsing, no quoting traps
                    self.assertEqual(h["args"], ["${CLAUDE_PLUGIN_ROOT}/hooks/run.sh", event])
                    self.assertTrue(set(h) <= ALLOWED_HANDLER_KEYS, set(h) - ALLOWED_HANDLER_KEYS)
                    self.assertIsInstance(h["timeout"], int)
                    self.assertTrue(0 < h["timeout"] <= 30)

    def test_post_tool_use_matcher_and_async(self):
        (group,) = self.events["PostToolUse"]
        self.assertEqual(group["matcher"], "Write|Edit|MultiEdit|NotebookEdit")
        (h,) = group["hooks"]
        self.assertIs(h["async"], True)
        self.assertEqual(h["timeout"], 10)

    def test_file_changed_has_no_matcher(self):
        # A matcher on FileChanged is also a watch list of literal file names; "*" would watch a file
        # literally named "*". Omitted = runs for every path returned in watchPaths.
        (group,) = self.events["FileChanged"]
        self.assertNotIn("matcher", group)
        self.assertNotIn("async", group["hooks"][0])

    def test_session_start_is_synchronous_and_stop_is_async(self):
        (g,) = self.events["SessionStart"]
        self.assertNotIn("async", g["hooks"][0])  # async output would land a turn late
        self.assertNotIn("matcher", g)
        (g,) = self.events["Stop"]
        self.assertIs(g["hooks"][0]["async"], True)
        self.assertNotIn("matcher", g)  # Stop does not support matchers

    def test_nothing_in_the_file_needs_a_shell(self):
        text = _read(HOOKS_JSON)
        for bad in ("&&", "||", ";", "`", "$(", "eval", "shell=True"):
            self.assertNotIn(bad, text)
        self.assertNotIn("user_config", text)  # not allowed in shell form; we never need it here


class PluginJsonTests(unittest.TestCase):
    def setUp(self):
        self.doc = _load(PLUGIN_JSON)

    def test_identity(self):
        self.assertEqual(self.doc["name"], "bilinc")
        self.assertRegex(self.doc["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        for reserved in ("claude-", "anthropic-", "anthropics-", "cc-plugin-"):
            self.assertFalse(self.doc["name"].startswith(reserved))
        self.assertEqual(self.doc["license"], "MIT")
        self.assertIsInstance(self.doc["author"], dict)
        self.assertTrue(self.doc["author"]["name"])
        self.assertTrue(self.doc["version"] and self.doc["description"])
        self.assertTrue(self.doc["homepage"].startswith("https://"))

    def test_api_key_is_a_sensitive_string_user_config(self):
        opt = self.doc["userConfig"]["api_key"]
        self.assertIs(opt["sensitive"], True)
        self.assertEqual(opt["type"], "string")
        self.assertTrue(opt["title"] and opt["description"])
        self.assertTrue(set(opt) <= USERCONFIG_KEYS, set(opt) - USERCONFIG_KEYS)  # unknown keys are an error
        self.assertNotIn("required", opt)  # Cloud is optional; local capture needs no key
        self.assertNotIn("default", opt)

    def test_only_documented_top_level_keys(self):
        known = {"$schema", "name", "displayName", "version", "description", "author", "homepage", "repository",
                 "license", "keywords", "metadata", "icon", "documentationUrl", "supportUrl", "privacyPolicyUrl",
                 "termsOfServiceUrl", "defaultEnabled", "dependencies", "settings", "userConfig", "channels",
                 "skills", "commands", "agents", "hooks", "mcpServers", "lspServers", "outputStyles",
                 "workflows", "experimental", "types"}
        self.assertTrue(set(self.doc) <= known, set(self.doc) - known)

    def test_no_secret_or_machine_specific_values(self):
        text = _read(PLUGIN_JSON)
        self.assertNotRegex(text, r"/Users/|/home/|bil_live_|sk-[A-Za-z0-9]{10,}")


class LayoutTests(unittest.TestCase):
    def test_no_bin_directory_anywhere(self):
        for dirpath, dirnames, _ in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            self.assertNotIn("bin", dirnames, dirpath)

    def test_manifests_are_in_the_documented_places(self):
        self.assertTrue(os.path.isfile(PLUGIN_JSON))
        self.assertTrue(os.path.isfile(HOOKS_JSON))
        # Only plugin.json (and marketplace.json) live inside .claude-plugin/.
        for name in os.listdir(os.path.join(ROOT, ".claude-plugin")):
            self.assertIn(name, ("plugin.json", "marketplace.json"))

    def test_no_shell_true_in_hook_code(self):
        for rel in ("hooks/hook.py", "lib/bilinc_memory/capture.py"):
            src = _read(os.path.join(ROOT, rel))
            self.assertNotIn("shell=True", src)
            self.assertNotIn("os.system", src)
            self.assertNotRegex(src, r"\beval\(|\bexec\(")

    def test_hook_py_has_no_third_party_imports(self):
        src = _read(HOOK_PY)
        imports = set(re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][\w]*)", src, re.M))
        self.assertTrue(imports <= {"json", "os", "sys", "signal", "bilinc_memory"}, imports)


class RunShTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bilinc-runsh-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_sh(self, path_dirs, stdin=b"{}", event="Stop", extra_env=None):
        env = {"PATH": os.pathsep.join(path_dirs), "HOME": self.tmp, "CLAUDE_PLUGIN_ROOT": ROOT,
               "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "c"), "BILINC_MEMORY_HOME": os.path.join(self.tmp, "b")}
        env.update(extra_env or {})
        return subprocess.run(["/bin/sh", RUN_SH, event], input=stdin, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30)

    def test_syntax_and_mode(self):
        subprocess.run(["/bin/sh", "-n", RUN_SH], check=True)
        self.assertTrue(os.stat(RUN_SH).st_mode & stat.S_IXUSR)
        first = _read(RUN_SH).splitlines()[0]
        self.assertEqual(first.strip(), "#!/bin/sh")

    def test_source_quotes_and_never_evals(self):
        src = _read(RUN_SH)
        self.assertNotRegex(src, r"\beval\b")
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("set -e", code)  # a failing probe must still end in exit 0
        self.assertIn('exec "$PY" -I "$ROOT/hooks/hook.py" "${1:-}"', src)
        # The macOS stub guard must be present.
        self.assertIn("/usr/bin/python3", src)
        self.assertIn("xcode-select -p", src)
        self.assertIn("/Library/Developer/CommandLineTools", src)

    def test_no_python3_on_path_exits_zero_silently(self):
        empty = os.path.join(self.tmp, "emptybin")
        os.makedirs(empty)
        r = self.run_sh([empty])
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_runs_python3_in_isolated_mode_with_hook_py_and_event(self):
        fake = os.path.join(self.tmp, "fakebin")
        os.makedirs(fake)
        log = os.path.join(self.tmp, "args.txt")
        with open(os.path.join(fake, "python3"), "w") as fh:
            fh.write('#!/bin/sh\nprintf "%%s\\n" "$@" > "%s"\ncat > /dev/null\n' % log)
        os.chmod(os.path.join(fake, "python3"), 0o755)
        r = self.run_sh([fake, "/usr/bin", "/bin"], event="PostToolUse", stdin=b'{"a":1}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_read(log).split("\n")[:3], ["-I", HOOK_PY, "PostToolUse"])

    def test_plugin_root_with_spaces_works(self):
        spaced = os.path.join(self.tmp, "plugin root")
        shutil.copytree(os.path.join(ROOT, "hooks"), os.path.join(spaced, "hooks"))
        shutil.copytree(os.path.join(ROOT, "lib"), os.path.join(spaced, "lib"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp, "CLAUDE_PLUGIN_ROOT": spaced,
               "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "c"), "BILINC_MEMORY_HOME": os.path.join(self.tmp, "b")}
        r = subprocess.run(["sh", os.path.join(spaced, "hooks", "run.sh"), "Stop"], input=b"{}", env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, b""))


if __name__ == "__main__":
    unittest.main()
