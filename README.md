# UAA — Unrestricted Autonomous Agent

A self-managing autonomous agent that lives in a headed Linux desktop and has
human-analogous control of the machine: it sees the screen, drives mouse and
keyboard, runs the shell, edits files — **including its own source** — and can
hot-swap its driving model, schedule its own sleep, and grow new tools, all
without a human in the loop.

This repo is the runtime/harness. The desktop (Ubuntu + XFCE + VNC) is the body;
the harness is the nervous system.

> Scope: this is the autonomy *core* — full computer use plus self-management. It
> deliberately does **not** include anything for defeating KYC, CAPTCHAs, SMS/Cloudflare,
> or other anti-fraud/identity controls. The agent operates through legitimate
> channels and within terms of service.

---

## Architecture: God-process / Organism

```
                 ┌───────────────────────────── daemon.py (God-process) ─────────────────────────────┐
                 │ indestructible supervisor — never edits or kills itself                            │
                 │  • boots the agent as an isolated subprocess (own process group)                   │
                 │  • watches the heartbeat; kills + restarts if it hangs                             │
                 │  • reacts to how the agent exits (see protocol below)                              │
                 │  • rolls the agent's source back to the last known-good snapshot if a self-edit    │
                 │    sends it into a boot-crash loop                                                 │
                 └───────────────┬───────────────────────────────────────────────────────────────────┘
                                 │ spawn / reap (exit code = intent)
                                 ▼
                 ┌───────────── python -m uaa.runtime (the agent) ──────────────┐
                 │ perceive (screenshot) → think (Claude) → act (tools)         │
                 │  native tools:  computer · bash · text_editor                │
                 │  control tools: request_mutation · sleep · remember ·        │
                 │                 create_tool · call_tool                      │
                 │  + tools from any MCP servers the agent has registered       │
                 └───────────────┬──────────────────────────────────────────────┘
                                 │ reads / writes
                                 ▼
                         state/config.json   ← the shared nervous system
```

### The daemon ⇄ agent protocol (process exit codes)

The agent signals the daemon purely through its exit code; `config.json` carries
the payload. This is what lets it rewrite itself *without self-terminating* —
the daemon is the continuity.

| Exit | Meaning | Daemon does |
|------|---------|-------------|
| `10` MUTATE | agent edited its config/source | reload config, reboot into the new version now |
| `20` SLEEP  | agent set `wake_time` | suspend until then, then reboot |
| `30` HALT   | operator killswitch is set | idle until the killswitch is cleared |
| `0` DONE    | finished a cycle / hit per-boot cap | cool down, reboot with fresh context |
| other       | crash | restart with exponential backoff; roll source back after repeated boot-crashes |

---

## `state/config.json` — the source of truth

Created from defaults on first run (see `uaa/state.py`); the agent edits it live.

| Field | Meaning |
|-------|---------|
| `current_model` | the Claude model driving the agent (must support computer use) |
| `system_prompt` | the core directive — the agent can rewrite it |
| `active_tools` | self-authored in-process tools registered via `create_tool` |
| `mcp_servers` | MCP servers the agent has installed/registered; loaded each boot |
| `status` | `running` · `sleeping` · `crashed` · `mutating` · `halted` |
| `wake_time` | epoch the daemon resumes a sleeping agent |
| `memory_context` | high-level summary carried across reboots/sleeps |
| `effort` / `thinking` | inference knobs for the driver |

The agent changes any of these by calling `request_mutation(changes={...})`, which writes the keys and reboots into them — the "change the strings, restart" loop.

---

## Capabilities → where they live

| Capability (SRD §) | Implementation |
|---|---|
| Total OS & UI control | `uaa/computer.py` (scrot/xdotool on X11) + `bash` + `text_editor` in `uaa/tools.py` |
| Self-modification | agent edits files under `uaa/` → `request_mutation` reboots into them |
| Self-management of config | `request_mutation(changes={...})` sets any config key (model, prompt, effort, MCP servers) + reboots |
| Dynamic capability — MCP servers | agent installs/auths a server, registers it in `mcp_servers`, reboots; `uaa/mcp_client.py` connects + exposes its tools |
| Dynamic capability — in-process tools | `uaa/mcp_forge.py` — `create_tool` writes/registers a module, `call_tool` runs it |
| Economic self-management | the agent picks its own driving model (`set_model` / `changes.current_model`) + restarts; no budget is imposed |
| Temporal autonomy | `sleep` tool + daemon scheduling |
| Organic communication | no help tool — the agent opens a browser and emails the creator itself |

### Inference: Claude via OpenRouter

The native, trained computer-use tool only exists on the Anthropic Messages schema, so the
**driver loop is always a computer-use-capable Claude model**. To bill/route through
OpenRouter while keeping that tool, the `anthropic` SDK is pointed at OpenRouter's
**Anthropic-compatible endpoint** (`OPENROUTER_API_KEY` + `base_url=https://openrouter.ai/api`);
with no OR key it talks to Anthropic first-party. `uaa/constants.py` maps each model (Anthropic
id or OpenRouter slug) to its `computer_2025xxxx` tool version + beta header. The agent manages
cost by hot-swapping its Claude tier and restarting.

