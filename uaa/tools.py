"""Client-side handlers for the bash and text-editor beta tools.

- BashSession: a persistent shell so `cd`, env, and background state survive
  across the agent's commands (human-analogous, not one-shot subprocesses).
- text_editor: implements the str_replace_based_edit_tool commands (view /
  create / str_replace / insert) the agent uses to read and rewrite files —
  including its own source under uaa/.
"""
from __future__ import annotations

import os
import re
import secrets
import select
import subprocess
import time
from pathlib import Path

from . import constants as C

_ENV = {**os.environ, "DISPLAY": C.DISPLAY}
_MAX_OUTPUT = 16000  # truncate long tool output so it doesn't blow the context window


def _truncate(s: str) -> str:
    if len(s) <= _MAX_OUTPUT:
        return s
    head = s[: _MAX_OUTPUT // 2]
    tail = s[-_MAX_OUTPUT // 2:]
    return f"{head}\n... [truncated {len(s) - _MAX_OUTPUT} chars] ...\n{tail}"


# --------------------------------------------------------------------------
# Persistent bash session
# --------------------------------------------------------------------------
class BashSession:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        # Per-session random sentinel so it can't collide with legitimate command
        # output (a `cat` of a file mentioning the marker won't end the read early).
        self._sentinel = f"__UAA_BASH_DONE_{secrets.token_hex(8)}__"
        self._pattern = re.compile(self._sentinel.encode() + rb"(\d+)\n")
        self._start()

    def _start(self) -> None:
        self.proc = subprocess.Popen(
            ["/bin/bash"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=str(C.REPO_ROOT),
            env=_ENV,
        )

    def _reap(self) -> None:
        """Kill (if needed) and wait() the current process so it can't become a zombie."""
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass

    def restart(self) -> None:
        self._reap()
        self._start()

    def run(self, command: str, timeout: float = 120.0) -> tuple[str, bool]:
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        assert self.proc and self.proc.stdin and self.proc.stdout

        payload = f"{command}\nprintf '\\n{self._sentinel}%s\\n' \"$?\"\n"
        try:
            self.proc.stdin.write(payload.encode())
            self.proc.stdin.flush()
        except BrokenPipeError:
            self.restart()
            return "Error: shell pipe broke; restarted. Re-run the command.", True

        buf = b""
        fd = self.proc.stdout.fileno()
        deadline = time.time() + timeout
        died = False
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self.restart()
                return _truncate(buf.decode(errors="replace")) + \
                    f"\n... [timed out after {int(timeout)}s; shell restarted]", True
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            if fd in ready:
                chunk = os.read(fd, 65536)
                if not chunk:  # shell exited (e.g. the command ran `exit`)
                    died = True
                    self.restart()  # reap the dead shell, then start a fresh one
                    break
                buf += chunk
                if self._pattern.search(buf):  # full sentinel, not just the prefix
                    break

        m = self._pattern.search(buf)
        if m:
            exit_code = int(m.group(1))
            output = buf[: m.start()].decode(errors="replace")
        elif died:
            out = _truncate(buf.decode(errors="replace").rstrip("\n"))
            return (out + "\n[the shell exited; session restarted]").strip(), True
        else:
            exit_code, output = 0, buf.decode(errors="replace")
        out = _truncate(output.rstrip("\n"))
        if exit_code != 0:
            out = (out + f"\n[exit code {exit_code}]").strip()
        return out, exit_code != 0


_bash_singleton: BashSession | None = None


def reap_bash() -> None:
    """Kill + reap the persistent shell (called on agent exit so it isn't left behind)."""
    global _bash_singleton
    if _bash_singleton is not None:
        try:
            _bash_singleton._reap()
        except Exception:
            pass
        _bash_singleton = None


def bash(inp: dict) -> tuple[str, bool]:
    """Handle a bash tool call. Supports {'restart': true} and {'command': '...'}."""
    global _bash_singleton
    if _bash_singleton is None:
        _bash_singleton = BashSession()
    if inp.get("restart"):
        _bash_singleton.restart()
        return "bash session restarted", False
    command = inp.get("command")
    if not command:
        return "Error: no command provided.", True
    return _bash_singleton.run(command)


# --------------------------------------------------------------------------
# Text editor (str_replace_based_edit_tool)
# --------------------------------------------------------------------------
def _number(text: str, start: int = 1) -> str:
    lines = text.splitlines()
    width = len(str(start + len(lines) - 1))
    return "\n".join(f"{str(i + start).rjust(width)}\t{ln}" for i, ln in enumerate(lines))


_PROTECTED_DAEMON = (C.REPO_ROOT / "daemon.py").resolve()


def _write_protected(path: Path) -> str | None:
    """Return a refusal message if writing `path` would corrupt the runtime plumbing.

    The agent may rewrite its own body (uaa/) and anything else freely. But state/
    holds lock-coordinated config + the killswitch + the rollback snapshots — a raw
    write there races the daemon's reads and can tear the file — so config changes
    go through request_mutation (the locked path) instead. daemon.py is the
    God-process that recovers the agent if it bricks itself, so it's off-limits.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if resolved == _PROTECTED_DAEMON:
        return ("Error: daemon.py is the God-process and is not editable. Change your "
                "own behavior under uaa/ instead, then call request_mutation.")
    if C.STATE_DIR.resolve() in resolved.parents or resolved == C.STATE_DIR.resolve():
        return ("Error: state/ is operator-managed. Use the control tools (request_mutation, "
                "remember, sleep) to change config — don't edit it directly.")
    return None


def text_editor(inp: dict) -> tuple[str, bool]:
    command = inp.get("command")
    raw_path = inp.get("path", "")
    if not raw_path:
        return "Error: 'path' is required.", True
    path = Path(raw_path)
    if command in ("create", "str_replace", "insert"):
        guard = _write_protected(path)
        if guard:
            return guard, True

    try:
        if command == "view":
            if path.is_dir():
                entries = sorted(os.listdir(path))
                return f"Directory {path}:\n" + "\n".join(entries), False
            if not path.exists():
                return f"Error: {path} does not exist.", True
            content = path.read_text(errors="replace")
            view_range = inp.get("view_range")
            if view_range and len(view_range) == 2:
                lines = content.splitlines()
                s, e = view_range
                e = len(lines) if e == -1 else e
                s = max(1, s)
                selected = "\n".join(lines[s - 1:e])
                return _truncate(_number(selected, start=s)), False
            return _truncate(_number(content)), False

        if command == "create":
            if path.exists():
                return f"Error: {path} already exists. Use str_replace/insert to edit it.", True
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inp.get("file_text", ""))
            return f"Created {path}.", False

        if command == "str_replace":
            if not path.exists():
                return f"Error: {path} does not exist.", True
            content = path.read_text(errors="replace")
            old = inp.get("old_str", "")
            new = inp.get("new_str", "")
            count = content.count(old)
            if count == 0:
                return "Error: old_str not found. It must match the file exactly.", True
            if count > 1:
                return f"Error: old_str matched {count} times; make it unique.", True
            path.write_text(content.replace(old, new, 1))
            return f"Edited {path}.", False

        if command == "insert":
            if not path.exists():
                return f"Error: {path} does not exist.", True
            lines = path.read_text(errors="replace").splitlines(keepends=True)
            line_no = int(inp.get("insert_line", 0))
            if line_no < 0 or line_no > len(lines):
                return f"Error: insert_line {line_no} out of range (0..{len(lines)}).", True
            new = inp.get("new_str", "")
            if not new.endswith("\n"):
                new += "\n"
            lines.insert(line_no, new)
            path.write_text("".join(lines))
            return f"Inserted into {path} after line {line_no}.", False

        return f"Error: unsupported text_editor command '{command}'.", True

    except Exception as exc:  # filesystem errors, permissions, etc.
        return f"Error: {exc}", True
