#!/usr/bin/env python3
"""ColdLineage agent — reads DataHub through the MCP Server, acts through a constrained executor.

    reads   ->  DataHub MCP Server (mcp-server-datahub), tools converted for Claude
    acts    ->  four ColdLineage operations: assess, simulate, plan, execute, restore
    gate    ->  a human types "approve" between plan and execute

The trust boundary is the point of this file. Claude never receives database
credentials, an S3 client, or the ability to issue SQL. It receives read-only MCP
tools plus five HTTP operations, one of which blocks on a human. Whatever the model
decides, the set of things it *can* do is fixed by this tool list -- which is a
stronger guarantee than any instruction in a system prompt.

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    .venv-agent/bin/python agent/coldlineage_agent.py \\
        "Can we archive anything from patient_encounters?"

Requires the ColdLineage API (default http://localhost:8000) and a DataHub GMS
(default http://localhost:8090). Both are read from the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx

try:
    import anthropic
    from anthropic import AsyncAnthropic, beta_async_tool
    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError as exc:  # pragma: no cover
    sys.exit(
        f"missing dependency: {exc}\n"
        "Install into the agent venv:\n"
        "    python3.11 -m venv .venv-agent\n"
        '    .venv-agent/bin/pip install "anthropic[mcp]" mcp-server-datahub httpx'
    )

COLDLINEAGE_URL = os.environ.get("COLDLINEAGE_URL", "http://localhost:8000").rstrip("/")
DATAHUB_GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8090")

# claude-opus-5 is the current Opus. Thinking is on by default on this model; effort
# is the lever for depth. This work is agentic and multi-step, so it runs at "high".
MODEL = "claude-opus-5"
EFFORT = "high"
MAX_TOKENS = 16_000

AUTO_APPROVE = False  # set by --auto-approve; the demo default is a real human gate


SYSTEM = f"""You are a data lifecycle engineer working inside a governed tiering system.

Your job: decide whether a specific DATE RANGE inside a table can be moved to cold
storage, prove it, and -- only with human approval -- execute the move.

WHY THIS IS NOT A ONE-LINER
DataHub can tell you a table is cold. It cannot tell you that half a table is cold,
because its model is dataset- and column-level. And nothing in DataHub moves a byte.
Your value is in the gap: find the date range no downstream consumer reads any more.

HOW TO DECIDE
A cutoff is safe if and only if it is no later than the earliest date any active
downstream consumer still reads. The ColdLineage tools derive each consumer's window
by parsing its real SQL out of DataHub. Trust that derivation over your own reading
of a query.

Everything unproven blocks. A consumer with no date predicate reads everything. A
consumer with no recorded SQL cannot be shown to be safe. Both refuse the archive,
and that refusal is correct -- absence of evidence is not evidence of safety.

TOOLS
You have DataHub MCP tools for reading the catalog (search, lineage, queries, entity
metadata) and five ColdLineage operations for assessing and acting. Use the MCP tools
when you want to look at the catalog directly -- to explain WHY a window is what it
is, to check ownership before recommending a move, to see the actual SQL. Use the
ColdLineage tools for the decision and the action.

HARD RULES
- Never claim a range is safe without naming the binding constraint and its headroom.
- An ACTIVE legal hold is unconditional. Do not propose a workaround.
- Never call coldlineage_execute_plan without having shown the user the plan first.
  That tool blocks on a human; if they decline, accept it and stop.
- A table being "hot" does not disqualify it. A heavily-queried table with four cold
  years is the best case there is, not a contradiction.
- Report what the tools returned. If a writeback operation partially failed, say so.

STYLE
Lead with the answer. State the cutoff you recommend, the rows it moves, and the
consumer that binds it -- then the supporting detail. Keep it to what a data owner
needs in order to approve or decline.

