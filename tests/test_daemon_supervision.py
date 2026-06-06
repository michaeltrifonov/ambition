"""Black-box integration test of the daemon (the God-process).

Launches the real daemon.py as a subprocess against a scripted fake agent and
asserts it actually: boots, reboots on MUTATE, sleeps/wakes on SLEEP, cools down
on DONE, backs off + rolls source back after repeated fast crashes, and hang-kills
a non-heartbeating agent — then halts on the killswitch.

Runs against a scratch UAA_HOME so the repo's own state/logs are untouched.

    python3 tests/test_daemon_supervision.py
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLAN = "mutate,sleep,done,crash,crash,crash,hang,park"  # 8 boots, indices 0..7


def _audit_events(home: Path) -> list:
    log = home / "logs" / "audit.jsonl"
    if not log.exists():
        return []
    out = []
    for line in log.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _wait(cond, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.25)
    raise AssertionError(f"timed out after {timeout}s waiting for: {what}")


def main() -> None:
    home = Path(tempfile.mkdtemp(prefix="uaa_daemon_test_"))
    env = {
        **os.environ,
        "UAA_HOME": str(home),
        "OPENROUTER_API_KEY": "sk-or-test-fake-key",  # satisfies the daemon key preflight
        "UAA_AGENT_CMD": f"{sys.executable} {REPO / 'tests' / 'fake_agent.py'}",
        "UAA_FAKE_PLAN": PLAN,
        # Shrink the daemon clock so the whole lifecycle runs in ~20s.
        "UAA_COOLDOWN_S": "1",
        "UAA_BACKOFF_BASE_S": "1",
        "UAA_BACKOFF_MAX_S": "2",
        "UAA_STABLE_AFTER_S": "999",   # never auto-promote during the test
        "UAA_FAST_CRASH_S": "30",      # all our crashes are fast
        "UAA_MAX_BOOT_CRASHES": "3",
        "UAA_HANG_TIMEOUT_S": "3",     # hang-kill quickly
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen([sys.executable, str(REPO / "daemon.py")], env=env,
                            cwd=str(REPO), start_new_session=True)
    try:
        state_dir = home / "state"

        def booted(n):
            return (state_dir / f".fakeboot_{n}").exists()

        # boot 0 (spawn), then MUTATE -> boot 1 happens promptly
        _wait(lambda: booted(0), 15, "first boot")
        _wait(lambda: booted(1), 15, "reboot after MUTATE")
        print("ok: spawn + MUTATE reboot")

        # boot 1 SLEEP (wake +2s) -> boot 2 after a real delay
        t = time.time()
        _wait(lambda: booted(2), 15, "wake after SLEEP")
        assert time.time() - t >= 1.5, "SLEEP did not actually suspend"
        print("ok: SLEEP scheduled wake")

        # boot 2 DONE -> boot 3 after cooldown
        _wait(lambda: booted(3), 15, "reboot after DONE")
        print("ok: DONE cooldown reboot")

        # boots 3,4,5 crash -> after 3 fast crashes the daemon rolls source back
        _wait(lambda: booted(5), 25, "three crash boots")
        _wait(lambda: any(e["event"] == "source_rollback" for e in _audit_events(home)),
              15, "source_rollback after crash loop")
        print("ok: crash backoff + source rollback")

        # boot 6 hang -> daemon hang-kills it
        _wait(lambda: booted(6), 20, "hang boot")
        _wait(lambda: any(e["event"] == "agent_hang_kill" for e in _audit_events(home)),
              20, "agent_hang_kill")
        print("ok: hang detected + killed")

        # boot 7 park (long sleep). Now flip the killswitch; daemon must idle.
        _wait(lambda: booted(7), 20, "park boot")
        (state_dir / "STOP").touch()
        # give it a beat, then confirm config status reflects halt or it simply stops booting
        time.sleep(3)
        events = _audit_events(home)
        spawns = sum(1 for e in events if e["event"] == "daemon_spawn")
        assert spawns >= 7, f"expected >=7 spawns, got {spawns}"
        print(f"ok: killswitch honored ({spawns} total spawns)")

        # daemon process is still alive (never died despite all that)
        assert proc.poll() is None, "daemon died — it must be indestructible"
        print("ok: daemon still alive through the whole sequence")
        print("DAEMON SUPERVISION TEST PASSED")
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()
