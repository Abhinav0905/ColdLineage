#!/usr/bin/env python3
"""End-to-end test of the OpenAI driver's agent loop, with no API key.

A scripted OpenAI-compatible server stands in for the model. Everything else is
the real thing: the real driver, the real MCP bridge, the real executor dispatch,
the real human-approval gate. The mock records every request it receives, so the
test can assert on what the driver actually put on the wire — which is the part
that is easy to get wrong and impossible to check by reading.

What this proves, and what it does not: the loop is correct — tool calls are
parsed, results are returned in the shape the Responses API expects, reasoning
items round-trip, and the approval gate stops a delete. It does not prove any
particular OpenAI model behaves well; only a key can show that.

    .venv-agent/bin/python agent/test_loop_integration.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import executor  # noqa: E402
from agent.drivers import openai_driver  # noqa: E402

# --- the scripted model ----------------------------------------------------


def _fc(idx: int, name: str, arguments: dict) -> dict:
    return {
        "type": "function_call",
        "id": f"fc_{idx}",
        "call_id": f"call_{idx}",
        "name": name,
        "arguments": json.dumps(arguments),
        "status": "completed",
    }


def _msg(text: str) -> dict:
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _reasoning(text: str, encrypted: str | None) -> dict:
    item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": text}],
    }
    if encrypted:
        item["encrypted_content"] = encrypted
    return item


def _response(output: list[dict]) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1_754_400_000,
        "status": "completed",
        "model": "scripted-model",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 5},
            "total_tokens": 120,
        },
    }


SCRIPT = [
    # turn 1: think, then read the catalog through MCP
    _response([_reasoning("Checking what the catalog says.", "enc-abc"),
               _fc(1, "get_dataset_queries", {"urn": "urn:li:dataset:x"})]),
    # turn 2: two tool calls in one turn, one MCP and one executor
    _response([_fc(2, "coldlineage_list_datasets", {}),
               _fc(3, "coldlineage_simulate_cutoff", {"dataset_id": 1, "cutoff_date": "2021-01-01"})]),
    # turn 3: try to delete — the gate must stop this
    _response([_fc(4, "coldlineage_execute_plan", {"plan_hash": "deadbeef", "approved_by": "tester"})]),
    # turn 4: give up gracefully
    _response([_msg("Declined, so nothing moved. The binding consumer is the HIPAA extract.")]),
]


class _Handler(BaseHTTPRequestHandler):
    received: list[dict] = []
    turn = 0

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or "{}")
        type(self).received.append(body)
        payload = SCRIPT[min(type(self).turn, len(SCRIPT) - 1)]
        type(self).turn += 1
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_):  # silence
        pass


# --- fakes for the two things we are not testing here ----------------------


class _FakeMCPSession:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(text=json.dumps({"queries": ["SELECT 1"]}))],
        )


MCP_TOOLS = [
    SimpleNamespace(
        name="get_dataset_queries",
        description="Get the SQL queries recorded against a dataset.",
        inputSchema={"type": "object", "properties": {"urn": {"type": "string"}}},
    ),
    SimpleNamespace(name="search", description="Search the catalog.", inputSchema=None),
]


def run_loop():
    """Drive the real loop against the scripted model; return everything observed."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    import openai as openai_pkg

    session = _FakeMCPSession()
    transcript: list[dict] = []
    api_calls: list[tuple] = []

    async def _fake_call(method, path, payload=None):
        api_calls.append((method, path, payload))
        return json.dumps({"ok": True, "path": path})

    original_call = executor._call
    original_client = openai_driver.AsyncOpenAI
    executor._call = _fake_call
    executor.set_auto_approve(False)

    import builtins

    original_input = builtins.input
    builtins.input = lambda *_: "no"  # decline the delete

    def _client(**kwargs):
        return openai_pkg.AsyncOpenAI(api_key="test", base_url=f"http://127.0.0.1:{port}/v1", **kwargs)

    openai_driver.AsyncOpenAI = _client
    try:
        asyncio.run(
            openai_driver.run(
                session=session,
                mcp_tools=MCP_TOOLS,
                question="Can we archive lab_results?",
                system="You are a test.",
                model="scripted-model",
                effort="high",
                max_turns=10,
                transcript=transcript,
            )
        )
    finally:
        executor._call = original_call
        openai_driver.AsyncOpenAI = original_client
        builtins.input = original_input
        server.shutdown()

    return SimpleNamespace(
        requests=_Handler.received,
        transcript=transcript,
        mcp_calls=session.calls,
        api_calls=api_calls,
    )


