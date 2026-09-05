"""Operator-side controls: killswitch, heartbeat, and source snapshot/rollback.

These keep the *owner* in control of their own box — they don't constrain what
the agent pursues, they keep a self-rewriting loop from costing you the machine.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import constants as C


# --- killswitch ------------------------------------------------------------
def killswitch_active() -> bool:
    return C.KILLSWITCH_PATH.exists()


def engage_killswitch() -> None:
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    C.KILLSWITCH_PATH.touch()


def clear_killswitch() -> None:
    C.KILLSWITCH_PATH.unlink(missing_ok=True)


# --- heartbeat (agent liveness, watched by the daemon) ---------------------
def beat() -> None:
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    C.HEARTBEAT_PATH.write_text(str(time.time()))


def heartbeat_age() -> float:
    try:
        return time.time() - float(C.HEARTBEAT_PATH.read_text().strip())
    except Exception:
        return float("inf")


def reset_heartbeat() -> None:
    beat()


# --- source snapshot / rollback -------------------------------------------
# The daemon snapshots uaa/ once the agent has run stably, and restores it if a
# self-edit sends the agent into a boot-crash loop. daemon.py itself is never
# snapshotted or modified by the agent — it's the fixed point everything else
# recovers to.
def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def snapshot_source() -> None:
    """Capture the current uaa/ as the known-good baseline (used once at first boot)."""
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _copy_tree(C.PKG_DIR, C.SNAPSHOT_DIR)


def stage_source() -> None:
    """Capture uaa/ as it is at boot — the exact bytes about to run.

    Python imports are cached at load, so a mid-run self-edit doesn't change the
    code that's executing. Staging at boot means we only ever promote source that
    actually ran stably, never freshly-edited-but-unproven source left on disk.
    """
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _copy_tree(C.PKG_DIR, C.STAGED_DIR)


def _tree_compiles(d: Path) -> bool:
    """Every .py in the tree parses — so we never store source that can't even load."""
    try:
        for p in d.rglob("*.py"):
            compile(p.read_text(errors="replace"), str(p), "exec")
        return True
    except (SyntaxError, OSError, ValueError):
        return False


def promote_staged() -> bool:
    """Promote the staged (just-ran) source to the known-good snapshot — only if it compiles.

    The staged source ran stably so it should compile; the check is a guard against
    ever recording a broken last-good that a later rollback would restore into a brick.
    """
    if not (C.STAGED_DIR.exists() and (C.STAGED_DIR / "runtime.py").exists()):
        return False
    if not _tree_compiles(C.STAGED_DIR):
        return False
    _copy_tree(C.STAGED_DIR, C.SNAPSHOT_DIR)
    return True


def has_snapshot() -> bool:
    return C.SNAPSHOT_DIR.exists() and (C.SNAPSHOT_DIR / "runtime.py").exists()


def restore_source() -> bool:
    """Restore uaa/ from the last-good snapshot. Returns True if a restore happened.

    Deliberately restores ONLY code (uaa/). config.json (memory_context,
    system_prompt) and mcp/ (self-authored tools) are the organism's accumulated
    state, not the thing that crashed — wiping them on every code rollback would
    erase the agent's memory and tools, which is worse than the bug it recovers
    from. The operator rails the agent must not be able to subvert (the
    killswitch) live under state/, which the agent can't write to directly; those
    survive rollback intentionally.
    """
    if not has_snapshot():
        return False
    _copy_tree(C.SNAPSHOT_DIR, C.PKG_DIR)
    return True
