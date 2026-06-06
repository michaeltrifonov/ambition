"""The `launch` tool must detach apps into their own session so they survive the
agent's reboots / the daemon's process-group kill.

    python3 tests/test_launch.py
"""
import os
import re
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uaa import runtime  # noqa: E402


def main() -> None:
    msg, err = runtime._launch("sleep 30")
    assert not err, msg
    pid = int(re.search(r"pid (\d+)", msg).group(1))
    time.sleep(0.3)
    try:
        assert os.getpgid(pid) != os.getpgid(os.getpid()), \
            "launched app shares the launcher's process group — would die on group-kill"
        assert os.getsid(pid) == pid, "launched app is not its own session leader"
        # empty command is an error, not a crash
        _, e2 = runtime._launch("   ")
        assert e2
        print("ok: launch detaches into its own session (survives the agent's group-kill)")
        print("LAUNCH TEST PASSED")
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    main()
