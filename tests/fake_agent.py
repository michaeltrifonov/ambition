"""A stand-in agent for black-box testing the daemon's supervision.

Each boot it reads a counter, performs the scripted action for that boot index
(from UAA_FAKE_PLAN, comma-separated), and exits with the matching code so the
daemon's exit-code dispatch, sleep scheduling, crash backoff, rollback, and
hang-kill can all be exercised with real subprocesses.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uaa import constants as C, safety, state  # noqa: E402

COUNTER = C.STATE_DIR / ".fakeboot_counter"


def _next_index() -> int:
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        n = int(COUNTER.read_text())
    except (OSError, ValueError):
        n = 0
    COUNTER.write_text(str(n + 1))
    return n


def _sleep_for(seconds: float) -> None:
    # Mirror the real runtime: a scheduled sleep sets status=sleeping + wake_time.
    state.update(lambda c: (c.__setitem__("status", C.STATUS_SLEEPING),
                            c.__setitem__("wake_time", time.time() + seconds)) and None)


def main() -> None:
    safety.beat()
    # Mirror the real runtime: every boot starts in the running state.
    state.update(lambda c: c.__setitem__("status", C.STATUS_RUNNING))
    i = _next_index()
    (C.STATE_DIR / f".fakeboot_{i}").write_text(str(time.time()))
    plan = [a for a in os.environ.get("UAA_FAKE_PLAN", "").split(",")]
    action = plan[i] if i < len(plan) and plan[i] else "park"
    safety.beat()

    if action == "mutate":
        sys.exit(C.EXIT_MUTATE)
    if action == "sleep":
        _sleep_for(2)
        sys.exit(C.EXIT_SLEEP)
    if action == "done":
        sys.exit(C.EXIT_DONE)
    if action == "crash":
        time.sleep(0.2)
        sys.exit(1)
    if action == "hang":
        time.sleep(3600)  # never beats again -> daemon hang-kills it
    _sleep_for(3600)  # "park": sleep far into the future so no further boots happen
    sys.exit(C.EXIT_SLEEP)


if __name__ == "__main__":
    main()
