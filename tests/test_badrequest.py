"""A 400 (BadRequestError) must NOT crash-loop. It should recover in layers:
revert a bad self-model-swap, or fall back to headless mode if computer-use is rejected.

    python3 tests/test_badrequest.py
"""
import os
import sys
import tempfile
import types
from types import SimpleNamespace as NS

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ["UAA_HOME"] = tempfile.mkdtemp(prefix="uaa_badreq_")

_anth = types.ModuleType("anthropic")
for n in ("RateLimitError", "InternalServerError", "APIConnectionError", "BadRequestError"):
    setattr(_anth, n, type(n, (Exception,), {}))
_anth.APIError = Exception
sys.modules["anthropic"] = _anth

from uaa import constants as C, runtime, state  # noqa: E402


class FakeResp:
    def __init__(self, content):
        self.content = content
        self.usage = NS(input_tokens=5, output_tokens=1,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0)


def tool(tid, name, inp):
    return NS(type="tool_use", id=tid, name=name, input=inp)


class NoMCP:
    def __init__(self, *a, **k): ...
    def start(self): ...
    def tool_schemas(self): return []
    def status(self): return {"connected": [], "failed": {}}
    def is_mcp_tool(self, n): return False
    def call(self, n, a): return "x", False
    def shutdown(self): ...


def run_main():
    runtime.MCPManager = NoMCP
    try:
        runtime.main()
    except SystemExit as e:
        return e.code


def test_headless_fallback():
    # GUI call 400s; no last-good model yet -> drop to headless, which succeeds.
    state.ensure()
    state.update(lambda c: (c.__setitem__("gui_available", True),
                            c.__setitem__("last_good_model", None),
                            c.__setitem__("current_model", "anthropic/claude-opus-4.8")) and None)

    class GuiFail:
        client = None

        def call(self, model, system, messages, extra_tools, effort, thinking, gui=True):
            if gui:
                raise _anth.BadRequestError("computer-use tool not supported by endpoint")
            return FakeResp([tool("h", "request_mutation", {"reason": "headless ok"})])

    runtime.Driver = lambda: GuiFail()
    code = run_main()
    cfg = state.read()
    assert code == C.EXIT_MUTATE, code
    assert cfg["gui_available"] is False, "should have fallen back to headless, not crash-looped"
    assert cfg["last_good_model"] == "anthropic/claude-opus-4.8", "headless success records last-good model"
    print("ok: computer-use 400 -> headless fallback (no crash-loop), last-good recorded")


def test_bad_model_revert():
    # A self-inflicted bad model swap 400s -> revert to the last-good model + reboot.
    state.ensure()
    state.update(lambda c: (c.__setitem__("gui_available", True),
                            c.__setitem__("last_good_model", "anthropic/claude-opus-4.8"),
                            c.__setitem__("current_model", "anthropic/does-not-exist")) and None)

    class AlwaysBad:
        client = None

        def call(self, model, system, messages, extra_tools, effort, thinking, gui=True):
            raise _anth.BadRequestError(f"model {model} not found")

    runtime.Driver = lambda: AlwaysBad()
    code = run_main()
    cfg = state.read()
    assert code == C.EXIT_MUTATE, code
    assert cfg["current_model"] == "anthropic/claude-opus-4.8", "should revert to last-good model"
    assert "reverted bad model swap" in (cfg.get("last_error") or "")
    print("ok: bad model swap 400 -> revert to last-good model + reboot")


if __name__ == "__main__":
    test_headless_fallback()
    test_bad_model_revert()
    print("BADREQUEST RECOVERY TEST PASSED")
