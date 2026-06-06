"""Validate the agent's perceive->think->act loop with a scripted fake driver.

No Anthropic API and no display: we stub `anthropic` (only its exception types are
referenced) and replace Driver/MCPManager. The fake driver returns a native tool
call (bash) then a control tool (request_mutation), letting us assert the loop
executes tools, builds tool_results, applies the mutation, and exits EXIT_MUTATE.

    python3 tests/test_agent_loop.py
"""
import os
import sys
import tempfile
import types
from types import SimpleNamespace as NS

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

HOME = tempfile.mkdtemp(prefix="uaa_loop_test_")
os.environ["UAA_HOME"] = HOME

# Stub `anthropic` so `import anthropic` in runtime.main() resolves (we only need
# the exception classes to exist; the fake driver never raises them).
_anth = types.ModuleType("anthropic")
for name in ("RateLimitError", "InternalServerError", "APIConnectionError"):
    setattr(_anth, name, type(name, (Exception,), {}))
_anth.APIError = Exception
sys.modules["anthropic"] = _anth

from uaa import constants as C, memory, runtime, state  # noqa: E402

PROBE = os.path.join(HOME, "probe")


class FakeResp:
    def __init__(self, content):
        self.content = content
        self.usage = NS(input_tokens=1, output_tokens=1,
                        cache_creation_input_tokens=0, cache_read_input_tokens=0)


class FakeDriver:
    client = None

    def __init__(self):
        self.calls = 0

    def call(self, model, system, messages, extra_tools, effort, thinking, gui=True):
        self.calls += 1
        # Sanity: the control tools and (empty) MCP tools are actually offered.
        assert any(t["name"] == "request_mutation" for t in extra_tools)
        if self.calls == 1:
            return FakeResp([
                NS(type="text", text="taking an action"),
                NS(type="tool_use", id="t1", name="bash",
                   input={"command": f"echo hi > {PROBE}"}),
            ])
        return FakeResp([
            NS(type="tool_use", id="t2", name="request_mutation",
               input={"reason": "switch model", "changes": {"current_model": "claude-sonnet-4-6"}}),
        ])


class NoMCP:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def tool_schemas(self):
        return []

    def status(self):
        return {"connected": [], "failed": {}}

    def is_mcp_tool(self, n):
        return False

    def call(self, n, a):
        return "x", False

    def shutdown(self):
        pass


def main() -> None:
    runtime.Driver = FakeDriver
    runtime.MCPManager = NoMCP
    memory.summarize = lambda client, model, messages, prior, focus=None: prior or "mem"  # no real API

    state.ensure()
    code = None
    try:
        runtime.main()
    except SystemExit as e:
        code = e.code

    assert code == C.EXIT_MUTATE, f"expected EXIT_MUTATE, got {code}"
    assert os.path.exists(PROBE), "bash tool was not actually executed by the loop"
    cfg = state.read()
    assert cfg["current_model"] == "claude-sonnet-4-6", cfg["current_model"]
    assert cfg["status"] == C.STATUS_MUTATING, cfg["status"]
    print("ok: loop dispatched bash, built tool_result, applied mutation, exited EXIT_MUTATE")
    print("AGENT LOOP TEST PASSED")


if __name__ == "__main__":
    main()
