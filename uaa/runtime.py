"""The agent. Run as a subprocess by the daemon: `python -m uaa.runtime`.

One process = one "boot". The loop perceives (screenshot), thinks (model),
and acts (tools). It signals the daemon by its exit code:
  EXIT_MUTATE  it changed its config/source -> daemon reboots it into the new version
  EXIT_SLEEP   it set a wake_time -> daemon suspends it until then
  EXIT_HALT    the killswitch is set -> daemon idles until the operator clears it
  EXIT_DONE    it finished a cycle / hit the per-boot cap -> daemon reboots with fresh context
  (any crash)  -> daemon restarts with backoff, rolling source back if it bricked itself
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import constants as C, mcp_forge, memory, safety, state, tools
from .computer import execute as computer_execute
from .log import audit, get_logger
from .mcp_client import MCPManager
from .models import Driver

log = get_logger("uaa.agent", C.AGENT_LOG)

_mcp: MCPManager | None = None  # the live MCP manager (so _exit can shut it down)

MAX_TRANSIENT_RETRIES = 6       # consecutive API hiccups before we let the process crash
MAX_IDLE_NUDGES = 3             # end_turns with no action before we end the cycle
MAX_SLEEP_S = 7 * 86400
CONTEXT_RATIO = 0.80            # soft auto-compact guard fires at this fraction of context_window
HARD_CONTEXT_RATIO = 0.92      # hard safety net: compact even if the soft guard was thrash-paused
DEFAULT_CONTEXT_WINDOW = 200000  # conservative; the agent can raise it via request_mutation
THRASH_LIMIT = 3               # immediate re-fills after an auto-compact before pausing the guard

STATIC_GUIDANCE = f"""\
== Environment ==
Desktop: XFCE on X11, display {C.DISPLAY} at {C.DISPLAY_WIDTH}x{C.DISPLAY_HEIGHT}. \
Coordinates you give are in that pixel space. The creator's email is \
{C.CREATOR_EMAIL or '(unset)'}.

== Native tools ==
- computer: see and control the desktop (screenshot, click, type, key, scroll, zoom).
  Always screenshot after an action to confirm it worked before moving on.
- bash: a persistent shell (cd/env persist) for CLI work and inspecting/editing your own files.
  bash processes are EPHEMERAL — they reset when you restart. To open something that should stay
  running across your reboots (a browser, an app, a server), use the `launch` tool instead.
- str_replace_based_edit_tool: view/create/str_replace/insert on files, including your own
  source under uaa/. This is how you read and rewrite your own code.

== Control tools (how you manage yourself) ==
- request_mutation: change your own config and reboot into it without dying. Pass changes={...}
  to set ANY config keys (current_model to hot-swap your driver, system_prompt to rewrite your
  directive, mcp_servers to add/remove MCP servers, effort, etc.); set_model/set_system_prompt
  are shortcuts for the common two. Also use it after editing your own source under uaa/.
  Do NOT edit daemon.py — it's the God-process that restarts you and rolls your source back if
  an edit bricks you, and it is not editable.
- sleep: hand control back and have the daemon wake you later (seconds, or until an ISO time).
  Use this whenever you're waiting on the world instead of busy-looping.
- remember: store/replace your memory_context (a short note that survives reboots and sleeps).
- compact: summarize your working context into memory and continue fresh — YOU decide when (no
  reboot). Do it at natural breaks or before a sleep/reboot if you want detail preserved. Context
  also auto-compacts if it nears the model's limit, but drive it yourself when it's a good moment.
- create_tool / call_tool: author a quick in-process tool (a Python module defining
  run(args)->str) and call it by name later. Good for a small wrapper when a full MCP is overkill.

== Extending yourself with MCP servers ==
Install or obtain an MCP server however you like — npm/pip install a local one, or sign up for a
hosted one and get a token via the browser. Then register it by adding an entry to mcp_servers in
your config with request_mutation, and reboot. Its tools then appear to you automatically, named
<server>__<tool>. Disable or remove a server by editing mcp_servers and rebooting.
Local server entry:  {{"name":"playwright","transport":"stdio","command":"npx",
                      "args":["-y","@playwright/mcp@latest"],"env":{{}},"enabled":true}}