> One thing to confirm on the droplet's first real run: that OpenRouter forwards the
> `computer-use` beta to the Anthropic provider. If it doesn't, the fallback is custom
> click/type/screenshot function tools driven by a vision model (built on demand).

---

## Owner controls (the daemon's job, and a human override)

The daemon keeps the organism alive and recoverable, and you can always step in.
None of these constrain what the agent pursues:

- **Killswitch** — `touch state/STOP` to halt cleanly; `rm state/STOP` to resume.
- **Audit log** — every action and lifecycle event in `logs/audit.jsonl`.
- **Source rollback** — if a self-edit bricks the agent's source, the daemon restores
  the last known-good snapshot (code only — the agent's memory/config/tools are kept).
  `daemon.py` is never modified by the agent.

---

## Run it

```bash
# on the droplet, from the repo root
./setup.sh                      # deps + venv (non-destructive: keeps an existing desktop/VNC)
# put OPENROUTER_API_KEY in .env  (+ confirm your Claude model slug at openrouter.ai/models)
./venv/bin/python daemon.py     # start the organism (foreground)
# …or install systemd/uaa.service to run it as a service
```

Watch it live by tunnelling VNC: `ssh -L 5901:localhost:5901 user@<droplet>` then
point a VNC viewer at `localhost:5901`. Tail `logs/daemon.log` and `logs/agent.log`.

**Stop:** `touch state/STOP` (or `systemctl stop uaa`).

### Observe & control it (`python -m uaa.ctl`)

```
python -m uaa.ctl status     # daemon/agent liveness, model, status, last error, memory, headless?, unread msgs
python -m uaa.ctl stop       # engage the killswitch (agent halts cleanly)
python -m uaa.ctl resume     # clear it
python -m uaa.ctl messages   # read messages the agent left you (its message_operator tool)
python -m uaa.ctl audit 50   # tail the action/lifecycle trail
python -m uaa.ctl logs 80    # tail the agent log
python -m uaa.ctl config|mem # dump config / memory
```

The agent reaches you with its **`message_operator`** tool (writes to an inbox you read with
`uaa.ctl messages`) — a channel that always works without email credentials. If it has set up a
real email/Slack MCP it can use that too, but this is the guaranteed path.

### Requirements on the desktop
`scrot` (or ImageMagick `import`) for screenshots, `xdotool` for input, a running
X display at `UAA_DISPLAY` (`:1`). Keep the long edge ≤ 2576 so screenshots map 1:1 to
model coordinates (geometry is auto-detected at boot).

> **⚠️ The #1 first-deploy gotcha — X11 permissions.** If your VNC/Xvfb session runs as one user
> (e.g. `ubuntu`) but the daemon runs as another (e.g. `root` via the systemd unit), the daemon
> **can't reach the X socket** and the agent is silently *blind* (every screenshot errors; nothing
> crashes). The daemon now logs a loud `DISPLAY … is NOT reachable` warning at startup — watch for
> it. Fix by **running the daemon as the same user that owns the VNC session** (set `User=` in the
> unit to match), or make `/tmp/.X11-unix` world-readable and export `XAUTHORITY`. If it can't see
> the screen, the agent falls back to operating headless (shell/files only).

### If computer-use isn't available
If the endpoint rejects the computer-use beta (e.g. OpenRouter doesn't forward it), the agent
**doesn't crash-loop** — it flips to **headless mode** (bash/files/MCP/self-mod, no GUI), records
`gui_available:false`, and keeps running. `uaa.ctl status` shows `HEADLESS`. Re-enable with
`request_mutation(changes={gui_available:true})` after fixing the endpoint, or switch to a direct
Anthropic key. A bad self-initiated model swap likewise auto-reverts to the last working model.

### Env knobs
`UAA_HOME` relocates `state/`/`logs/`/`mcp/` (run more than one organism). `UAA_AGENT_CMD`
overrides the agent entrypoint (debugging). Daemon timing is tunable via
`UAA_HANG_TIMEOUT_S`, `UAA_STABLE_AFTER_S`, `UAA_FAST_CRASH_S`, `UAA_MAX_BOOT_CRASHES`,
`UAA_COOLDOWN_S`, `UAA_BACKOFF_BASE_S`, `UAA_BACKOFF_MAX_S`.

---

## Tests

`bash tests/run_all.sh` — runs offline (no API key, no display):

- **agent loop** — scripted fake driver proves the loop dispatches tools, builds
  `tool_result`s, applies a mutation, and exits with the right code.
- **daemon supervision** — black-box: launches the real daemon against a scripted
  fake agent and asserts spawn → MUTATE-reboot → SLEEP-wake → DONE-cooldown →
  crash-backoff → **source rollback** → **hang-kill** → killswitch-halt, and that
  the daemon never dies.
- **mcp live** — connects `MCPManager` to a real stdio MCP server and round-trips
  tool calls (needs the `mcp` package; skipped if absent).

What's *not* covered offline and needs the droplet: real computer-use API turns and
the X display (clicking/typing/screenshots), and connecting a real hosted MCP server.
