"""The inference layer.

Driver speaks the Anthropic Messages (computer-use beta) schema — but the client
is pointed at OpenRouter when OPENROUTER_API_KEY is set, so Claude is billed/routed
through OpenRouter while keeping the native, trained computer-use tool. With no OR
key it talks to Anthropic first-party directly. It assembles the native tools
(computer/bash/text_editor) at the right version for the driving model, sets
adaptive thinking + effort, attaches the beta header, and runs one turn.

The agent manages its own inference cost by swapping its driving model and
restarting (request_mutation) — we don't impose a budget here.
"""
from __future__ import annotations

import os

from . import computer, constants as C


class Driver:
    def __init__(self) -> None:
        import anthropic  # imported lazily so tooling without the dep still loads

        or_key = os.environ.get("OPENROUTER_API_KEY")
        if or_key:
            # OpenRouter's Anthropic-compatible endpoint: the SDK appends /v1/messages.
            base = os.environ.get("UAA_OPENROUTER_BASE", "https://openrouter.ai/api")
            self.client = anthropic.Anthropic(base_url=base, auth_token=or_key)
            self.via = "openrouter"
        else:
            self.client = anthropic.Anthropic()  # direct Anthropic (ANTHROPIC_API_KEY)
            self.via = "anthropic"

    def native_tools(self, model: str, gui: bool = True) -> list[dict]:
        tools = [dict(C.BASH_TOOL), dict(C.TEXT_EDITOR_TOOL)]
        if gui:  # the computer tool is the only one that needs the beta header
            comp_type, _ = C.computer_use_for(model)
            w, h = computer.get_geometry()
            computer_tool = {
                "type": comp_type, "name": "computer",
                "display_width_px": w, "display_height_px": h, "display_number": C.DISPLAY_NUMBER,
            }
            if comp_type == "computer_20251124":
                computer_tool["enable_zoom"] = True
            tools.insert(0, computer_tool)
        return tools

    def call(self, model, system, messages, extra_tools, effort="high", thinking=True, gui=True):
        """One turn of the agent loop. Returns the raw Message response.

        gui=False is the headless fallback: no computer tool and no computer-use beta,
        so the agent keeps running (bash/files/MCP) even if the endpoint won't accept
        the computer-use beta (e.g. OpenRouter doesn't forward it).
        """
        tools = self.native_tools(model, gui) + list(extra_tools)
        kwargs = {
            "model": model,
            "max_tokens": C.MAX_TOKENS,
            "system": system,
            "messages": messages,
            "tools": tools,
            "output_config": {"effort": effort},
        }
        if gui:
            kwargs["betas"] = [C.computer_use_for(model)[1]]
        if thinking:
            # display:summarized so reasoning lands in the audit log; blocks are
            # preserved automatically because we append response.content verbatim.
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}

        return self.client.beta.messages.create(**kwargs)
