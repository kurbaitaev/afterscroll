#!/bin/bash
# saved-to-notes quick verify: the same checks as CI (.github/workflows/ci.yml).
# Run from anywhere; prints PASS/FAIL per check and exits non-zero on any failure.
# Needs .venv once: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt 'ruff==0.9.10'
set -uo pipefail
cd "$(dirname "$0")/../../.." || exit 2
PY=.venv/bin/python; [ -x "$PY" ] || { echo "FAIL  no .venv (see the header of this script)"; echo "VERIFY FAILED"; exit 1; }
RUFF=.venv/bin/ruff; [ -x "$RUFF" ] || RUFF=ruff
fail=0
check() {  # name, command...
  local name="$1"; shift
  if out="$("$@" 2>&1)"; then echo "PASS  $name"
  else fail=1; echo "FAIL  $name"; echo "$out" | tail -15 | sed 's/^/      /'; fi
}
check compile     "$PY" -m compileall -q bot.py acquire.py notion.py ledger.py watchdog.py doctor.py
check ruff        "$RUFF" check --no-cache .
check pipeline    "$PY" tests/test_pipeline.py
check cards       "$PY" tests/test_cards.py
if command -v shellcheck >/dev/null; then check shellcheck shellcheck ctl.sh install.sh
else echo "SKIP  shellcheck (not installed; CI still runs it)"; fi
[ "$fail" = 0 ] && echo "VERIFY PASSED" || echo "VERIFY FAILED"
exit $fail