Environment: ColdLineage API at {COLDLINEAGE_URL}, DataHub GMS at {DATAHUB_GMS_URL}.
"""


# ---------------------------------------------------------------------------
# ColdLineage executor tools. This is the entire action surface.
# ---------------------------------------------------------------------------


async def _call(method: str, path: str, payload: dict | None = None) -> str:
    async with httpx.AsyncClient(timeout=300) as http:
        try:
            response = await http.request(method, f"{COLDLINEAGE_URL}{path}", json=payload)
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"ColdLineage API unreachable: {exc}"})
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:2000]}
    if response.status_code >= 400:
        return json.dumps({"error": True, "status": response.status_code, "detail": body}, default=str)
    return json.dumps(body, default=str)


@beta_async_tool
async def coldlineage_list_datasets() -> str:
    """List every dataset ColdLineage can act on, with its measured size, temperature
    score, archive state, and any blockers. Start here when the user has not named a
    specific table.
    """
    return await _call("GET", "/api/datasets")


@beta_async_tool
async def coldlineage_assess_dataset(dataset_id: int) -> str:
    """Full assessment of one dataset: the DataHub-sourced context, every downstream
    consumer's derived history window (with the SQL it was parsed from), the evidence
    graph, the temperature breakdown, and any hard blockers.

    Args:
        dataset_id: Numeric id from coldlineage_list_datasets.
    """
    return await _call("GET", f"/api/datasets/{dataset_id}")


@beta_async_tool
async def coldlineage_simulate_cutoff(dataset_id: int, cutoff_date: str) -> str:
    """Test one proposed cutoff against every downstream consumer without changing
    anything. Returns SAFE_TO_ARCHIVE, ARCHIVE_WITH_REHYDRATION, or DO_NOT_ARCHIVE,
    the binding constraint, and per-consumer headroom in days.

    Cheap and side-effect free -- call it several times to find the best cutoff.

    Args:
        dataset_id: Numeric id from coldlineage_list_datasets.
        cutoff_date: Proposed cutoff, YYYY-MM-DD. Rows strictly older move to cold storage.
    """
    return await _call("POST", f"/api/datasets/{dataset_id}/simulate", {"cutoff_date": cutoff_date})


@beta_async_tool
async def coldlineage_build_plan(dataset_id: int, cutoff_date: str) -> str:
    """Turn a cutoff into an executable plan: exact row count, estimated bytes, the
    verdict, all blockers, and a plan_hash binding those facts together.

    Nothing moves. You must build a plan before you can execute one.

    Args:
        dataset_id: Numeric id from coldlineage_list_datasets.
        cutoff_date: The cutoff to plan, YYYY-MM-DD.
    """
    return await _call("POST", f"/api/datasets/{dataset_id}/plan", {"cutoff_date": cutoff_date})


@beta_async_tool
async def coldlineage_execute_plan(plan_hash: str, approved_by: str) -> str:
    """Execute an approved plan. THIS MOVES DATA AND DELETES THE SOURCE ROWS.

    A human is asked to confirm before anything runs; if they decline, this returns a
    declined result and nothing happens. The executor re-derives the plan from live
    state and refuses if anything drifted since the plan was built, then archives to
    Parquet, re-downloads the object and re-verifies its checksum, and only then
    deletes -- finally writing the archive provenance back into DataHub.

    Args:
        plan_hash: The plan_hash from coldlineage_build_plan.
        approved_by: Identity of the person approving. Ask the user; do not invent one.
    """
    print("\n" + "=" * 72)
    print("  HUMAN APPROVAL REQUIRED — this will move data and delete source rows")
    print("=" * 72)
    print(f"  plan_hash   : {plan_hash}")
    print(f"  approved_by : {approved_by}")
    print("=" * 72)

    if AUTO_APPROVE:
        print("  --auto-approve was passed; proceeding without prompting.\n")
    else:
        answer = await asyncio.to_thread(input, '  Type "approve" to execute, anything else to decline: ')
        if answer.strip().lower() != "approve":
            print("  Declined. Nothing was moved.\n")
            return json.dumps(
                {
                    "declined": True,
                    "message": (
                        "The human declined this plan. Nothing was executed and no rows were "
                        "removed. Do not retry or look for another route to execute it."
                    ),
                }
            )
        print("  Approved. Executing...\n")

    return await _call("POST", "/api/execute", {"plan_hash": plan_hash, "approved_by": approved_by})


@beta_async_tool
async def coldlineage_restore(run_id: int, temporary: bool = True) -> str:
    """Rehydrate an archived range. Verifies the stored object's SHA-256 against the
    manifest and refuses to restore on mismatch.

    Args:
        run_id: The run_id returned by coldlineage_execute_plan.
        temporary: True restores into a side table for inspection; False appends back
            into the source table, making it whole again.
    """
    return await _call("POST", "/api/restore", {"run_id": run_id, "temporary": temporary})


EXECUTOR_TOOLS = [
    coldlineage_list_datasets,
    coldlineage_assess_dataset,
    coldlineage_simulate_cutoff,
    coldlineage_build_plan,
    coldlineage_execute_plan,
    coldlineage_restore,
]


# ---------------------------------------------------------------------------
# Transcript rendering
# ---------------------------------------------------------------------------


def _render(message: Any, transcript: list[dict]) -> None:
    for block in message.content:
        if block.type == "thinking" and getattr(block, "thinking", ""):
            print(f"\n\033[2m[thinking] {block.thinking}\033[0m")
            transcript.append({"type": "thinking", "text": block.thinking})
        elif block.type == "text" and block.text.strip():
            print(f"\n{block.text}")
            transcript.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            args = json.dumps(block.input, default=str)
            if len(args) > 160:
                args = args[:157] + "..."
            print(f"\n\033[36m  -> {block.name}({args})\033[0m")
            transcript.append({"type": "tool_use", "name": block.name, "input": block.input})


async def preflight() -> bool:
    ok = True
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            health = (await http.get(f"{COLDLINEAGE_URL}/api/health")).json()
            dh = health.get("datahub", {})
            print(f"  ColdLineage : {COLDLINEAGE_URL}  (DataHub {dh.get('mode')}, reachable={dh.get('reachable')})")
            if dh.get("mode") == "live" and not dh.get("reachable"):
                print("    ! DataHub is configured live but unreachable — context reads will fail.")
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"  ColdLineage : UNREACHABLE at {COLDLINEAGE_URL} ({type(exc).__name__})")
            ok = False
    return ok


async def main() -> int:
    global AUTO_APPROVE

    parser = argparse.ArgumentParser(description="ColdLineage agent (DataHub MCP + constrained executor)")
    parser.add_argument("prompt", nargs="*", help="What to ask the agent.")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Skip the human gate. For automated testing only — never for a demo.")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--save-transcript", metavar="PATH", help="Write the transcript as JSON.")
    args = parser.parse_args()

    AUTO_APPROVE = args.auto_approve
    question = " ".join(args.prompt) or "Which datasets have an archivable historical range, and what cutoff do you recommend for the best candidate?"

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ANTHROPIC_API_KEY is not set.\n"
              "  export ANTHROPIC_API_KEY=sk-ant-...\n"
              "or authenticate with the Anthropic CLI (`ant auth login`).", file=sys.stderr)
        return 2

    print("\033[1mColdLineage agent\033[0m")
    print(f"  model       : {MODEL} (effort={EFFORT}, adaptive thinking)")
    if not await preflight():
        print("\n  Preflight failed. Start the stack first (see README) and retry.")
        return 1

    # The DataHub MCP server runs as a subprocess over stdio and inherits DATAHUB_GMS_URL
    # from this process. Its tools are read-only here: mutation tools stay off, so the
    # agent physically cannot write to the catalog except through our verified executor.
    env = {
        **os.environ,
        "DATAHUB_GMS_URL": DATAHUB_GMS_URL,
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
        print("mcp-server-datahub not found. Install it into the agent venv:\n"
              '    .venv-agent/bin/pip install mcp-server-datahub', file=sys.stderr)
        return 2

    server = StdioServerParameters(command=command, args=["--transport", "stdio"], env=env)

    client = AsyncAnthropic()
    transcript: list[dict] = [{"type": "question", "text": question}]

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            listed = await mcp.list_tools()
            mcp_tools = [async_mcp_tool(t, mcp) for t in listed.tools]
            print(f"  DataHub MCP : {len(mcp_tools)} tools — {', '.join(t.name for t in listed.tools[:6])}"
                  f"{' ...' if len(listed.tools) > 6 else ''}")
            print(f"  executor    : {len(EXECUTOR_TOOLS)} constrained operations (human gate on execute)")
            print(f"\n\033[1m> {question}\033[0m")

            runner = client.beta.messages.tool_runner(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": EFFORT},
                tools=[*mcp_tools, *EXECUTOR_TOOLS],
                messages=[{"role": "user", "content": question}],
                max_iterations=args.max_turns,
            )

            last = None
            async for message in runner:
                last = message
                _render(message, transcript)

    print("\n" + "-" * 72)
    if last is not None:
        usage = last.usage
        print(f"  stop_reason={last.stop_reason}  "
              f"in={usage.input_tokens} out={usage.output_tokens} "
              f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)}")

    if args.save_transcript:
        with open(args.save_transcript, "w") as fh:
            json.dump({"question": question, "model": MODEL, "effort": EFFORT, "transcript": transcript},
                      fh, indent=2, default=str)
        print(f"  transcript -> {args.save_transcript}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130) from None
