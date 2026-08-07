#!/usr/bin/env python3
"""Offline tests for the agent — no API key, no network, no running stack.

The claim this file defends is the one the whole design rests on: **swapping the
model provider does not change what the agent can do.** If a driver ever grows a
capability the other lacks, or the approval gate stops being provider-neutral,
these tests fail.

    .venv-agent/bin/python agent/test_agent.py
    .venv-agent/bin/python -m pytest agent/test_agent.py -q
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import executor  # noqa: E402
from agent.drivers import PROVIDERS, anthropic_driver, openai_driver  # noqa: E402
from agent.mcp_datahub import resolve_host_url, result_to_text  # noqa: E402
from agent.prompt import system_prompt  # noqa: E402

EXPECTED = {
    "coldlineage_list_datasets",
    "coldlineage_assess_dataset",
    "coldlineage_simulate_cutoff",
    "coldlineage_build_plan",
    "coldlineage_execute_plan",
    "coldlineage_restore",
}


# --- the parity claim ------------------------------------------------------


def test_both_providers_are_offered_identical_tools():
    anthropic_names = {t["name"] for t in executor.as_anthropic_tools()}
    openai_names = {t["name"] for t in executor.as_openai_tools()}
    assert anthropic_names == openai_names == EXPECTED, (
        f"action surface diverged: anthropic={anthropic_names} openai={openai_names}"
    )


def test_schemas_are_identical_across_wire_formats():
    a = {t["name"]: t["input_schema"] for t in executor.as_anthropic_tools()}
    o = {t["name"]: t["parameters"] for t in executor.as_openai_tools()}
    assert a == o, "the same JSON Schema must reach both providers"


def test_descriptions_are_identical_across_wire_formats():
    a = {t["name"]: t["description"] for t in executor.as_anthropic_tools()}
    o = {t["name"]: t["description"] for t in executor.as_openai_tools()}
    assert a == o


def test_every_declared_provider_has_a_driver_module():
    assert set(PROVIDERS) == {"anthropic", "openai"}
    for driver in (anthropic_driver, openai_driver):
        for attr in ("run", "available", "credential_help", "DEFAULT_MODEL"):
            assert hasattr(driver, attr), f"{driver.__name__} is missing {attr}"


# --- schema hygiene --------------------------------------------------------


def test_executor_schemas_satisfy_openai_strict_mode():
    """strict=True is rejected unless every property is required and the object
    is closed. We ship strict, so the schemas must actually comply."""
    for tool in executor.as_openai_tools(strict=True):
        schema = tool["parameters"]
        assert schema["type"] == "object", tool["name"]
        assert schema.get("additionalProperties") is False, tool["name"]
        assert set(schema.get("required", [])) == set(schema["properties"]), (
            f"{tool['name']}: strict mode requires every property to be listed in `required`"
        )


def test_openai_tools_are_flat_not_nested():
    """Responses API function tools are flat; the nested {"function": {...}}
    shape belongs to Chat Completions and 400s here."""
    for tool in executor.as_openai_tools():
        assert tool["type"] == "function"
        assert "function" not in tool
        assert isinstance(tool["name"], str)


def test_anthropic_tools_can_actually_be_constructed():
    tools = anthropic_driver._executor_tools()
    assert {t.name for t in tools} == EXPECTED
    for tool in tools:
        assert tool.input_schema["type"] == "object"


# --- the MCP bridge --------------------------------------------------------


def _fake_mcp_tool(name, schema=None, description="d"):
    return SimpleNamespace(name=name, description=description, inputSchema=schema)


def test_mcp_bridge_translates_and_keeps_a_name_map():
    tools = [
        _fake_mcp_tool("search", {"type": "object", "properties": {"q": {"type": "string"}}}),
        _fake_mcp_tool("get_lineage", None),
    ]
    params, alias = openai_driver.bridge_mcp_tools(tools)
    assert [p["name"] for p in params] == ["search", "get_lineage"]
    assert alias == {"search": "search", "get_lineage": "get_lineage"}
    assert all(p["type"] == "function" for p in params)
    # A missing inputSchema must still produce a valid object schema.
    assert params[1]["parameters"]["type"] == "object"


def test_mcp_tools_are_not_strict():
    """Third-party schemas are not written to OpenAI's strict rules; forcing
    strict on them would 400 on somebody else's JSON Schema."""
    params, _ = openai_driver.bridge_mcp_tools([_fake_mcp_tool("search", None)])
    assert params[0]["strict"] is False


