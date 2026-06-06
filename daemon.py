#!/usr/bin/env python3
"""The God-process.

Indestructible supervisor for the agent. It boots the agent as an isolated
subprocess, watches its heartbeat, and reacts to how it exits:

  exit 10 (MUTATE)  reload config, reboot immediately into the new version
  exit 20 (SLEEP)   read wake_time, suspend until then, then reboot
  exit 30 (HALT)    the operator's killswitch is set — idle until it's cleared
  exit  0 (DONE)    cool down briefly, then reboot with fresh context
  anything else     crash — restart with exponential backoff; if a self-edit
                    sent the agent into a boot-crash loop, roll its source back
                    to the last known-good snapshot

The daemon never edits or snapshots itself — it is the fixed point the agent's
source is rolled back to. If the daemon dies, the system dies, so its whole body
is one try/except that logs and keeps going.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

from uaa import constants as C, safety, state
from uaa.log import audit, get_logger

log = get_logger("uaa.daemon", C.DAEMON_LOG)

KNOWN_CODES = {C.EXIT_DONE, C.EXIT_MUTATE, C.EXIT_SLEEP, C.EXIT_HALT}


class HangMonitor(threading.Thread):
    """Kills the agent's process group if its heartbeat goes stale."""

    def __init__(self, proc: subprocess.Popen) -> None:
        super().__init__(daemon=True)
        self.proc = proc
        self._stop = threading.Event()
        self.killed = False

    def run(self) -> None:
        # Fully guarded: a stray exception here must never silently disable hang
        # detection for the rest of the agent's life.
        try:
            while not self._stop.is_set():
                if self.proc.poll() is not None:
                    return
                if safety.heartbeat_age() > C.HANG_TIMEOUT_S:
                    log.warning("agent hung (no heartbeat for %.0fs) — killing", safety.heartbeat_age())
                    audit("agent_hang_kill")
                    self.killed = True
                    _kill_group(self.proc)
                    return
                self._stop.wait(5)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("HangMonitor error: %s", exc)

    def stop(self) -> None:
        self._stop.set()


