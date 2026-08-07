"""Anthropic driver — Claude via the SDK's tool runner.

The runner owns the agentic loop: it calls the API, executes whichever tools
Claude asked for, feeds the results back, and repeats until Claude stops asking.
We supply the tools and read the messages as they stream past.

Tools are built from `executor.TOOLS`, not from docstrings, so the schema Claude
sees and the schema GPT sees are generated from one definition.
"""

from __future__ import annotations

import os
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.lib.tools import BetaAsyncFunctionTool
from anthropic.lib.tools.mcp import async_mcp_tool

from .. import render
from ..executor import TOOLS

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
MAX_TOKENS = 16_000

ENV_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def available() -> bool:
    return any(os.environ.get(k) for k in ENV_KEYS)


def credential_help() -> str:
    return (
        "ANTHROPIC_API_KEY is not set.\n"
        "    export ANTHROPIC_API_KEY=sk-ant-...\n"
        "  or authenticate with the Anthropic CLI (`ant auth login`)."
    )


def _executor_tools() -> list[BetaAsyncFunctionTool]:
    return [
        BetaAsyncFunctionTool(
            t["fn"],
            name=t["name"],
            description=t["description"],
            input_schema=t["schema"],
        )
        for t in TOOLS
    ]


def _render(message: Any, transcript: list[dict]) -> None:
    for block in message.content:
        if block.type == "thinking":
            render.thinking(getattr(block, "thinking", ""), transcript)
        elif block.type == "text":
            render.say(block.text, transcript)
        elif block.type == "tool_use":
            render.tool_use(block.name, block.input, transcript)


async def run(
    *,
    session: Any,
    mcp_tools: list[Any],
    question: str,
    system: str,
    model: str | None,
    effort: str,
    max_turns: int,
    transcript: list[dict],
) -> None:
    model = model or DEFAULT_MODEL
    client = AsyncAnthropic()

    tools = [async_mcp_tool(t, session) for t in mcp_tools] + _executor_tools()

    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        # Adaptive thinking with summaries visible, so the reasoning shows on camera.
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": effort},
        tools=tools,
        messages=[{"role": "user", "content": question}],
        max_iterations=max_turns,
    )

    last = None
    async for message in runner:
        last = message
        _render(message, transcript)

    if last is not None:
        render.usage(
            f"anthropic/{model}",
            stop_reason=last.stop_reason,
            **{
                "in": last.usage.input_tokens,
                "out": last.usage.output_tokens,
                "cache_read": getattr(last.usage, "cache_read_input_tokens", 0),
            },
        )
