"""The DataHub MCP session — shared by every driver.

MCP is an open protocol, which is exactly why this file has no provider in it.
The same stdio subprocess, the same `list_tools()`, the same `call_tool()`
dispatch feeds Claude and GPT alike. Each driver only has to translate the tool
*descriptions* into its own wire format; the transport and the calls are common.

The server is started with mutation tools off (its default), so an agent
physically cannot write to the catalog through MCP. Writeback happens solely
inside the ColdLineage executor, after an archive has been verified.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def resolve_host_url(url: str) -> tuple[str, str | None]:
    """Translate a container-only hostname into one the host can reach.

    `.env` is written for the containerised backend, where reaching a service on
    the host means `host.docker.internal`. The agent runs *on* the host, where
    that name does not resolve at all — so sourcing the project's own `.env`
    breaks it, which is a confusing way to fail for something that is really a
    one-word difference. Rewrite it and say so.

    Returns (url, note). The note is None when nothing needed changing.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return url, None
    try:
        socket.getaddrinfo(host, None)
        return url, None
    except socket.gaierror:
        pass
    if host != "host.docker.internal":
        return url, f"{host!r} does not resolve from here"
    port = f":{parsed.port}" if parsed.port else ""
    swapped = urlunparse(parsed._replace(netloc=f"localhost{port}"))
    return swapped, f"{host} is container-only; using {swapped} from the host"


DATAHUB_GMS_URL, _GMS_NOTE = resolve_host_url(
    os.environ.get("DATAHUB_GMS_URL", "http://localhost:8090")
)


def _server_params(datahub_gms_url: str) -> StdioServerParameters:
    env = {
        **os.environ,
        "DATAHUB_GMS_URL": datahub_gms_url,
        # The MCP server ships usage telemetry to a third-party endpoint. On a machine
        # behind TLS interception that produces a wall of certificate-verify retries on
        # every tool call and buys us nothing, so it is off.
        "DATAHUB_TELEMETRY_ENABLED": "false",
    }
    if os.environ.get("DATAHUB_TOKEN"):
        env["DATAHUB_GMS_TOKEN"] = os.environ["DATAHUB_TOKEN"]

    # Prefer the console script next to the interpreter running us, so the agent uses
    # the mcp-server-datahub from its own venv rather than whatever is on PATH.
    local = Path(sys.executable).parent / "mcp-server-datahub"
    command = str(local) if local.exists() else (shutil.which("mcp-server-datahub") or "")
    if not command:
        raise RuntimeError(
            "mcp-server-datahub not found. Install it into the agent venv:\n"
            "    .venv-agent/bin/pip install mcp-server-datahub"
        )
    return StdioServerParameters(command=command, args=["--transport", "stdio"], env=env)


@asynccontextmanager
async def datahub_mcp(datahub_gms_url: str = DATAHUB_GMS_URL):
    """Yield an initialised MCP ClientSession plus its advertised tools."""
    params = _server_params(datahub_gms_url)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            yield session, listed.tools


def result_to_text(result: Any) -> str:
    """Flatten an MCP CallToolResult into something a model can read.

    MCP returns a list of typed content blocks; every provider wants a string.
    Errors are surfaced as text rather than raised, so one failing catalog read
    does not abort the run — the model can see what happened and route around it.
    """
    if getattr(result, "isError", False):
        pass  # still render the content; it carries the error message

    chunks: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            chunks.append(text)
            continue
        data = getattr(block, "data", None)
        if data is not None:
            chunks.append(f"[{getattr(block, 'mimeType', 'binary')} omitted]")
            continue
        chunks.append(str(block))

    if not chunks:
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return json.dumps(structured, default=str)
        return "(no content)"
    return "\n".join(chunks)
