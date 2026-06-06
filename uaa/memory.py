"""Rolling memory: keep the live context bounded, and carry a high-level summary
across reboots/sleeps in config.memory_context.

We do our own lightweight summarization (cheap model) rather than relying on
server-side compaction, so the surviving summary is also the thing the agent
re-reads on its next boot — one mechanism for both jobs.
"""
from __future__ import annotations

MAX_MEMORY_CHARS = 16000        # a compaction summary is the agent's working state — allow detail
_MAX_TRANSCRIPT_CHARS = 400000  # cap the rendered text fed to the summarizer (images already dropped)

COMPACTION_PROMPT = """\
You are compacting the working context of an autonomous agent so it can keep going seamlessly \
after older messages are dropped. Write a thorough, structured summary that preserves EVERYTHING \
needed to continue without re-discovering it. It is far worse to omit a fact than to be verbose. \
Use these sections with concrete specifics (omit a section only if truly empty):

1. OBJECTIVE — the current goal and any sub-goals.
2. STATE OF THE WORLD — what is on screen / running now; software installed; accounts and logins \
that exist; what's been set up (MCP servers registered, tools created, files written and their paths).
3. PROGRESS — what's been accomplished and the key decisions made (and why).
4. CREDENTIALS & IDENTIFIERS — accounts, usernames, tokens, URLs, file paths, IDs the agent will \
need again. Record them verbatim; do NOT drop them.
5. BLOCKERS / WAITING-ON — anything the agent is waiting for (e.g. an email reply) and from whom.
6. IMMEDIATE NEXT STEP — the very next concrete action to take.
7. PITFALLS — what already failed and must not be blindly retried.
{focus}
PRIOR MEMORY (carry forward anything still true; it may already hold older context):
{prior}

CONVERSATION TO SUMMARIZE:
{transcript}

Output ONLY the summary."""


def _render(messages: list) -> str:
    """Flatten a message list to text for summarization (images dropped)."""
    out = []
    for msg in messages:
        role = msg["role"] if isinstance(msg, dict) else getattr(msg, "role", "?")
        content = msg["content"] if isinstance(msg, dict) else getattr(msg, "content", "")
        if isinstance(content, str):
            out.append(f"{role}: {content}")
            continue
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if btype == "text":
                txt = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
                out.append(f"{role}: {txt}")
            elif btype == "thinking":
                th = block.get("thinking") if isinstance(block, dict) else getattr(block, "thinking", "")
                if th:
                    out.append(f"{role}[thinking]: {th}")
            elif btype == "tool_use":
                name = block.get("name") if isinstance(block, dict) else getattr(block, "name", "?")
                inp = block.get("input") if isinstance(block, dict) else getattr(block, "input", "")
                out.append(f"{role}[tool_use {name}]: {str(inp)[:300]}")
            elif btype == "tool_result":
                # tool_result content is itself a list of blocks; pull any text.
                inner = block.get("content") if isinstance(block, dict) else getattr(block, "content", "")
                if isinstance(inner, list):
                    texts = []
                    for b in inner:
                        bt = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                        if bt == "text":
                            texts.append((b.get("text") if isinstance(b, dict) else getattr(b, "text", "")) or "")
                    out.append(f"{role}[tool_result]: {' '.join(texts)[:300]}")
                elif isinstance(inner, str):
                    out.append(f"{role}[tool_result]: {inner[:300]}")
    return "\n".join(out)


def summarize(client, model: str, messages: list, prior_memory: str, focus: str | None = None) -> str:
    """Compact the working transcript into a thorough memory summary.

    Uses the agent's own model for quality (it has to preserve real state). Robust:
    retries once and never raises — falls back to the prior memory so compaction can
    never crash the loop.
    """
    transcript = _render(messages)
    focus_line = (f"FOCUS — emphasize this in the summary: {focus}\n" if focus else "")
    prompt = COMPACTION_PROMPT.format(
        focus=focus_line,
        prior=prior_memory or "(none)",
        transcript=transcript[-_MAX_TRANSCRIPT_CHARS:],
    )
    for _ in range(2):
        try:
            resp = client.messages.create(
                # 8192 is comfortably above the MAX_MEMORY_CHARS (~4k-token) cap, so the
                # model finishes the summary rather than getting silently truncated.
                model=model,
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            )
            # Response blocks may be SDK objects OR dicts (e.g. via OpenRouter) — handle both,
            # otherwise an AttributeError here would silently drop every fresh summary.
            text = next((
                (b.get("text") if isinstance(b, dict) else getattr(b, "text", ""))
                for b in resp.content
                if (b.get("type") if isinstance(b, dict) else getattr(b, "type", None)) == "text"
            ), "")
            if text and text.strip():
                return text.strip()[:MAX_MEMORY_CHARS]
        except Exception:
            continue
    return prior_memory
