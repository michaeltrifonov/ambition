"""Unrestricted Autonomous Agent (UAA) — runtime package.

Architecture (see README):
  daemon.py        the God-process: supervises, recovers, schedules, rolls back
  uaa/runtime.py   the agent: perceive -> think -> act, self-modifies, sleeps
  state/config.json the shared nervous system the agent edits and the daemon reads

The agent self-modifies by editing files under uaa/ (its body) and signalling a
mutate-reboot. It must NOT edit daemon.py (the God-process) — that's the continuity
the daemon protects and rolls back to if the agent bricks its own source.
"""

__version__ = "0.1.0"
