"""Operator console for the organism.  `python -m uaa.ctl <command>`

  status            one-screen health: daemon/agent liveness, model, status, errors
  stop              engage the killswitch (agent finishes cleanly and halts)
  resume            clear the killswitch
  logs [n]          tail the agent log (default 40 lines)
  audit [n]         tail the audit trail (default 40 events, pretty)
  messages [n]      read messages the agent left for you (via its message_operator tool)
  config            print the full config.json
  mem               print the agent's current memory_context

Read-only except stop/resume, which just toggle the killswitch file. Honors
UAA_HOME like the rest of the system.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import constants as C, safety, state


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _daemon_pid() -> int | None:
    try:
        pid = int(C.DAEMON_PID_PATH.read_text().strip())
        return pid if _alive(pid) else None
    except (OSError, ValueError):
        return None


def _ago(ts) -> str:
    if not ts:
        return "—"
    d = time.time() - ts
    if d < 0:
        return f"in {int(-d)}s"
    if d < 90:
        return f"{int(d)}s ago"
    if d < 5400:
        return f"{int(d / 60)}m ago"
    return f"{int(d / 3600)}h ago"


def cmd_status() -> None:
    cfg = state.read()
    pid = _daemon_pid()
    hb = safety.heartbeat_age()
    agent_live = hb < C.HANG_TIMEOUT_S
    print(f"daemon      : {'running (pid %d)' % pid if pid else 'NOT running'}")
    print(f"agent       : {'alive' if agent_live else 'idle/sleeping/down'} "
          f"(last heartbeat {int(hb)}s ago)" if hb != float('inf') else "agent       : no heartbeat yet")
    print(f"killswitch  : {'ENGAGED — halting' if safety.killswitch_active() else 'off'}")
    print(f"status      : {cfg.get('status')}")
    if not cfg.get("gui_available", True):
        print("display     : HEADLESS (computer-use was rejected by the endpoint)")
    print(f"model       : {cfg.get('current_model')}   effort={cfg.get('effort')}")
    if C.OPERATOR_INBOX.exists():
        try:
            n = sum(1 for _ in open(C.OPERATOR_INBOX))
            if n:
                print(f"messages    : {n} from the agent  (read with: uaa.ctl messages)")
        except OSError:
            pass
    print(f"boots       : {cfg.get('boot_count')}   restarts={cfg.get('restart_count')}")
    wake = cfg.get("wake_time")
    if wake:
        print(f"wake_time   : {_ago(wake)}")
    tools = ", ".join(t.get("name", "?") for t in cfg.get("active_tools", [])) or "none"
    mcp = ", ".join(s.get("name", "?") for s in cfg.get("mcp_servers", []) if s.get("enabled", True)) or "none"
    print(f"tools       : {tools}")
    print(f"mcp servers : {mcp}")
    if cfg.get("last_error"):
        print(f"last_error  : {str(cfg['last_error']).splitlines()[0][:200]}")
    mem = cfg.get("memory_context") or ""
    if mem:
        print(f"memory      : {mem[:200]}{'…' if len(mem) > 200 else ''}")


def cmd_stop() -> None:
    safety.engage_killswitch()
    print(f"killswitch engaged ({C.KILLSWITCH_PATH}). The agent will finish its turn and halt.")


def cmd_resume() -> None:
    safety.clear_killswitch()
    print("killswitch cleared. The daemon will resume the agent.")


def _tail(path, n) -> None:
    if not path.exists():
        print(f"(no {path.name} yet)")
        return
    lines = path.read_text(errors="replace").splitlines()
    for ln in lines[-n:]:
        print(ln)


def cmd_logs(n) -> None:
    _tail(C.AGENT_LOG, n)


def cmd_audit(n) -> None:
    if not C.AUDIT_LOG.exists():
        print("(no audit log yet)")
        return
    for ln in C.AUDIT_LOG.read_text(errors="replace").splitlines()[-n:]:
        try:
            e = json.loads(ln)
            ts = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
            extra = {k: v for k, v in e.items() if k not in ("ts", "event")}
            print(f"{ts}  {e.get('event'):<20} {json.dumps(extra, default=str) if extra else ''}")
        except json.JSONDecodeError:
            print(ln)


def cmd_messages(n) -> None:
    if not C.OPERATOR_INBOX.exists():
        print("(no messages from the agent yet)")
        return
    lines = C.OPERATOR_INBOX.read_text(errors="replace").splitlines()[-n:]
    for ln in lines:
        try:
            m = json.loads(ln)
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.get("ts", 0)))
            subj = m.get("subject") or ""
            print(f"── {when}{('  ·  ' + subj) if subj else ''}")
            print(m.get("body", ""))
            print()
        except json.JSONDecodeError:
            print(ln)


def cmd_config() -> None:
    print(json.dumps(state.read(), indent=2, sort_keys=True))


def cmd_mem() -> None:
    print(state.read().get("memory_context") or "(empty)")


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    arg = argv[1] if len(argv) > 1 else None
    if cmd == "status":
        cmd_status()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "resume":
        cmd_resume()
    elif cmd == "logs":
        cmd_logs(int(arg) if arg else 40)
    elif cmd == "audit":
        cmd_audit(int(arg) if arg else 40)
    elif cmd == "messages":
        cmd_messages(int(arg) if arg else 20)
    elif cmd == "config":
        cmd_config()
    elif cmd == "mem":
        cmd_mem()
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