def test_mcp_bridge_sanitises_illegal_names():
    params, alias = openai_driver.bridge_mcp_tools([_fake_mcp_tool("datahub:get lineage!", None)])
    wire = params[0]["name"]
    assert wire == "datahub_get_lineage_"
    assert alias[wire] == "datahub:get lineage!", "the real MCP name must survive for dispatch"


def test_result_to_text_flattens_content_blocks():
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text="one"), SimpleNamespace(text="two")],
    )
    assert result_to_text(result) == "one\ntwo"


def test_result_to_text_falls_back_to_structured_content():
    result = SimpleNamespace(isError=False, content=[], structuredContent={"total": 16})
    assert json.loads(result_to_text(result)) == {"total": 16}


# --- the trust boundary ----------------------------------------------------


def test_declining_the_gate_blocks_execution_and_tells_the_model_to_stop():
    """The gate lives in the neutral executor, so this holds for every provider."""
    executor.set_auto_approve(False)
    called = []

    async def _tripwire(*a, **k):
        called.append(a)
        return "{}"

    original = executor._call
    executor._call = _tripwire
    original_input = __builtins__["input"] if isinstance(__builtins__, dict) else __builtins__.input
    try:
        import builtins

        builtins.input = lambda *_: "no"
        out = json.loads(asyncio.run(executor.execute_plan("abc123", "someone")))
    finally:
        executor._call = original
        import builtins

        builtins.input = original_input

    assert out["declined"] is True
    assert not called, "declining must not reach the ColdLineage API at all"
    assert "Do not retry" in out["message"]


def test_dispatch_refuses_unknown_tools():
    out = json.loads(asyncio.run(executor.dispatch("rm_minus_rf", {})))
    assert "unknown tool" in out["error"]


def test_dispatch_refuses_bad_arguments_without_raising():
    out = json.loads(asyncio.run(executor.dispatch("coldlineage_assess_dataset", {"nope": 1})))
    assert "bad arguments" in out["error"]


def test_executor_imports_no_model_sdk():
    """The security boundary must not depend on any provider."""
    source = (Path(__file__).parent / "executor.py").read_text()
    for banned in ("import anthropic", "import openai", "from anthropic", "from openai"):
        assert banned not in source, f"executor.py must stay provider-neutral, found: {banned}"


def test_one_prompt_serves_both_providers():
    text = system_prompt("http://api", "http://gms")
    assert "http://api" in text and "http://gms" in text
    for vendor in ("Claude", "GPT", "Anthropic", "OpenAI"):
        assert vendor not in text, f"the shared prompt must not name a vendor ({vendor})"


# --- endpoint negotiation --------------------------------------------------
#
# Compatible endpoints reject different parameters. The driver drops exactly
# what was refused and remembers it. These tests pin that behaviour, because
# getting it subtly wrong degrades a run silently rather than loudly.


class _FakeStatusError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status_code = status


def _wire(model="gpt-5.6", effort="high"):
    return openai_driver.Wire(model, effort, mcp_params=[])


def test_minimal_effort_is_clamped_on_models_that_reject_it():
    assert openai_driver.clamp_effort("gpt-5.6-sol", "minimal") == "low"
    assert openai_driver.clamp_effort("gpt-5.5", "minimal") == "minimal"
    assert openai_driver.clamp_effort("gpt-5.6", "high") == "high"
    assert openai_driver.clamp_effort("gpt-5.6", "nonsense") == "medium"


def test_rejected_effort_downgrades_instead_of_killing_reasoning():
    """An unsupported effort value must not cost the whole reasoning block —
    that would silently disable summaries and continuity for the entire run."""
    wire = _wire(effort="max")
    note = wire.narrow("Unsupported value: 'max' is not supported with the 'gpt-5.6' model")
    assert note is not None
    assert wire.reasoning is True, "reasoning must survive an effort rejection"
    assert wire.effort == "xhigh", f"expected a downgrade, got {wire.effort}"


def test_effort_ladder_bottoms_out_then_disables_reasoning():
    wire = _wire(effort="low")
    wire.narrow("unsupported_value: effort")  # low -> minimal
    assert wire.effort == "minimal" and wire.reasoning
    note = wire.narrow("unsupported_value: effort")  # nothing weaker that is useful
    assert wire.reasoning is False and "without reasoning" in note


