#!/usr/bin/env bash
# Run the UAA test suite. The MCP live test needs the `mcp` package; if a venv
# with it exists we use that, otherwise we skip that one test.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY=python3
MCP_PY=""
[ -x ./venv/bin/python ] && ./venv/bin/python -c "import mcp" 2>/dev/null && MCP_PY=./venv/bin/python

fail=0
run() { echo "=== $1 ==="; shift; "$@" || { echo "FAILED"; fail=1; }; echo; }

run "compile"            $PY -m py_compile daemon.py uaa/*.py tests/*.py
run "computer commands"  $PY tests/test_computer_commands.py
run "launch detach"      $PY tests/test_launch.py
run "compaction"         $PY tests/test_compaction.py
run "badrequest recover" $PY tests/test_badrequest.py
run "agent loop"         $PY tests/test_agent_loop.py
run "sleep"              $PY tests/test_sleep.py
run "daemon supervision" $PY tests/test_daemon_supervision.py
if [ -n "$MCP_PY" ]; then
  run "mcp live"         $MCP_PY tests/test_mcp_live.py
  run "mcp self-install" $MCP_PY tests/test_mcp_selfinstall.py
else
  echo "=== mcp live + self-install ===  SKIPPED (no venv with the mcp package)"; echo
fi

[ $fail -eq 0 ] && echo "ALL TESTS PASSED" || { echo "SOME TESTS FAILED"; exit 1; }
