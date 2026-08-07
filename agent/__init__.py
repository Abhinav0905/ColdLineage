"""ColdLineage agent — reads DataHub through the MCP Server, acts through a
constrained executor, with a human between the plan and the delete.

Layout:

    executor.py     the six operations + their schemas + the approval gate
    prompt.py       the system prompt
    mcp_datahub.py  the DataHub MCP stdio session
    render.py       terminal + transcript output
    drivers/        one module per model provider, and nothing else

Everything above `drivers/` is provider-neutral on purpose. The guardrail is the
tool list, not the model, so it has to live where the model cannot reach it.
"""

__all__ = ["executor", "prompt", "mcp_datahub", "render", "drivers"]
