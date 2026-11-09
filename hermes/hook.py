#!/usr/bin/env python3
"""Bilinc capture for Hermes: ``python3 -I hook.py <EventName>`` (JSON on stdin).

Run by the plugin (``__init__.py``) as a subprocess, so a crash or a slow disk
never reaches the Hermes process. Never fails: any problem ends in exit 0 with
nothing on stdout and at most one short line on stderr.
"""
import json
import os
import sys

MAX_STDIN = 1024 * 1024
EVENTS = ("PostToolUse", "SessionStart", "Stop")
ALARM_SECONDS = 25


def _quit(code=0):
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(code)


def main(argv):
    event = argv[1] if len(argv) > 1 else ""
    if event not in EVENTS:
        return
    try:
        import signal

        signal.signal(signal.SIGALRM, lambda *_: _quit(0))
        signal.alarm(ALARM_SECONDS)
    except Exception:
        pass
    raw = sys.stdin.buffer.read(MAX_STDIN + 1)
    if not raw or len(raw) > MAX_STDIN:
        return
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        return
    os.environ["BILINC_MEMORY_AGENT"] = "hermes"
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from bilinc_memory import capture

    capture.handle(event, payload)  # Hermes ignores hook output; nothing is printed


if __name__ == "__main__":
    try:
        main(sys.argv)
    except BaseException as exc:  # noqa: BLE001 - capture must never fail a session
        try:
            sys.stderr.write("bilinc: capture skipped (%s)\n" % type(exc).__name__)
        except Exception:
            pass
    _quit(0)
