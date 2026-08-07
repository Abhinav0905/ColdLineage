"""The ColdLineage action surface — six operations, no vendor in sight.

This module is the security boundary, and it is deliberately free of any model
SDK. It knows how to call the ColdLineage API over HTTP and how to ask a human
before destroying anything. It does not know, and must never know, which model
is driving it.

Two consequences worth stating plainly:

  1. The set of things an agent *can* do is fixed here, in `TOOLS`. Swapping
     Claude for GPT does not widen it by one byte. That is the whole argument
     for putting the guardrail in the tool list rather than in a prompt.

  2. The human approval gate lives in `execute_plan`, not in a provider's tool
     wrapper. Any driver that can reach the executor inherits the gate; there
     is no code path to the delete that routes around it.

No database credentials, no object-store client, no SQL. Just six HTTP calls.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable

import httpx

COLDLINEAGE_URL = os.environ.get("COLDLINEAGE_URL", "http://localhost:8000").rstrip("/")

# Set by the CLI's --auto-approve. The demo default is a real human gate; this
# exists so the test suite can exercise the executor without a TTY.
AUTO_APPROVE = False


def set_auto_approve(value: bool) -> None:
    global AUTO_APPROVE
    AUTO_APPROVE = value


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


# ---------------------------------------------------------------------------
# The six operations
# ---------------------------------------------------------------------------


async def list_datasets() -> str:
    return await _call("GET", "/api/datasets")


async def assess_dataset(dataset_id: int) -> str:
    return await _call("GET", f"/api/datasets/{int(dataset_id)}")


async def simulate_cutoff(dataset_id: int, cutoff_date: str) -> str:
    return await _call(
        "POST", f"/api/datasets/{int(dataset_id)}/simulate", {"cutoff_date": cutoff_date}
    )


async def build_plan(dataset_id: int, cutoff_date: str) -> str:
    return await _call(
        "POST", f"/api/datasets/{int(dataset_id)}/plan", {"cutoff_date": cutoff_date}
    )


async def execute_plan(plan_hash: str, approved_by: str) -> str:
    """The only destructive operation, and the only one that blocks on a human."""
    print("\n" + "=" * 72)
    print("  HUMAN APPROVAL REQUIRED — this will move data and delete source rows")
    print("=" * 72)
    print(f"  plan_hash   : {plan_hash}")
    print(f"  approved_by : {approved_by}")
    print("=" * 72)

    if AUTO_APPROVE:
        print("  --auto-approve was passed; proceeding without prompting.\n")
    else:
        answer = await asyncio.to_thread(
            input, '  Type "approve" to execute, anything else to decline: '
        )
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


async def restore(run_id: int, temporary: bool = True) -> str:
    return await _call("POST", "/api/restore", {"run_id": int(run_id), "temporary": bool(temporary)})


# ---------------------------------------------------------------------------
# One schema definition, two wire formats
# ---------------------------------------------------------------------------
#
# JSON Schema is the common denominator: Anthropic calls it `input_schema`,
# OpenAI calls it `parameters`, and both speak the same dialect underneath.
# Written strict-compatible (every property required, additionalProperties
# false) so OpenAI's structured-output mode accepts them unmodified.


def _schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "coldlineage_list_datasets",
        "description": (
            "List every dataset ColdLineage can act on, with its measured size, temperature "
            "score, archive state, and any blockers. Start here when the user has not named "
            "a specific table."
        ),
        "schema": _schema({}),
        "fn": list_datasets,
    },
    {
        "name": "coldlineage_assess_dataset",
        "description": (
            "Full assessment of one dataset: the DataHub-sourced context, every downstream "
            "consumer's derived history window (with the SQL it was parsed from), the "
            "evidence graph, the temperature breakdown, and any hard blockers."
        ),
        "schema": _schema(
            {
                "dataset_id": {
                    "type": "integer",
                    "description": "Numeric id from coldlineage_list_datasets.",
                }
            }
        ),
        "fn": assess_dataset,
    },
    {
        "name": "coldlineage_simulate_cutoff",
        "description": (
            "Test one proposed cutoff against every downstream consumer without changing "
            "anything. Returns SAFE_TO_ARCHIVE, ARCHIVE_WITH_REHYDRATION, or DO_NOT_ARCHIVE, "
            "the binding constraint, and per-consumer headroom in days. Cheap and side-effect "
            "free — call it several times to find the best cutoff."
        ),
        "schema": _schema(
            {
                "dataset_id": {
                    "type": "integer",
                    "description": "Numeric id from coldlineage_list_datasets.",
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "Proposed cutoff, YYYY-MM-DD. Rows strictly older move to cold storage.",
                },
            }
        ),
        "fn": simulate_cutoff,
    },
    {
        "name": "coldlineage_build_plan",
        "description": (
            "Turn a cutoff into an executable plan: exact row count, estimated bytes, the "
            "verdict, all blockers, and a plan_hash binding those facts together. Nothing "
            "moves. You must build a plan before you can execute one."
        ),
        "schema": _schema(
            {
                "dataset_id": {
                    "type": "integer",
                    "description": "Numeric id from coldlineage_list_datasets.",
                },
                "cutoff_date": {
                    "type": "string",
                    "description": "The cutoff to plan, YYYY-MM-DD.",
                },
            }
        ),
        "fn": build_plan,
    },
    {
        "name": "coldlineage_execute_plan",
        "description": (
            "Execute an approved plan. THIS MOVES DATA AND DELETES THE SOURCE ROWS.\n\n"
            "A human is asked to confirm before anything runs; if they decline, this returns "
            "a declined result and nothing happens. The executor re-derives the plan from "
            "live state and refuses if anything drifted since the plan was built, then "
            "archives to Parquet, re-downloads the object and re-verifies its checksum, and "
            "only then deletes — finally writing the archive provenance back into DataHub."
        ),
        "schema": _schema(
            {
                "plan_hash": {
                    "type": "string",
                    "description": "The plan_hash from coldlineage_build_plan.",
                },
                "approved_by": {
                    "type": "string",
                    "description": "Identity of the person approving. Ask the user; do not invent one.",
                },
            }
        ),
        "fn": execute_plan,
    },
    {
        "name": "coldlineage_restore",
        "description": (
            "Rehydrate an archived range. Verifies the stored object's SHA-256 against the "
            "manifest and refuses to restore on mismatch."
        ),
        "schema": _schema(
            {
                "run_id": {
                    "type": "integer",
                    "description": "The run_id returned by coldlineage_execute_plan.",
                },
                "temporary": {
                    "type": "boolean",
                    "description": (
                        "True restores into a side table for inspection; false appends back "
                        "into the source table, making it whole again."
                    ),
                },
            }
        ),
        "fn": restore,
    },
]

BY_NAME: dict[str, Callable[..., Awaitable[str]]] = {t["name"]: t["fn"] for t in TOOLS}


async def dispatch(name: str, arguments: dict[str, Any]) -> str:
    """Run one executor tool by name. Unknown names are refused, not guessed at."""
    fn = BY_NAME.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool {name!r}"})
    try:
        return await fn(**arguments)
    except TypeError as exc:
        return json.dumps({"error": f"bad arguments for {name}: {exc}"})


# ---------------------------------------------------------------------------
# Wire-format conversion
# ---------------------------------------------------------------------------


def as_anthropic_tools() -> list[dict[str, Any]]:
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
        for t in TOOLS
    ]


def as_openai_tools(strict: bool = True) -> list[dict[str, Any]]:
    """OpenAI Responses API function tools are flat — no nested `function` object."""
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["schema"],
            "strict": strict,
        }
        for t in TOOLS
    ]


async def preflight(datahub_gms_url: str) -> bool:
    """Report what is actually reachable. Never claim health we did not observe."""
    ok = True
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            health = (await http.get(f"{COLDLINEAGE_URL}/api/health")).json()
            dh = health.get("datahub", {})
            print(
                f"  ColdLineage : {COLDLINEAGE_URL}  "
                f"(DataHub {dh.get('mode')}, reachable={dh.get('reachable')})"
            )
            if dh.get("mode") == "live" and not dh.get("reachable"):
                print("    ! DataHub is configured live but unreachable — context reads will fail.")
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"  ColdLineage : UNREACHABLE at {COLDLINEAGE_URL} ({type(exc).__name__})")
            ok = False
    return ok
