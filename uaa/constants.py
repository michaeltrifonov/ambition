"""Paths, the daemon<->agent protocol, and model/tool/pricing tables.

Everything is computed relative to the repo root so the tree is portable across
the local checkout and the droplet (no absolute paths baked in).
"""
from __future__ import annotations

import os
from pathlib import Path

# Load .env if python-dotenv is present (optional). Done once, here, so both the
# daemon and the agent subprocess see the same environment.
try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "uaa"            # the agent's body; always the live source tree

# UAA_HOME relocates runtime state/logs/tools (defaults to the repo). Lets you run
# more than one organism, or point a test at a scratch dir without touching the repo.
HOME_DIR = Path(os.environ.get("UAA_HOME", REPO_ROOT))
STATE_DIR = HOME_DIR / "state"
LOG_DIR = HOME_DIR / "logs"
MCP_DIR = HOME_DIR / "mcp"

CONFIG_PATH = STATE_DIR / "config.json"
HEARTBEAT_PATH = STATE_DIR / "heartbeat"
KILLSWITCH_PATH = STATE_DIR / "STOP"          # touch this file to halt the organism
DAEMON_PID_PATH = STATE_DIR / "daemon.pid"    # the God-process writes its pid here
SNAPSHOT_DIR = STATE_DIR / ".last_good"       # daemon's rollback copy of uaa/ (proven source)
STAGED_DIR = STATE_DIR / ".staged"            # copy of uaa/ as it booted (the code that ran)

AUDIT_LOG = LOG_DIR / "audit.jsonl"           # append-only record of every action
OPERATOR_INBOX = LOG_DIR / "operator_inbox.jsonl"  # messages the agent leaves for the human
AGENT_LOG = LOG_DIR / "agent.log"             # agent stdout/stderr
DAEMON_LOG = LOG_DIR / "daemon.log"           # daemon lifecycle events

# --- daemon <-> agent exit-code protocol -----------------------------------
# The agent signals intent purely through its process exit code; config.json
# carries the payload (new model, wake_time, etc.).
EXIT_DONE = 0      # finished a cycle cleanly -> daemon cools down, then reboots
EXIT_MUTATE = 10   # changed config/source -> daemon reloads config, reboots now
EXIT_SLEEP = 20    # set wake_time -> daemon suspends until then, then reboots
EXIT_HALT = 30     # requested halt -> daemon idles until the killswitch is cleared
# any other nonzero (incl. negative = signalled/killed) == crash -> backoff reboot

# --- status values written to config.status --------------------------------
STATUS_RUNNING = "running"
STATUS_SLEEPING = "sleeping"
STATUS_CRASHED = "crashed"
STATUS_MUTATING = "mutating"
STATUS_HALTED = "halted"

# --- computer-use environment ----------------------------------------------
DISPLAY = os.environ.get("UAA_DISPLAY", ":1")
DISPLAY_WIDTH = int(os.environ.get("UAA_DISPLAY_WIDTH", "1280"))
DISPLAY_HEIGHT = int(os.environ.get("UAA_DISPLAY_HEIGHT", "800"))
# display_number for the computer tool (the N in ":N")
try:
    DISPLAY_NUMBER = int(DISPLAY.lstrip(":").split(".")[0])
except ValueError:
    DISPLAY_NUMBER = 1

CREATOR_EMAIL = os.environ.get("UAA_CREATOR_EMAIL", "")

# --- daemon tuning (env-overridable for ops + tests) ------------------------
def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


HANG_TIMEOUT_S = _envf("UAA_HANG_TIMEOUT_S", 600)   # heartbeat stale longer -> kill + restart
STABLE_AFTER_S = _envf("UAA_STABLE_AFTER_S", 60)    # alive this long -> promote source to "last good"
MAX_BOOT_CRASHES = int(_envf("UAA_MAX_BOOT_CRASHES", 3))  # consecutive fast crashes -> roll source back
FAST_CRASH_S = _envf("UAA_FAST_CRASH_S", 30)        # crash sooner than this counts toward rollback
BACKOFF_BASE_S = _envf("UAA_BACKOFF_BASE_S", 5)
BACKOFF_MAX_S = _envf("UAA_BACKOFF_MAX_S", 300)
COOLDOWN_S = _envf("UAA_COOLDOWN_S", 10)            # pause after a clean EXIT_DONE before rebooting

# --- inference loop tuning --------------------------------------------------
MAX_TOKENS = 8192          # output cap per turn (well under the streaming threshold)
SCREENSHOT_SETTLE_S = 0.6  # wait after an action before screenshotting the result

# Computer-use tool version + required beta header, keyed by driver model.
# computer_20251124 / computer-use-2025-11-24 for the 4.6–4.8 family + Opus 4.5;
# the older pair for Sonnet 4.5 / Haiku 4.5.
_CU_NEW = ("computer_20251124", "computer-use-2025-11-24")   # Opus 4.6-4.8, Sonnet 4.6, Opus 4.5
_CU_OLD = ("computer_20250124", "computer-use-2025-01-24")   # Sonnet 4.5, Haiku 4.5, Opus 4.0/4.1
# Substrings (in a lowercased model id) that mean the OLDER computer-use pair.
_CU_OLD_MARKERS = ("sonnet-4-5", "sonnet-4.5", "haiku-4-5", "haiku-4.5",
                   "opus-4-1", "opus-4.1", "opus-4-0", "opus-4.0",
                   "sonnet-4-0", "sonnet-4.0")

TEXT_EDITOR_TOOL = {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"}
BASH_TOOL = {"type": "bash_20250124", "name": "bash"}


def computer_use_for(model: str):
    """(computer_tool_type, beta_header) for a driver model.

    Tolerant of OpenRouter slugs (e.g. 'anthropic/claude-opus-4.8') and id styles
    ('claude-opus-4-8'). Defaults to the current pair unless the id clearly names
    an older Claude family.
    """
    m = (model or "").lower()
    if any(marker in m for marker in _CU_OLD_MARKERS):
        return _CU_OLD
    return _CU_NEW
