"""Provider drivers.

Each module here exposes a single coroutine:

    async def run(*, session, mcp_tools, question, system, model, effort,
                  max_turns, transcript) -> None

Everything a driver is allowed to do to the world is already fixed by
`agent.executor`. A driver decides *how to ask a model*, not *what the agent
can touch*. Adding a provider must never widen the action surface — if it
does, the new code belongs in the executor, under review.
"""

from __future__ import annotations

PROVIDERS = ("anthropic", "openai")
