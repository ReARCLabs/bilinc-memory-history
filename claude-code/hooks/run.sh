#!/bin/sh
# Bilinc hook launcher (POSIX sh). Never blocks a session: every failure path exits 0.
# Usage: run.sh <EventName>   (the hook JSON arrives on stdin and is passed through)
# No `set -e` on purpose: a failing probe must still end in `exit 0`.

ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "$ROOT" ]; then
  ROOT="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)" || exit 0
fi

PY="$(command -v python3 2>/dev/null)" || exit 0
[ -n "$PY" ] || exit 0

# macOS ships /usr/bin/python3 as a stub that pops the Command Line Tools
# installer dialog when the tools are missing. Detect that and stay silent.
if [ "$PY" = "/usr/bin/python3" ] && [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
  if ! xcode-select -p >/dev/null 2>&1 && [ ! -d /Library/Developer/CommandLineTools ]; then
    exit 0
  fi
fi

exec "$PY" -I "$ROOT/hooks/hook.py" "${1:-}"
