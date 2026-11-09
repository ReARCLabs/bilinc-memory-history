#!/usr/bin/env python3
"""Bilinc hook entry point: ``python3 -I hook.py <EventName>`` (JSON on stdin).

Contract: this script NEVER blocks or fails a Claude Code session. Any problem
(bad stdin, oversized input, unexpected exception, unsafe store, timeout) ends
in exit code 0, nothing on stdout and at most one short line on stderr.
Output, when there is any, is a single line of compact JSON on stdout.
"""
import json
import os
import sys

MAX_STDIN = 4 * 1024 * 1024  # 4 MiB: a 1 MiB Write payload fits with JSON escaping
EVENTS = ("PostToolUse", "FileChanged", "SessionStart", "Stop")
# Hard wall-clock stop. Claude Code does not enforce `timeout` on async hooks,
# so we enforce it ourselves (POSIX only; harmless no-op elsewhere).
ALARM_SECONDS = {"SessionStart": 12, "FileChanged": 8, "PostToolUse": 25, "Stop": 25}


def _quit(code=0):
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(code)


def _on_alarm(signum, frame):  # pragma: no cover - timing dependent
    try:
        sys.stderr.write("bilinc: hook timed out\n")
    except Exception:
        pass
    _quit(0)


def _read_payload():
    raw = sys.stdin.buffer.read(MAX_STDIN + 1)
    if not raw or len(raw) > MAX_STDIN:
        return None
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else None


def main(argv):
    event = argv[1] if len(argv) > 1 else ""
    if event not in EVENTS:
        return 0
    try:
        import signal

        signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(ALARM_SECONDS.get(event, 10))
    except Exception:
        pass
    payload = _read_payload()
    if payload is None:
        return 0
    named = payload.get("hook_event_name")
    if named is not None and named != event:
        return 0
    lib = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib")
    sys.path.insert(0, lib)
    from bilinc_memory import capture

    result = capture.handle(event, payload)
    if isinstance(result, dict) and result:
        sys.stdout.write(json.dumps(result, separators=(",", ":"), ensure_ascii=True) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        main(sys.argv)
    except BaseException as exc:  # noqa: BLE001 - a hook must never fail a session
        try:
            sys.stderr.write("bilinc: hook skipped (%s)\n" % type(exc).__name__)
        except Exception:
            pass
    _quit(0)