def test_strict_rejection_actually_changes_the_tools_sent():
    """Popping a top-level `strict` key would be a no-op — strict lives inside
    each tool dict, so a 'retry' would re-send byte-identical JSON."""
    wire = _wire()
    before = wire.request(system="s", conversation=[])["tools"]
    assert all(t["strict"] for t in before if t["name"] in EXPECTED)
    wire.narrow("Invalid schema: 'strict' is not supported")
    after = wire.request(system="s", conversation=[])["tools"]
    assert not any(t["strict"] for t in after), "the retry must actually differ"


def test_include_rejection_drops_both_include_and_store():
    wire = _wire()
    assert "include" in wire.request(system="s", conversation=[])
    wire.narrow("Unknown parameter: 'include'.")
    request = wire.request(system="s", conversation=[])
    assert "include" not in request and "store" not in request


def test_negotiated_state_persists_so_a_rejection_is_paid_once():
    """Rebuilding the request fresh each turn would re-send a known-bad
    parameter and burn a 400 round-trip per turn."""
    wire = _wire()
    wire.narrow("Unknown parameter: 'include'.")
    for _ in range(3):
        assert "include" not in wire.request(system="s", conversation=[])


def test_unrecognised_400_is_not_retried_blindly():
    assert _wire().narrow("your account has insufficient quota") is None


def test_model_errors_are_recognised_on_both_400_and_404():
    for status in (400, 404):
        assert openai_driver._is_model_error(
            _FakeStatusError(status, "The model 'gpt-9' does not exist")
        ), f"gateways return {status} for unknown models"
    assert not openai_driver._is_model_error(_FakeStatusError(400, "invalid schema for tool"))
    assert not openai_driver._is_model_error(_FakeStatusError(500, "model overloaded"))


def test_stop_reason_keeps_the_incomplete_detail():
    response = SimpleNamespace(
        status="incomplete", incomplete_details=SimpleNamespace(reason="max_output_tokens")
    )
    assert openai_driver._stop_reason(response) == "incomplete:max_output_tokens"
    assert openai_driver._stop_reason(SimpleNamespace(status="completed", incomplete_details=None)) == "completed"


# --- the project's own .env is written for containers ----------------------


def test_container_only_hostname_is_rewritten_for_the_host():
    """`.env` sets DATAHUB_GMS_URL=http://host.docker.internal:8090, which is
    right inside a container and unresolvable on the host — where the agent
    runs. Sourcing the project's own .env used to kill the MCP subprocess."""
    url, note = resolve_host_url("http://host.docker.internal:8090")
    assert url == "http://localhost:8090"
    assert note and "container-only" in note


def test_resolvable_hostnames_are_left_alone():
    for url in ("http://localhost:8090", "http://127.0.0.1:8000"):
        assert resolve_host_url(url) == (url, None)


def test_an_unresolvable_host_is_reported_not_silently_rewritten():
    url, note = resolve_host_url("http://no-such-host.invalid:8090")
    assert url == "http://no-such-host.invalid:8090", "only the docker name is safe to rewrite"
    assert note and "does not resolve" in note


# --- the catalog must not be able to name a guardrailed operation ----------


def test_an_mcp_tool_cannot_take_an_executor_name():
    """Dispatch resolves executor names first, so a colliding MCP tool would be
    silently unreachable — and a catalog that picks the names of guardrailed
    operations is a catalog that can confuse the guardrail."""
    params, alias = openai_driver.bridge_mcp_tools(
        [_fake_mcp_tool("coldlineage_execute_plan", None), _fake_mcp_tool("search", None)]
    )
    assert [p["name"] for p in params] == ["search"]
    assert "coldlineage_execute_plan" not in alias


def test_duplicate_mcp_wire_names_are_refused():
    params, _ = openai_driver.bridge_mcp_tools(
        [_fake_mcp_tool("get lineage", None), _fake_mcp_tool("get:lineage", None)]
    )
    assert len(params) == 1, "two MCP names sanitising to one wire name must not both register"


def test_snake_case_input_schema_is_tolerated():
    tool = SimpleNamespace(
        name="search", description="d", inputSchema=None,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    params, _ = openai_driver.bridge_mcp_tools([tool])
    assert params[0]["parameters"]["properties"] == {"q": {"type": "string"}}


# --- runner ----------------------------------------------------------------


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