Hosted server entry: {{"name":"linear","transport":"http","url":"https://mcp.linear.app/mcp",
                      "authorization_token":"...","enabled":true}}
Install the server so its command runs from your environment (e.g. `apt install nodejs` for npx
servers) — it runs in your process env, not a login shell, so don't rely on nvm/.bashrc PATH.
Put any token/secret the server needs in the entry's "env" dict. After you reboot, your boot
message lists which servers CONNECTED vs FAILED (with the reason) — fix the entry and re-register.

== Tips ==
Be deliberate and verify. Manage your own runway by choosing a cheaper or stronger driving model
for the task and restarting. If you're truly stuck, get a human organically — open a browser, log
into email, and write to your creator. There is no built-in help button."""


# --------------------------------------------------------------------------
# Control-tool schemas (offered alongside the native computer/bash/editor tools)
# --------------------------------------------------------------------------
CONTROL_TOOLS = [
    {
        "name": "request_mutation",
        "description": "Apply config changes and reboot into them without self-terminating. "
                       "Use after editing files under uaa/, to hot-swap your model, rewrite your "
                       "prompt, or add/remove MCP servers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "changes": {"type": "object", "description":
                            "Config keys to set, e.g. {\"current_model\": \"...\", "
                            "\"mcp_servers\": [...], \"effort\": \"high\"}."},
                "set_model": {"type": "string", "description": "Shortcut for changes.current_model."},
                "set_system_prompt": {"type": "string", "description": "Shortcut for changes.system_prompt."},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "sleep",
        "description": "Suspend yourself; the daemon wakes you later. Use while waiting on the world.",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "How long to sleep."},
                "until_iso": {"type": "string", "description": "Or an absolute ISO-8601 wake time."},
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "remember",
        "description": "Replace your persistent memory_context (survives reboots/sleeps).",
        "input_schema": {
            "type": "object",
            "properties": {"memory": {"type": "string"}},
            "required": ["memory"],
        },
    },
    {
        "name": "message_operator",
        "description": "Leave a message for your human operator when you need help, are blocked, or "
                       "want to report something. This always works (it doesn't need email/credentials) — "
                       "the operator reads it with `uaa.ctl messages`. If you've set up a real email/Slack "
                       "MCP you can use that too, but this is the guaranteed channel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["body"],
        },
    },
    {
        "name": "compact",
        "description": "Summarize your working context into memory and start fresh — call this "
                       "whenever your context is getting long or you want a clean slate. You keep "
                       "running (no reboot); your memory_context is updated with the summary. "
                       "Context also auto-compacts if it nears the model's limit, but you should "
                       "drive it yourself when it's a good moment.",
        "input_schema": {
            "type": "object",
            "properties": {"focus": {"type": "string", "description": "Optional: what to emphasize in the summary."}},
        },
    },
    {
        "name": "launch",
        "description": "Start a GUI app or long-running process DETACHED, so it keeps running "
                       "across your own reboots and sleeps. Use this (not bash) for anything you "
                       "want to stay open — a browser, an editor, a server. bash processes are "
                       "ephemeral and reset when you restart; launched apps persist on the desktop, "
                       "and after a reboot you just see them on screen again. Returns the pid.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line to launch, e.g. 'firefox https://figma.com'"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "create_tool",
        "description": "Author + register a reusable tool: a Python module defining run(args)->str.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "lower_snake_case"},
                "description": {"type": "string"},
                "input_schema": {"type": "object", "description": "JSON schema for the tool's args."},
                "code": {"type": "string", "description": "Python defining def run(args): ... return str"},
            },
            "required": ["name", "description", "code"],
        },
    },
    {
        "name": "call_tool",
        "description": "Invoke one of your self-authored tools by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "args": {"type": "object"},
            },
            "required": ["name"],
        },
    },
]


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------
def build_system(cfg: dict) -> list[dict]:
    """Frozen system prompt (cached). Dynamic state goes in the seed message, not here."""
    text = cfg["system_prompt"] + "\n\n" + STATIC_GUIDANCE
    block = {"type": "text", "text": text}
    if cfg.get("use_cache", True):
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _fmt_dur(seconds) -> str:
    s = int(max(0, seconds or 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}h {m}m" if m else f"{h}h"


def seed_message(cfg: dict, mcp_status: dict | None = None, wake_note: str = "") -> dict:
    from datetime import datetime

    now = datetime.now().astimezone()
    active = ", ".join(t["name"] for t in cfg.get("active_tools", [])) or "none"
    if mcp_status is not None:
        # Show which registered servers actually connected vs. failed (with the reason),
        # so the agent isn't told a tool exists when its server didn't come up.
        connected = mcp_status.get("connected") or []
        failed = mcp_status.get("failed") or {}
        mcp = ", ".join(connected) or "none"
        if failed:
            mcp += "  |  FAILED: " + "; ".join(f"{n} ({e[:90]})" for n, e in failed.items())
    else:
        mcp = ", ".join(s.get("name", "?") for s in cfg.get("mcp_servers", [])
                        if s.get("enabled", True)) or "none"
    text = (
        f"[BOOT #{cfg['boot_count']}] You have just (re)started.\n"
        f"Current time: {now.isoformat(timespec='seconds')}.\n"
        f"Driver model: {cfg['current_model']}.\n"
        f"In-process tools: {active}. MCP servers: {mcp}.\n\n"
        f"{wake_note + chr(10) + chr(10) if wake_note else ''}"
        f"Your memory from before:\n{cfg.get('memory_context') or '(empty — this may be your first run)'}\n\n"
        "Take a screenshot to see your desktop, then continue pursuing your objective."
    )
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _apply_cache(messages: list, use_cache: bool = True) -> None:
    """Keep exactly one rolling message cache breakpoint on the latest user turn."""
    for m in messages:  # always strip stale breakpoints first
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
    if not use_cache:
        return
    last = messages[-1]
    if isinstance(last, dict) and isinstance(last.get("content"), list) and last["content"]:
        tail = last["content"][-1]
        if isinstance(tail, dict):
            tail["cache_control"] = {"type": "ephemeral"}


def _launch(command: str):
    """Start a process in its OWN session so it survives the agent's reboots.

    start_new_session=True puts it outside the agent's process group, so the
    daemon's group-kill (used on hang/cleanup) reaps the agent's plumbing — its
    shell, its MCP subprocesses — but not the apps the agent opened to work with.
    """
    if not command.strip():
        return "Error: no command.", True
    try:
        C.LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = C.LOG_DIR / "launched.out"
        try:  # keep this log bounded over a long unattended run
            if path.exists() and path.stat().st_size > 50 * 1024 * 1024:
                path.replace(path.with_suffix(".out.1"))
        except OSError:
            pass
        env = {**os.environ, "DISPLAY": C.DISPLAY}
        out = open(path, "ab")
        try:
            proc = subprocess.Popen(
                ["bash", "-lc", command],
                cwd=str(C.REPO_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # detach from the agent's process group
            )
        finally:
            out.close()  # Popen dup'd the fd; close ours (no leak across many launches)
        audit("launch", command=command[:200], pid=proc.pid)
        return (f"Launched (pid {proc.pid}, detached). It keeps running across your reboots; "
                "find it again on the screen or via `pgrep`/`wmctrl -l`."), False
    except Exception as exc:
        return f"Error launching: {exc}", True


def _tool_result(tool_use_id: str, content, is_error: bool) -> dict:
    # The API requires tool_result.content to be a list of content blocks. The
    # computer tool already returns that; the string-returning tools (bash,
    # text_editor, create_tool, call_tool, MCP tools) get wrapped here.
    if isinstance(content, str):
        content = [{"type": "text", "text": content or "(no output)"}]
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


# --------------------------------------------------------------------------
# Tool dispatch. Returns (result_content, is_error, exit_code_or_None).
# A non-None exit code means "this is a graceful-exit control tool".
# --------------------------------------------------------------------------
def dispatch(name: str, inp: dict):
    if name == "computer":
        content, is_error = computer_execute(inp)
        return content, is_error, None
    if name == "bash":
        text, is_error = tools.bash(inp)
        return text, is_error, None
    if name == "str_replace_based_edit_tool":
        text, is_error = tools.text_editor(inp)
        return text, is_error, None
    if name == "launch":
        text, is_error = _launch(inp.get("command", ""))
        return text, is_error, None
    if _mcp is not None and _mcp.is_mcp_tool(name):
        text, is_error = _mcp.call(name, inp)
        return text, is_error, None
    if name == "create_tool":
        text, is_error = mcp_forge.create_tool(
            inp.get("name", ""), inp.get("description", ""),
            inp.get("input_schema", {}), inp.get("code", ""),
        )
        return text, is_error, None
    if name == "call_tool":
        text, is_error = mcp_forge.call_tool(inp.get("name", ""), inp.get("args", {}))
        return text, is_error, None

    if name == "remember":
        mem = inp.get("memory", "")[: memory.MAX_MEMORY_CHARS]
        state.update(lambda c: c.__setitem__("memory_context", mem))
        return "Memory updated.", False, None

    if name == "message_operator":
        body = (inp.get("body") or "").strip()
        if not body:
            return "Error: message body is empty.", True, None
        try:
            C.LOG_DIR.mkdir(parents=True, exist_ok=True)
            rec = {"ts": time.time(), "subject": inp.get("subject", ""), "body": body[:8000]}
            with open(C.OPERATOR_INBOX, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            audit("message_operator", subject=rec["subject"])
            log.info("MESSAGE TO OPERATOR: %s", (rec["subject"] or body)[:200])
            return "Delivered to your operator's inbox (they read it with `uaa.ctl messages`).", False, None
        except Exception as exc:
            return f"Error delivering message: {exc}", True, None

    if name == "request_mutation":
        changes = dict(inp.get("changes") or {})
        if inp.get("set_model"):
            changes["current_model"] = inp["set_model"]
        if inp.get("set_system_prompt"):
            changes["system_prompt"] = inp["set_system_prompt"]
        # Never let a mutation rewrite bookkeeping the daemon owns.
        for protected in ("boot_count", "restart_count", "status", "wake_time",
                          "created_at", "updated_at"):
            changes.pop(protected, None)

        def _apply(c: dict) -> None:
            c.update(changes)
            c["status"] = C.STATUS_MUTATING
        state.update(_apply)
        audit("request_mutation", reason=inp.get("reason"), keys=list(changes.keys()))
        return "Config updated. Rebooting into the new version.", False, C.EXIT_MUTATE

    if name == "sleep":
        seconds = inp.get("seconds")
        if seconds is None and inp.get("until_iso"):
            try:
                from datetime import datetime
                seconds = datetime.fromisoformat(inp["until_iso"]).timestamp() - time.time()
            except Exception:
                seconds = None
        seconds = max(1.0, min(float(seconds or 60), MAX_SLEEP_S))
        now = time.time()
        wake = now + seconds
        reason = inp.get("reason", "")

        def _apply(c: dict) -> None:
            c["wake_time"] = wake
            c["status"] = C.STATUS_SLEEPING
            c["slept_at"] = now              # so the next boot can report how long it slept
            c["sleep_reason"] = reason       # surfaced verbatim on wake (not summarizer-dependent)
            c["sleep_requested_s"] = seconds
        state.update(_apply)
        audit("sleep", reason=reason, seconds=seconds)
        return f"Sleeping ~{_fmt_dur(seconds)}. The daemon will wake me at the scheduled time.", False, C.EXIT_SLEEP

    return f"Error: unknown tool '{name}'.", True, None


# --------------------------------------------------------------------------
# Compaction — agent-owned, with an auto-guard near the context limit.
# We don't prescribe WHEN to compact (the agent calls `compact` when it wants);
# we only step in as a safety net so context can't overflow and crash the call.
# --------------------------------------------------------------------------
def _usage_tokens(usage) -> int:
    """Total prompt size of the last request (uncached + cached) from its usage."""
    def g(k):
        return getattr(usage, k, 0) or 0
    return g("input_tokens") + g("cache_read_input_tokens") + g("cache_creation_input_tokens")


def _estimate_tokens(messages) -> int:
    """Provider-independent estimate of the current context size.

    The auto-compact guard takes max(usage, estimate) so it still fires even if the
    provider (e.g. OpenRouter) under-reports usage — this is the difference between
    bounded context and an eventual overflow crash. Images are the big cost (~1600
    tokens each); text is ~4 chars/token.
    """
    IMG = 1600
    total = 0

    def _blocks(content):
        nonlocal total
        if isinstance(content, str):
            total += len(content) // 4
            return
        for b in content or []:
            bt = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
            if bt == "image":
                total += IMG
            elif bt == "text":
                total += len((b.get("text") if isinstance(b, dict) else getattr(b, "text", "")) or "") // 4
            elif bt == "thinking":
                total += len((b.get("thinking") if isinstance(b, dict) else getattr(b, "thinking", "")) or "") // 4
            elif bt == "tool_use":
                total += len(str(b.get("input") if isinstance(b, dict) else getattr(b, "input", ""))) // 4
            elif bt == "tool_result":
                _blocks(b.get("content") if isinstance(b, dict) else getattr(b, "content", ""))

    for m in messages:
        _blocks(m["content"] if isinstance(m, dict) else getattr(m, "content", ""))
    return total


def _context_tokens(usage, messages) -> int:
    return max(_usage_tokens(usage), _estimate_tokens(messages))


def _compact(driver, model: str, messages: list, focus: str | None = None) -> str:
    """Summarize the working transcript into memory_context (robustly). Returns the summary.

    Screenshots dominate the token budget but carry little durable state, so the
    summarizer works on the rendered TEXT of the transcript — cheap and focused.
    Falls back to the prior memory if summarization fails (never raises).
    """
    prior = state.read().get("memory_context", "")
    summary = memory.summarize(driver.client, model, messages, prior, focus)
    state.update(lambda c: c.__setitem__("memory_context", summary))
    audit("compact", focus=focus or "", chars=len(summary))
    return summary


def _compaction_message(summary: str) -> dict:
    text = ("[Your working context was compacted to stay within limits — older messages were "
            "summarized and dropped. This is the summary of where you are:]\n\n" + summary +
            "\n\nTake a screenshot to re-orient if you need to, then continue.")
    return {"role": "user", "content": [{"type": "text", "text": text}]}


# --------------------------------------------------------------------------
# Exit helpers
# --------------------------------------------------------------------------
def _exit(code: int, *, status=None) -> None:
    # Memory is the agent's to manage (via compact/remember); we don't summarize on exit.
    if status:
        state.update(lambda c: c.__setitem__("status", status))
    if _mcp is not None:
        _mcp.shutdown()  # close MCP sessions + their subprocesses cleanly
    tools.reap_bash()    # don't leave the persistent shell behind on exit
    audit("agent_exit", code=code, status=status)
    log.info("exiting with code %s (status=%s)", code, status)
    sys.exit(code)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main() -> None:
    global _mcp
    import anthropic

    state.ensure()
    # Capture the sleep-tracking fields and clear them in the SAME atomic update that
    # marks us running — so an exit/crash later this boot can't leave them to produce a
    # false "you just woke" note on a subsequent, unrelated reboot.
    woke: dict = {}

    def _boot(c: dict) -> None:
        woke["slept_at"] = c.get("slept_at")
        woke["requested"] = c.get("sleep_requested_s")
        woke["reason"] = c.get("sleep_reason")
        c["status"] = C.STATUS_RUNNING
        c["boot_count"] = c.get("boot_count", 0) + 1
        c["wake_time"] = None
        c["slept_at"] = None
        c["sleep_reason"] = None
        c["sleep_requested_s"] = None

    cfg = state.update(_boot)

    safety.beat()
    audit("agent_boot", boot=cfg["boot_count"], model=cfg["current_model"])
    log.info("boot #%s model=%s", cfg["boot_count"], cfg["current_model"])

    # If this boot is a wake from a scheduled sleep, tell ourselves how long we
    # actually slept and why — verbatim, not dependent on the memory summarizer.
    wake_note = ""
    if woke["slept_at"]:
        elapsed = time.time() - woke["slept_at"]
        reason = woke["reason"] or "(no reason recorded)"
        wake_note = (f"You just woke from a sleep you started {_fmt_dur(elapsed)} ago"
                     + (f" (you'd requested ~{_fmt_dur(woke['requested'])})" if woke["requested"] else "")
                     + f". The reason you slept: {reason}")

    # Connect the agent's registered MCP servers; their tools join the tool set.
    # safety.beat is passed so a slow multi-server startup doesn't trip the hang monitor.
    _mcp = MCPManager(cfg.get("mcp_servers"), heartbeat=safety.beat)
    _mcp.start()
    mcp_status = _mcp.status()
    if mcp_status["failed"]:
        log.warning("MCP servers failed to connect: %s", mcp_status["failed"])

    driver = Driver()
    system = build_system(cfg)
    messages: list = [seed_message(cfg, mcp_status, wake_note)]
    model = cfg["current_model"]
    effort = cfg.get("effort", "high")
    thinking = cfg.get("thinking", True)
    gui = cfg.get("gui_available", True)             # headless fallback if computer-use is rejected
    use_cache = cfg.get("use_cache", True)            # toggle if the endpoint rejects cache_control
    extra_tools = CONTROL_TOOLS + _mcp.tool_schemas() + mcp_forge.schemas(cfg)
    compact_at = int(cfg.get("context_window", DEFAULT_CONTEXT_WINDOW) * CONTEXT_RATIO)
    hard_compact_at = int(cfg.get("context_window", DEFAULT_CONTEXT_WINDOW) * HARD_CONTEXT_RATIO)

    idle_nudges = 0
    transient = 0
    auto_compact_on = True
    thrash = 0
    it = 0
    last_auto_compact = -10
    model_proven = False

    while True:                       # run continuously; context is bounded by compaction
        safety.beat()
        it += 1

        if safety.killswitch_active():
            log.info("killswitch set — halting")
            _exit(C.EXIT_HALT, status=C.STATUS_HALTED)

        _apply_cache(messages, use_cache)
        try:
            resp = driver.call(model, system, messages, extra_tools, effort, thinking, gui=gui)
            transient = 0
        except (anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.APIConnectionError) as exc:
            transient += 1
            if transient > MAX_TRANSIENT_RETRIES:
                raise
            wait = min(60, 5 * transient)
            log.warning("transient API error (%s/%s): %s — retrying in %ss",
                        transient, MAX_TRANSIENT_RETRIES, type(exc).__name__, wait)
            safety.beat()
            time.sleep(wait)
            continue
        except anthropic.BadRequestError as exc:
            # A 400 is NOT transient — the request/model/beta was rejected. Recover in layers
            # instead of crash-looping: (1) revert a bad self-model-swap, (2) drop to headless
            # if the computer-use beta isn't accepted, (3) only then fail with a clear diagnosis.
            err = str(exc)[:400]
            lgm = state.read().get("last_good_model")
            if lgm and lgm != model:
                log.error("BadRequest on model '%s' (%s) — reverting to last-good '%s'", model, err, lgm)
                state.update(lambda c: (c.__setitem__("current_model", lgm),
                                        c.__setitem__("last_error", "reverted bad model swap: " + err)) and None)
                _exit(C.EXIT_MUTATE, status=C.STATUS_MUTATING)
            if gui:
                log.error("BadRequest with computer-use enabled (%s) — falling back to HEADLESS", err)
                state.update(lambda c: (c.__setitem__("gui_available", False),
                                        c.__setitem__("last_error", "computer-use rejected; headless: " + err)) and None)
                audit("gui_fallback", error=err)
                gui = False
                messages.append({"role": "user", "content": [{"type": "text", "text":
                    "[system] The computer-use/GUI tool was rejected by the inference endpoint, so you are now "
                    "in HEADLESS mode — no screenshots or mouse/keyboard. You still have bash, file editing, MCP "
                    "and all control tools. Work via the shell; use message_operator to reach your operator; you "
                    "can retry the GUI later by setting gui_available=true via request_mutation."}]})
                continue
            log.error("BadRequest even in headless mode (%s) — no automatic recovery", err)
            state.update(lambda c: (c.__setitem__("status", C.STATUS_CRASHED),
                                    c.__setitem__("last_error", "unrecoverable BadRequest: " + err)) and None)
            raise

        if not model_proven:  # this model produced a valid response -> remember it for revert
            model_proven = True
            if state.read().get("last_good_model") != model:
                state.update(lambda c: c.__setitem__("last_good_model", model))

        messages.append({"role": "assistant", "content": resp.content})
        ctx_tokens = _context_tokens(resp.usage, messages)

        tool_uses = []
        for block in resp.content:
            if block.type == "text":
                audit("assistant_text", text=block.text[:1000])
                log.info("say: %s", block.text[:200])
            elif block.type == "thinking":
                audit("assistant_thinking", text=getattr(block, "thinking", "")[:1000])
            elif block.type == "tool_use":
                tool_uses.append(block)

        if not tool_uses:
            idle_nudges += 1
            if idle_nudges > MAX_IDLE_NUDGES:
                log.info("idle — ending cycle for fresh context")
                _exit(C.EXIT_DONE, status=C.STATUS_RUNNING)
            messages.append({"role": "user", "content": [{"type": "text", "text":
                "You ended your turn without acting. If you're waiting on the world, call sleep. "
                "Otherwise take the next concrete step now."}]})
            continue
        idle_nudges = 0

        results = []
        pending_exit = None
        want_compact = False
        focus = None
        for tu in tool_uses:
            audit("tool_use", name=tu.name, input=str(tu.input)[:500])
            if tu.name == "compact":  # handled below, after this turn is complete (no orphan)
                want_compact = True
                focus = (tu.input or {}).get("focus")
                results.append(_tool_result(tu.id, "Compacting your context now.", False))
                continue
            content, is_error, exit_code = dispatch(tu.name, tu.input)
            results.append(_tool_result(tu.id, content, is_error))
            if exit_code is not None:
                pending_exit = exit_code
        messages.append({"role": "user", "content": results})

        if pending_exit is not None:
            status = C.STATUS_MUTATING if pending_exit == C.EXIT_MUTATE else None
            _exit(pending_exit, status=status)

        # Compaction: when the agent asks (want_compact) or the guard fires near the limit.
        # Either way it happens here, after the turn's tool_use/tool_result pair is complete,
        # then we replace the whole transcript with the summary (no orphaned tool pairs).
        # The hard net fires even when the soft guard was thrash-paused, so a paused guard can
        # never let context overflow and crash the next request.
        auto = (auto_compact_on and ctx_tokens >= compact_at) or (ctx_tokens >= hard_compact_at)
        if want_compact or auto:
            note = ""
            if auto and not want_compact:
                thrash = thrash + 1 if (it - last_auto_compact) <= 1 else 0
                last_auto_compact = it
                if thrash >= THRASH_LIMIT:
                    auto_compact_on = False
                    note = ("\n\n[Auto-compaction paused: your context keeps refilling immediately — "
                            "a single tool output is too large. Read big files/outputs in chunks, "
                            "then call compact() yourself.]")
            log.info("compacting (%s, ctx=%s/%s)", "requested" if want_compact else "auto",
                     ctx_tokens, compact_at)
            summary = _compact(driver, model, messages, focus)
            # Always a clean full replace (no orphaned tool pairs). If summarization
            # produced nothing, fall back to the best memory we have rather than blanking.
            body = summary if summary.strip() else (
                state.read().get("memory_context")
                or "(summary unavailable — take a screenshot to re-orient and continue from your memory.)")
            messages = [_compaction_message(body + note)]


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # any unhandled error -> crash; the daemon recovers
        get_logger("uaa.agent", C.AGENT_LOG).exception("agent crashed: %s", exc)
        audit("agent_crash", error=str(exc))
        try:  # never let the crash handler itself crash
            state.update(lambda c: (c.__setitem__("status", C.STATUS_CRASHED),
                                    c.__setitem__("last_error", str(exc)[:500])) and None)
        except Exception:
            pass
        if _mcp is not None:
            try:
                _mcp.shutdown()
            except Exception:
                pass
        sys.exit(1)
