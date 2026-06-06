"""Shared logging + append-only audit trail.

The audit log is one JSON object per line — every action the agent takes and
every lifecycle event the daemon handles. It's the thing that makes a
self-rewriting autonomous agent debuggable after the fact.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import constants as C


def get_logger(name: str, path) -> logging.Logger:
    C.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler(path)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def audit(event: str, **fields: Any) -> None:
    """Append one structured record to the audit log. Never raises."""
    try:
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "event": event, **fields}
        with open(C.AUDIT_LOG, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass
