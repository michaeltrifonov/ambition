"""Sleep: records why+when it slept (so wake knows the reason + elapsed time), and
the scheduled sleep survives a daemon (re)start instead of waking the agent early.

    python3 tests/test_sleep.py
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["UAA_HOME"] = tempfile.mkdtemp(prefix="uaa_sleep_unit_")

from uaa import constants as C, runtime, state  # noqa: E402


def test_records_and_surfaces():
    state.ensure()
    out, err, code = runtime.dispatch("sleep", {"seconds": 43200, "reason": "waiting for creator reply re: account verification"})
    assert code == C.EXIT_SLEEP and not err, (out, err, code)
    cfg = state.read()
    assert cfg["sleep_reason"].startswith("waiting for creator reply")
    assert cfg["slept_at"] and cfg["wake_time"] and cfg["sleep_requested_s"] == 43200
    assert runtime._fmt_dur(43200) == "12h"

    # the boot seed surfaces the wake note + the current time (so it knows elapsed + now)
    wake_note = "You just woke from a sleep you started 12h 1m ago. The reason you slept: waiting for creator reply"
    txt = runtime.seed_message(cfg, {"connected": [], "failed": {}}, wake_note)["content"][0]["text"]
    assert "Current time:" in txt
    assert "12h 1m ago" in txt and "waiting for creator reply" in txt
    print("ok: sleep records reason+timing; wake boot surfaces the reason, elapsed, and current time")


def test_durable_across_daemon_restart():
    home = Path(tempfile.mkdtemp(prefix="uaa_sleep_dur_"))
    (home / "state").mkdir(parents=True)
    wake = time.time() + 4  # as if an agent slept and the daemon was then restarted
    (home / "state" / "config.json").write_text(json.dumps({
        "status": "sleeping", "wake_time": wake, "current_model": "x", "boot_count": 0,
        "mcp_servers": [], "active_tools": [], "memory_context": "",
    }))
    marker = home / "AGENT_BOOTED"
    stub = home / "stub_agent.py"  # a minimal "agent": mark that we booted, then idle briefly
    stub.write_text(f"open(r'{marker}', 'w').close()\nimport time; time.sleep(5)\n")
    env = {
        **os.environ, "UAA_HOME": str(home), "OPENROUTER_API_KEY": "sk-or-test-fake",
        "UAA_AGENT_CMD": f"{sys.executable} {stub}", "UAA_COOLDOWN_S": "1",
    }

    proc = subprocess.Popen([sys.executable, str(REPO / "daemon.py")], env=env,
                            cwd=str(REPO), start_new_session=True)
    try:
        time.sleep(3)  # still inside the 4s scheduled sleep
        assert not marker.exists(), "daemon spawned the agent EARLY — scheduled sleep not honored across restart"
        deadline = time.time() + 8
        while time.time() < deadline and not marker.exists():
            time.sleep(0.2)
        assert marker.exists(), "daemon never woke the agent at the scheduled time"
        print("ok: daemon honors a persisted scheduled sleep across a restart (wakes on time, not early)")
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    test_records_and_surfaces()
    test_durable_across_daemon_restart()
    print("SLEEP TEST PASSED")