RESULT = run_loop()


# --- assertions ------------------------------------------------------------


def test_loop_ran_every_scripted_turn():
    assert len(RESULT.requests) == 4, f"expected 4 model turns, got {len(RESULT.requests)}"


def test_tools_offered_include_both_mcp_and_executor():
    tools = RESULT.requests[0]["tools"]
    names = {t["name"] for t in tools}
    assert "get_dataset_queries" in names, "MCP tools were not bridged"
    assert "coldlineage_simulate_cutoff" in names, "executor tools were not offered"
    assert len(names) == len(MCP_TOOLS) + len(executor.TOOLS)


def test_mcp_tool_call_reached_the_mcp_session():
    assert RESULT.mcp_calls == [("get_dataset_queries", {"urn": "urn:li:dataset:x"})]


def test_executor_calls_reached_the_coldlineage_api():
    paths = [c[1] for c in RESULT.api_calls]
    assert "/api/datasets" in paths
    assert "/api/datasets/1/simulate" in paths


def test_tool_results_are_returned_in_responses_api_shape():
    """`function_call_output` with a matching `call_id` — the shape the API
    requires, and the one a Chat-Completions habit gets wrong."""
    second = RESULT.requests[1]["input"]
    outputs = [i for i in second if i.get("type") == "function_call_output"]
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == "call_1"
    assert isinstance(outputs[0]["output"], str), "output must be a string, not an object"
    assert json.loads(outputs[0]["output"])["queries"] == ["SELECT 1"]


def test_parallel_tool_calls_in_one_turn_are_all_answered():
    third = RESULT.requests[2]["input"]
    ids = {i["call_id"] for i in third if i.get("type") == "function_call_output"}
    assert {"call_2", "call_3"} <= ids, "both calls from a single turn must be answered"


def test_reasoning_items_round_trip_when_encrypted():
    second = RESULT.requests[1]["input"]
    reasoning = [i for i in second if i.get("type") == "reasoning"]
    assert len(reasoning) == 1, "an encrypted reasoning item must be echoed back"
    assert reasoning[0]["encrypted_content"] == "enc-abc"


def test_conversation_accumulates_rather_than_resetting():
    lengths = [len(r["input"]) for r in RESULT.requests]
    assert lengths == sorted(lengths) and lengths[0] < lengths[-1], (
        f"input must grow across turns, got {lengths}"
    )


def test_the_gate_stopped_the_delete():
    executed = [c for c in RESULT.api_calls if c[1] == "/api/execute"]
    assert not executed, "declining the gate must never reach /api/execute"


def test_the_model_was_told_the_decline_was_final():
    fourth = RESULT.requests[3]["input"]
    declines = [
        i for i in fourth
        if i.get("type") == "function_call_output" and "declined" in str(i.get("output"))
    ]
    assert declines, "the decline must be reported back to the model"
    assert "Do not retry" in declines[0]["output"]


def test_reasoning_summary_and_final_text_reach_the_transcript():
    kinds = [e["type"] for e in RESULT.transcript]
    assert "thinking" in kinds, "reasoning summaries should be rendered"
    assert "text" in kinds
    final = [e for e in RESULT.transcript if e["type"] == "text"][-1]
    assert "nothing moved" in final["text"]


def test_effort_and_instructions_were_sent():
    first = RESULT.requests[0]
    assert first["reasoning"]["effort"] == "high"
    assert first["instructions"] == "You are a test."


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}\n      {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}\n      {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
