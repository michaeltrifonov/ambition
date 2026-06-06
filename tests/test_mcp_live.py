"""Live end-to-end test of the MCP client against a real stdio MCP server.

Run with a python that has the `mcp` package installed:
    ./venv/bin/python tests/test_mcp_live.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uaa.mcp_client import MCPManager  # noqa: E402

PY = sys.executable
SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_echo_server.py")


def main() -> None:
    m = MCPManager([
        {"name": "echo", "transport": "stdio", "command": PY, "args": [SERVER], "enabled": True},
    ])
    m.start()
    try:
        names = sorted(s["name"] for s in m.tool_schemas())
        print("discovered tools:", names)
        assert "echo__echo" in names and "echo__add" in names, names

        out, err = m.call("echo__echo", {"text": "hello"})
        print("echo__echo ->", repr(out), "is_error:", err)
        assert out == "echo: hello" and not err, (out, err)

        out, err = m.call("echo__add", {"a": 2, "b": 40})
        print("echo__add  ->", repr(out), "is_error:", err)
        assert out == "42" and not err, (out, err)

        out, err = m.call("echo__missing", {})
        assert err, "unknown tool should error"

        # schema is surfaced for the model to call
        sch = next(s for s in m.tool_schemas() if s["name"] == "echo__echo")
        assert sch["input_schema"]["type"] == "object" and "text" in sch["input_schema"]["properties"]
        print("schema ok:", sch["input_schema"]["properties"].keys())
    finally:
        m.shutdown()
    print("MCP LIVE TEST PASSED")


if __name__ == "__main__":
    main()
