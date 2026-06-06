"""Exercise the agent-owned compaction + the auto-guard with a scripted driver.

Proves: the `compact` tool summarizes + resets the transcript (passing focus),
the auto-guard fires when context crosses the threshold, memory_context is
updated, and the loop keeps running cleanly (no orphaned tool pairs / crash).

    python3 tests/test_compaction.py
"""
import os
import sys
import tempfile
import types
from types import SimpleNamespace as NS

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ["UAA_HOME"] = tempfile.mkdtemp(prefix="uaa_compact_test_")

_anth = types.ModuleType("anthropic")
for n in ("RateLimitError", "InternalServerError", "APIConnectionError"):
    setattr(_anth, n, type(n, (Exception,), {}))
_anth.APIError = Exception
sys.modules["anthropic"] = _anth

from uaa import constants as C, memory, runtime, state  # noqa: E402


def usage(input_tokens):
    return NS(input_tokens=input_tokens, output_tokens=1,
              cache_read_input_tokens=0, cache_creation_input_tokens=0)


def tool(id, name, inp):
    return NS(type="tool_use", id=id, name=name, input=inp)


class FakeResp:
    def __init__(self, content, input_tokens):
        self.content = content
        self.usage = usage(input_tokens)


class FakeDriver:
    client = None

    def __init__(self):
        self.calls = 0
        self.first_texts = []   # the first message's text at each call

    def call(self, model, system, messages, extra_tools, effort, thinking, gui=True):
        self.calls += 1
        first = messages[0]["content"]
        self.first_texts.append(first[0]["text"] if isinstance(first, list) else str(first))
        if self.calls == 1:   # agent asks to compact (focus passed)
            return FakeResp([tool("a", "compact", {"focus": "keep the plan"})], input_tokens=5)
        if self.calls == 2:   # huge context -> auto-guard must fire
            return FakeResp([tool("b", "bash", {"command": "echo hi"})], input_tokens=5000)
        return FakeResp([tool("c", "request_mutation", {"reason": "done"})], input_tokens=5)


class NoMCP:
    def __init__(self, *a, **k): ...
    def start(self): ...
    def tool_schemas(self): return []
    def status(self): return {"connected": [], "failed": {}}
    def is_mcp_tool(self, n): return False
    def call(self, n, a): return "x", False
    def shutdown(self): ...


def main() -> None:
    summarize_calls = []

    def fake_summarize(client, model, messages, prior, focus=None):
        summarize_calls.append({"focus": focus})
        return f"SUMMARY#{len(summarize_calls)} focus={focus}"

    runtime.Driver = FakeDriver
    runtime.MCPManager = NoMCP
    memory.summarize = fake_summarize

    state.ensure()
    state.update(lambda c: c.__setitem__("context_window", 100))  # compact_at = 80

    fake = FakeDriver()  # capture the instance main() will use
    runtime.Driver = lambda: fake

    code = None
    try:
        runtime.main()
    except SystemExit as e:
        code = e.code

    assert code == C.EXIT_MUTATE, code
    # two compactions: one agent-requested (focus), one auto-guard (no focus)
    assert len(summarize_calls) == 2, summarize_calls
    assert summarize_calls[0]["focus"] == "keep the plan", summarize_calls
    assert summarize_calls[1]["focus"] is None, summarize_calls
    # memory_context holds the latest summary
    assert state.read()["memory_context"] == "SUMMARY#2 focus=None", state.read()["memory_context"]
    # the transcript was actually reset to the compaction summary between calls
    assert "[BOOT" in fake.first_texts[0]
    assert "compacted" in fake.first_texts[1] and "SUMMARY#1" in fake.first_texts[1]
    assert "SUMMARY#2" in fake.first_texts[2]
    print("ok: compact tool (with focus) + auto-guard both fired; transcript reset; memory updated")
    print("COMPACTION TEST PASSED")


if __name__ == "__main__":
    main()
