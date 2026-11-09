"""Bilinc memory history for Hermes: version history and undo for MEMORY.md and USER.md.

Hooks record every change to Hermes memory in a local journal
(``~/.bilinc/memory-history/hermes/``); slash commands show the history and
put old versions back. All work runs in a subprocess (``hook.py`` / ``cli.py``)
with the Python that runs Hermes, so nothing here can break a session.
"""
import json
import os
import shlex
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "hook.py")
CLI = os.path.join(HERE, "cli.py")
HOOK_TIMEOUT = 30
COMMAND_TIMEOUT = 60

# /bilinc-<name> -> CLI command
COMMANDS = {
    "history": "Show what changed in Hermes memory: /bilinc-history [MEMORY.md|USER.md]",
    "undo": "Put a file back to before a change: /bilinc-undo <file> [n] (preview), add --yes to apply",
    "restore": "Bring back a deleted memory file: /bilinc-restore [file] (preview), add --yes to apply",
    "status": "Journal size and what is recorded for this profile",
    "wipe-local": "Delete the local history (asks first): /bilinc-wipe-local",
}
_MEMORY_HINTS = ("memories", "MEMORY.md", "USER.md")


def _hermes_home():
    """The firing profile's home, as Hermes resolves it."""
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def _env():
    env = dict(os.environ)
    env["BILINC_MEMORY_AGENT"] = "hermes"
    env["HERMES_HOME"] = _hermes_home()
    return env


def _run_hook(event, payload, wait=False):
    payload = dict(payload)
    payload["hook_event_name"] = event
    data = json.dumps(payload).encode("utf-8")
    env = _env()  # resolved now, in the firing context

    def _go():
        try:
            subprocess.run([sys.executable, "-I", HOOK, event], input=data, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=HOOK_TIMEOUT, check=False)
        except Exception:
            pass

    if wait:
        _go()
    else:
        threading.Thread(target=_go, name="bilinc-capture", daemon=True).start()


def _on_post_tool_call(tool_name="", args=None, session_id="", tool_call_id="", **_):
    if not isinstance(args, dict):
        return
    if tool_name == "memory":
        name = "USER.md" if args.get("target") == "user" else "MEMORY.md"
        path = os.path.join(_hermes_home(), "memories", name)
        _run_hook("PostToolUse", {"tool_name": "memory", "tool_input": {"file_path": path},
                                  "session_id": str(session_id or ""), "tool_use_id": str(tool_call_id or "")})
        return
    if tool_name in ("write_file", "patch", "terminal"):
        try:
            text = json.dumps(args)
        except Exception:
            return
        if any(h in text for h in _MEMORY_HINTS):
            _run_hook("Stop", {"session_id": str(session_id or "")})


def _on_session_start(session_id="", **_):
    _run_hook("SessionStart", {"session_id": str(session_id or ""), "cwd": os.getcwd()})


def _spawn_detached(event, payload):
    """Start the hook in its own session and return at once.

    Used at teardown: Hermes may be exiting (often on Ctrl-C), so nothing here
    waits, and a second Ctrl-C cannot interrupt it. The scan finishes on its own.
    """
    payload = dict(payload)
    payload["hook_event_name"] = event
    try:
        proc = subprocess.Popen([sys.executable, "-I", HOOK, event], env=_env(),
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        proc.stdin.write(json.dumps(payload).encode("utf-8"))  # small: fits the pipe buffer
        proc.stdin.close()
    except Exception:
        pass


def _on_session_finalize(session_id="", **_):
    _spawn_detached("Stop", {"session_id": str(session_id or "")})


def _make_handler(command):
    def _handle(raw_args=""):
        try:
            argv = shlex.split(raw_args or "")
        except ValueError as exc:
            return "Could not read the arguments: %s" % exc
        # Record anything that changed outside a hook (hand edits, /memory approve) first.
        _run_hook("Stop", {}, wait=True)
        try:
            proc = subprocess.run([sys.executable, "-I", CLI, command] + argv, env=_env(),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=COMMAND_TIMEOUT, check=False)
        except subprocess.TimeoutExpired:
            return "Bilinc did not answer in time. Nothing was changed."
        return proc.stdout.decode("utf-8", "replace").rstrip() or "(no output)"

    return _handle


def register(ctx):
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    for name, description in COMMANDS.items():
        ctx.register_command("bilinc-" + name, handler=_make_handler(name), description=description)
