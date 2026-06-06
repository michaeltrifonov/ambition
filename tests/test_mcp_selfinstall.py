"""Live end-to-end test of the agent's MCP self-install loop.

Proves the full path the agent takes when it decides "I should give myself an
MCP to interact with X": it registers the server in its own config the way
request_mutation would (config['mcp_servers'] = [...]), then a fresh boot reads
that config and stands the server up, discovers its tools, and calls them.

We don't drive the real request_mutation tool (that reboots the process); we
reproduce *exactly* what it writes — runtime.py's dispatch does `c.update(changes)`
into config, so a mutation carrying `mcp_servers=[...]` is identical to
`state.update` setting `config['mcp_servers']` here. Then we do what runtime.main()
does on the next boot: `MCPManager(state.read()['mcp_servers']).start()`.

The registry entry's `command` is the venv python so the spawned stdio server
can `import mcp` — i.e. the same interpreter the agent would have installed the
package into before registering the server.

Run with the venv python (it has the `mcp` package):
    ./venv/bin/python tests/test_mcp_selfinstall.py
"""
import os
import sys
import tempfile

# Make the repo importable (uaa.*) regardless of cwd.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

SERVER = os.path.join(REPO_ROOT, "tests", "mcp_echo_server.py")
# The interpreter the registry entry will spawn the stdio server with. Must have
# `mcp` importable — that's the venv python this test itself runs under.
VENV_PY = sys.executable


def main() -> None:
    # 1. Fresh UAA_HOME so we never touch the repo's real state/config.json.
    #    Set BEFORE importing uaa.* so constants.py picks it up at import time.
    tmp_home = tempfile.mkdtemp(prefix="uaa_selfinstall_")
    os.environ["UAA_HOME"] = tmp_home

    from uaa import state  # noqa: E402  (imported after UAA_HOME is set)
    from uaa import constants as C  # noqa: E402
    from uaa.mcp_client import MCPManager  # noqa: E402

    assert C.HOME_DIR == os.path.realpath(tmp_home) or str(C.HOME_DIR) == tmp_home, \
        ("UAA_HOME not honored", str(C.HOME_DIR), tmp_home)
    print("UAA_HOME      :", tmp_home)
    print("config target :", C.CONFIG_PATH)
    print("venv python   :", VENV_PY)

    # 2. First boot: create config.json from defaults. Starts with no MCP servers.
    cfg = state.ensure()
    assert cfg["mcp_servers"] == [], cfg["mcp_servers"]
    assert C.CONFIG_PATH.exists(), "ensure() should have written config.json"

    # 3. The agent decides to give itself the 'echo' MCP and registers it. This is
    #    precisely what request_mutation(changes={'mcp_servers': [...]}) writes:
    #    runtime.py applies it via `c.update(changes)`. We reproduce that write.
    entry = {
        "name": "echo",
        "transport": "stdio",
        "command": VENV_PY,        # interpreter that can import `mcp`
        "args": [SERVER],
        "enabled": True,
    }
    state.update(lambda c: c.__setitem__("mcp_servers", [entry]))

    # Re-read from disk to prove it persisted (a real reboot re-reads the file).
    persisted = state.read()["mcp_servers"]
    assert persisted == [entry], persisted
    print("registered    :", persisted[0]["name"], "->", persisted[0]["command"])

    # 4. Fresh boot wiring: runtime.main() does MCPManager(cfg['mcp_servers']).
    m = MCPManager(state.read()["mcp_servers"])
    m.start()
    try:
        # 5. Connect + discover: tools surface namespaced <server>__<tool>.
        names = sorted(s["name"] for s in m.tool_schemas())
        print("discovered    :", names)
        assert "echo__echo" in names, ("echo__echo missing", names)
        assert "echo__add" in names, ("echo__add missing", names)

        # 6. Use: call a discovered tool and check the result.
        out, err = m.call("echo__echo", {"text": "self-installed"})
        print("echo__echo    ->", repr(out), "is_error:", err)
        assert out == "echo: self-installed" and not err, (out, err)

        out, err = m.call("echo__add", {"a": 19, "b": 23})
        print("echo__add     ->", repr(out), "is_error:", err)
        assert out == "42" and not err, (out, err)

        # The tool schema is well-formed for the model to call.
        sch = next(s for s in m.tool_schemas() if s["name"] == "echo__echo")
        assert sch["input_schema"]["type"] == "object", sch["input_schema"]
        assert "text" in sch["input_schema"]["properties"], sch["input_schema"]
    finally:
        m.shutdown()

    print("MCP SELF-INSTALL TEST PASSED")


if __name__ == "__main__":
    main()