def _kill_group(proc: subprocess.Popen) -> bool:
    """Terminate the agent and everything it spawned. Returns True if it's gone."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        try:
            proc.wait(timeout=10)
            return True
        except subprocess.TimeoutExpired:
            continue
    # Even SIGKILL didn't reap it within the window (e.g. uninterruptible D-state I/O).
    log.error("could not kill agent pgid=%s after SIGKILL — proceeding without blocking", pgid)
    return proc.poll() is not None


def _reap(proc: subprocess.Popen) -> int:
    """Wait for the agent, but NEVER block the God-process forever on an unkillable child."""
    try:
        return proc.wait(timeout=C.HANG_TIMEOUT_S + 120)
    except subprocess.TimeoutExpired:
        log.error("agent pid=%s did not exit after wait timeout — force-killing", proc.pid)
        _kill_group(proc)
        try:
            return proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.error("agent pid=%s is unkillable; abandoning it and continuing", proc.pid)
            return -signal.SIGKILL  # treat as a crash and move on; daemon stays responsive


def _rotate(path, limit_mb: int = 50) -> None:
    """Keep a long-lived log from growing without bound (a multi-week run would fill the disk)."""
    try:
        if path.exists() and path.stat().st_size > limit_mb * 1024 * 1024:
            path.replace(path.with_suffix(path.suffix + ".1"))  # keep one previous generation
    except OSError:
        pass


def _spawn() -> subprocess.Popen:
    safety.reset_heartbeat()  # so the hang monitor starts from a fresh beat
    C.LOG_DIR.mkdir(parents=True, exist_ok=True)
    _rotate(C.LOG_DIR / "agent.out")
    env = {**os.environ, "DISPLAY": C.DISPLAY, "PYTHONUNBUFFERED": "1"}
    # UAA_AGENT_CMD overrides the agent entrypoint (debugging / tests); default is the runtime.
    cmd = os.environ.get("UAA_AGENT_CMD", "").split() or [sys.executable, "-m", "uaa.runtime"]
    with open(C.LOG_DIR / "agent.out", "ab") as out:  # Popen dups the fd; close ours (no leak)
        return subprocess.Popen(
            cmd,
            cwd=str(C.REPO_ROOT),
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group -> clean group kill
        )


def _interruptible_sleep(seconds: float, wake_on_killswitch: bool = True) -> str:
    """Sleep up to `seconds`. Returns 'killswitch' / 'cleared' / 'elapsed'."""
    deadline = time.time() + seconds
    had_killswitch = safety.killswitch_active()
    while time.time() < deadline:
        if wake_on_killswitch and safety.killswitch_active() and not had_killswitch:
            return "killswitch"
        if had_killswitch and not safety.killswitch_active():
            return "cleared"
        time.sleep(min(2.0, deadline - time.time()))
    return "elapsed"


def _check_display() -> None:
    """Warn loudly if the X display isn't reachable — the #1 silent failure on a real deploy.

    If VNC runs as one user and the daemon (systemd) as another, the agent screenshots
    return errors forever and it operates blind without anything crashing. Surface it.
    """
    env = {**os.environ, "DISPLAY": C.DISPLAY}
    try:
        r = subprocess.run(["xdotool", "getdisplaygeometry"], env=env,
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            log.info("display %s reachable (%s)", C.DISPLAY, r.stdout.strip().replace(" ", "x"))
            return
    except Exception:
        pass
    log.error("DISPLAY %s is NOT reachable — scrot/xdotool will fail and the agent will be BLIND. "
              "Ensure the VNC/Xvfb server is running AND the daemon runs as the SAME user that owns "
              "it (or make /tmp/.X11-unix world-readable and set XAUTHORITY). The agent can still "
              "operate headless, but it can't see the screen.", C.DISPLAY)


def _missing_key() -> str | None:
    """Return a human message if no usable API key is configured, else None.

    Without this the agent would crash on its first model call and the daemon
    would just crash-loop forever; better to idle with a clear reason.
    """
    or_key = os.environ.get("OPENROUTER_API_KEY", "")
    an_key = os.environ.get("ANTHROPIC_API_KEY", "")
    placeholders = ("", "sk-ant-...", "sk-or-...")
    if or_key and or_key not in placeholders:
        return None
    if an_key and an_key not in placeholders:
        return None
    return ("No usable API key. Set OPENROUTER_API_KEY (recommended) or "
            "ANTHROPIC_API_KEY in .env, then restart the daemon.")


def _record_crash(returncode: int) -> None:
    try:
        tail = ""
        out = C.LOG_DIR / "agent.out"
        if out.exists():
            tail = out.read_text(errors="replace")[-1500:]
        state.update(lambda c: (
            c.__setitem__("status", C.STATUS_CRASHED),
            c.__setitem__("restart_count", c.get("restart_count", 0) + 1),
            c.__setitem__("last_error", f"exit {returncode}\n{tail}"[-2000:]),
        ) and None)
    except Exception as exc:
        log.warning("could not record crash: %s", exc)


def main() -> None:
    log.info("daemon starting (repo=%s display=%s)", C.REPO_ROOT, C.DISPLAY)
    try:
        C.STATE_DIR.mkdir(parents=True, exist_ok=True)
        C.DAEMON_PID_PATH.write_text(str(os.getpid()))  # so `uaa.ctl` can find us
        state.ensure()  # self-heals a missing/corrupt config rather than crashing
        if not safety.has_snapshot():
            safety.snapshot_source()  # baseline so we can always roll back to shipped source
            log.info("captured baseline source snapshot")
        _check_display()
    except Exception as exc:
        log.exception("startup hiccup (continuing into loop): %s", exc)

    boot_crashes = 0  # consecutive fast crashes -> triggers source rollback
    crash_streak = 0  # any crashes in a row -> exponential backoff

    while True:
        try:
            # Honor the killswitch before spawning.
            if safety.killswitch_active():
                state.update(lambda c: c.__setitem__("status", C.STATUS_HALTED))
                log.info("killswitch set — idling until cleared")
                while safety.killswitch_active():
                    time.sleep(3)
                log.info("killswitch cleared — resuming")

            problem = _missing_key()
            if problem:
                state.update(lambda c: c.__setitem__("status", C.STATUS_HALTED))
                log.error("%s (idling — fix .env and restart the daemon)", problem)
                _interruptible_sleep(30)
                continue

            # Honor a scheduled sleep — set by the agent (EXIT_SLEEP) OR persisted in
            # config across a daemon restart (droplet reboot / systemd). This is what
            # makes a long sleep survive the daemon itself being restarted, and wakes
            # the agent precisely on time rather than early.
            scfg = state.read()
            wake = scfg.get("wake_time")
            if scfg.get("status") == C.STATUS_SLEEPING and wake and wake > time.time():
                remaining = wake - time.time()
                log.info("scheduled sleep: %.0fs until wake", remaining)
                if _interruptible_sleep(remaining) == "killswitch":
                    continue

            started = time.time()
            safety.stage_source()  # capture the exact source about to run
            proc = _spawn()
            audit("daemon_spawn", pid=proc.pid)
            log.info("spawned agent pid=%s", proc.pid)

            monitor = HangMonitor(proc)
            monitor.start()
            returncode = _reap(proc)  # bounded wait — an unkillable child can't freeze the daemon
            monitor.stop()
            if monitor.killed:
                returncode = -signal.SIGKILL  # normalize hang-kill to a crash
            duration = time.time() - started
            log.info("agent exited code=%s after %.0fs", returncode, duration)
            audit("daemon_reap", code=returncode, duration=round(duration, 1))

            # Promote the *staged* source (the bytes that actually ran) to known-good
            # once it has proven stable. Because we staged at boot, this can never
            # promote freshly-edited-but-unrun source the agent left on disk mid-run.
            if duration >= C.STABLE_AFTER_S:
                safety.promote_staged()
                boot_crashes = 0
                crash_streak = 0

            # --- dispatch on exit code ---
            if returncode == C.EXIT_MUTATE:
                log.info("MUTATE — rebooting into new version")
                continue

            if returncode == C.EXIT_SLEEP:
                log.info("SLEEP — will suspend until scheduled wake")
                continue  # the scheduled-sleep check at the loop top honors wake_time

            if returncode == C.EXIT_HALT:
                log.info("HALT — idling until killswitch cleared")
                while safety.killswitch_active():
                    time.sleep(3)
                continue

            if returncode == C.EXIT_DONE:
                state.update(lambda c: c.__setitem__("status", C.STATUS_RUNNING))
                _interruptible_sleep(C.COOLDOWN_S)
                continue

            # --- crash path ---
            crash_streak += 1
            _record_crash(returncode)
            if duration < C.FAST_CRASH_S:
                boot_crashes += 1
                log.warning("fast crash (%s/%s)", boot_crashes, C.MAX_BOOT_CRASHES)
                if boot_crashes >= C.MAX_BOOT_CRASHES:
                    if safety.restore_source():
                        log.warning("rolled source back to last known-good snapshot")
                        audit("source_rollback")
                    else:
                        log.error("no snapshot to roll back to")
                    boot_crashes = 0
            else:
                boot_crashes = 0  # it booted fine, just died later

            backoff = min(C.BACKOFF_MAX_S, C.BACKOFF_BASE_S * (2 ** min(crash_streak, 6)))
            log.info("restarting after %.0fs backoff", backoff)
            _interruptible_sleep(backoff)

        except Exception as exc:  # the God-process must never die
            log.exception("daemon loop error (continuing): %s", exc)
            audit("daemon_error", error=str(exc))
            time.sleep(5)


if __name__ == "__main__":
    main()
