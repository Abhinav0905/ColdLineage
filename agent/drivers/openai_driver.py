"""OpenAI driver — the Responses API with a hand-written agent loop.

Deliberately built on the plain `openai` SDK rather than `openai-agents`:

  * one dependency instead of a framework;
  * it works against anything that speaks the Responses API, so `OPENAI_BASE_URL`
    points it at Azure OpenAI, vLLM, Ollama, OpenRouter or a self-hosted model
    without touching this file;
  * the loop stays visible, which matters when the interesting part of the
    system is where the loop is *not* allowed to go.

The DataHub MCP session is the same object the Anthropic driver uses. MCP is an
open protocol; only the tool *descriptions* need translating into Responses-API
function tools, and only the dispatch needs routing back. That is the whole
difference between the two drivers.

Compatible endpoints vary wildly in what request parameters they accept, so the
driver negotiates: when a 400 names a parameter, it drops or downgrades exactly
that parameter, remembers the answer, and carries on. Degrading a run beats
ending it, and paying for the same rejection every turn beats nothing at all.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from openai import APIStatusError, AsyncOpenAI, AuthenticationError, NotFoundError

from .. import render
from ..executor import BY_NAME, as_openai_tools, dispatch
from ..mcp_datahub import result_to_text

# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------
# Model lineups move faster than hackathon deadlines, so none of this is
# load-bearing: the default is overridable, and an unreachable model makes the
# driver ask the key what it *can* reach rather than dying on a 404.

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6")

# Best first. Only consulted when the configured model is rejected.
PREFERRED_MODELS = ("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4")

# Never auto-select: non-reasoning, deprecated, or a silently rotating snapshot.
BLOCKED_MODELS = {"chat-latest", "chatgpt-4o-latest", "gpt-4o", "gpt-4o-mini"}
NON_TEXT = ("realtime", "transcribe", "whisper", "tts", "audio", "embedding", "image", "moderation")

MAX_OUTPUT_TOKENS = 16_000

# The SDK's ReasoningEffort literal in 2.53.0; the API rejects anything else.
VALID_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Reported to be a hard 400 on the gpt-5.6 family while remaining valid on older
# models. Clamped rather than hardcoded, because that report is second-hand.
NO_MINIMAL = ("gpt-5.6",)
# Order used when an endpoint rejects the effort we asked for.
EFFORT_LADDER = ("max", "xhigh", "high", "medium", "low", "minimal", "none")

ENV_KEYS = ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY")

_NAME_OK = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def available() -> bool:
    return any(os.environ.get(k) for k in ENV_KEYS)


def credential_help() -> str:
    return (
        "OPENAI_API_KEY is not set.\n"
        "    export OPENAI_API_KEY=sk-...\n"
        "  To use a compatible endpoint instead (Azure, vLLM, Ollama, OpenRouter):\n"
        "    export OPENAI_BASE_URL=https://your-endpoint/v1\n"
        "    export OPENAI_MODEL=<model-id-that-endpoint-serves>"
    )


def clamp_effort(model: str, effort: str) -> str:
    """Keep an effort value the model will accept, without hardcoding a matrix."""
    if effort not in VALID_EFFORTS:
        return "medium"
    if effort == "minimal" and model.startswith(NO_MINIMAL):
        return "low"
    return effort


# ---------------------------------------------------------------------------
# MCP tools -> Responses API function tools
# ---------------------------------------------------------------------------


def _sanitise(name: str) -> str:
    """Responses function names are [a-zA-Z0-9_-]{1,64}; MCP is laxer."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]
    return cleaned or "mcp_tool"


def bridge_mcp_tools(mcp_tools: list[Any]) -> tuple[list[dict], dict[str, str]]:
    """Return (function tool params, wire-name -> real MCP name).

    Two rules worth stating:

    `strict` is False for MCP tools. Their schemas come from a third-party
    server and are not written to OpenAI's strict-mode rules (which demand every
    property be required and additionalProperties be false). Forcing strict here
    would 400 on somebody else's schema. Our own executor tools *are* written to
    those rules, so they keep strict on.

    A catalog tool may not take an executor tool's name. Dispatch resolves
    executor names first, so a colliding MCP tool would be silently unreachable —
    and a catalog that can choose the names of guardrailed operations is a
    catalog that can confuse the guardrail. Refuse it loudly instead.
    """
    params: list[dict] = []
    alias: dict[str, str] = {}
    for tool in mcp_tools:
        wire = _sanitise(tool.name)
        if not _NAME_OK.match(wire):  # pragma: no cover - defensive
            print(f"  ! skipping MCP tool {tool.name!r}: unusable name")
            continue
        if wire in BY_NAME:
            print(f"  ! refusing MCP tool {tool.name!r}: it collides with an executor operation")
            continue
        if wire in alias:
            print(f"  ! refusing MCP tool {tool.name!r}: duplicate wire name {wire!r}")
            continue
        alias[wire] = tool.name
        # mcp 1.x spells it inputSchema; tolerate the snake_case variant too.
        schema = (
            getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None)
            or {"type": "object", "properties": {}}
        )
        params.append(
            {
                "type": "function",
                "name": wire,
                "description": (getattr(tool, "description", "") or "")[:1024],
                "parameters": schema,
                "strict": False,
            }
        )
    return params, alias


# ---------------------------------------------------------------------------
# Endpoint negotiation
# ---------------------------------------------------------------------------


class Wire:
    """What this endpoint has agreed to accept.

    Every rejection is recorded here so it is paid once, not once per turn.
    """

    def __init__(self, model: str, effort: str, mcp_params: list[dict]):
        self.model = model
        self.effort = clamp_effort(model, effort)
        self.mcp_params = mcp_params
        self.reasoning = self.effort != "none"
        self.summary = True
        self.encrypted = True
        self.strict = True

    def tools(self) -> list[dict]:
        return self.mcp_params + as_openai_tools(strict=self.strict)

    def request(self, *, system: str, conversation: list[dict]) -> dict[str, Any]:
        req: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": conversation,
            "tools": self.tools(),
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
        if self.reasoning:
            reasoning: dict[str, Any] = {"effort": self.effort}
            if self.summary:
                reasoning["summary"] = "auto"
            req["reasoning"] = reasoning
        if self.encrypted:
            # store=False keeps the transcript off the server; the encrypted
            # payload is what makes reasoning replayable without it.
            req["include"] = ["reasoning.encrypted_content"]
            req["store"] = False
        return req

    def narrow(self, message: str) -> str | None:
        """Drop or downgrade exactly the parameter a 400 complained about.

        Returns a human-readable note, or None if nothing here explains it — in
        which case the caller re-raises rather than retrying blindly.
        """
        low = message.lower()

        # Most specific first. In particular, an unsupported *effort value* must
        # be read as being about the effort, not about reasoning as a whole —
        # otherwise one bad enum silently disables summaries and continuity for
        # the rest of the run.
        #
        # Note the wording this has to survive. OpenAI's real body reads
        # "Unsupported value: 'max' is not supported with the 'gpt-5.6' model",
        # which never contains the word "effort"; the `param` and `code` fields
        # that do are only present when the SDK stringifies the whole body. So
        # match on the quoted effort value too.
        effort_named = f"'{self.effort}'" in low or f'"{self.effort}"' in low
        if self.reasoning and (
            "effort" in low
            or "unsupported_value" in low
            or "unsupported value" in low
            or effort_named
        ):
            weaker = _weaken(self.effort)
            if weaker:
                self.effort = weaker
                return f"endpoint rejected that effort; retrying at effort={weaker}"
            self.reasoning = False
            return "endpoint rejected every effort value; continuing without reasoning"

        if self.summary and "summary" in low:
            self.summary = False
            return "endpoint rejected reasoning summaries; continuing without them"

        if self.encrypted and ("encrypted" in low or "include" in low or "store" in low):
            self.encrypted = False
            return "endpoint rejected `include`/`store`; continuing without reasoning replay"

        if self.reasoning and "reasoning" in low:
            self.reasoning = False
            return "endpoint does not support `reasoning`; continuing without it"

        if self.strict and ("strict" in low or "additionalproperties" in low or "schema" in low):
            self.strict = False
            return "endpoint rejected strict tool schemas; continuing without strict"

        return None


def _weaken(effort: str) -> str | None:
    try:
        idx = EFFORT_LADDER.index(effort)
    except ValueError:
        return "medium"
    for candidate in EFFORT_LADDER[idx + 1 :]:
        if candidate != "none":
            return candidate
    return None


def _is_model_error(exc: APIStatusError) -> bool:
    """Unknown models come back as 404 from OpenAI and 400 from some gateways."""
    if exc.status_code not in (400, 404):
        return False
    text = str(exc).lower()
    return "model" in text and (
        "not exist" in text
        or "not found" in text
        or "model_not_found" in text
        or "does not have access" in text
        or "unknown model" in text
    )


async def _pick_available_model(client: AsyncOpenAI, current: str) -> tuple[str | None, str]:
    """Ask the key what it can actually reach, and take the best of it.

    `models.list()` returns no capability fields, so selection is against a
    static preference list — never inferred. An endpoint serving only models we
    know nothing about gets a refusal, not a guess: silently driving an unknown
    model through a destructive tool surface is worse than stopping.
    """
    try:
        listing = await client.models.list()
        ids = {m.id for m in listing.data}
    except Exception as exc:  # noqa: BLE001
        return None, f"model {current!r} was rejected, and listing models also failed ({exc})."

    for candidate in PREFERRED_MODELS:
        if candidate in ids and candidate != current:
            return candidate, f"model {current!r} is unavailable; falling back to {candidate!r}."

    guesses = sorted(
        m
        for m in ids
        if m.startswith("gpt-5")
        and m not in BLOCKED_MODELS
        and not any(tag in m for tag in NON_TEXT)
    )
    if guesses:
        return guesses[-1], f"model {current!r} is unavailable; guessing {guesses[-1]!r}."

    preview = "\n    ".join(sorted(ids)[:40]) or "(none returned)"
    return None, (
        f"model {current!r} is not available to this key, and none of the models it can "
        f"reach are known-good for this work.\n"
        f"  Set OPENAI_MODEL explicitly — this key can reach:\n    {preview}"
    )


async def _create(client: AsyncOpenAI, wire: Wire, *, system: str, conversation: list[dict]) -> Any:
    """One turn, negotiating the request down until the endpoint accepts it."""
    while True:
        try:
            return await client.responses.create(**wire.request(system=system, conversation=conversation))
        except APIStatusError as exc:
            if _is_model_error(exc):
                raise
            if exc.status_code != 400:
                raise
            note = wire.narrow(str(exc))
            if note is None:
                raise
            print(f"\n  \033[2m({note})\033[0m")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def _echo(items: list[Any], keep_reasoning: bool) -> list[dict]:
    """Serialise this turn's output items to send back as next turn's input.

    Reasoning items must be round-tripped for the model to keep its chain across
    tool calls, but they are only replayable when the server returned their
    encrypted payload. When it did not, dropping them is the safe move: the model
    loses continuity, the request still succeeds. Echoing a reasoning item with
    no `encrypted_content` is the failure mode this guards against.
    """
    out: list[dict] = []
    for item in items:
        kind = getattr(item, "type", None)
        if kind == "reasoning" and not keep_reasoning:
            continue
        if kind == "reasoning" and not getattr(item, "encrypted_content", None):
            continue
        out.append(item.model_dump(exclude_none=True, mode="json"))
    return out


def _stop_reason(response: Any) -> str:
    status = getattr(response, "status", None) or "completed"
    details = getattr(response, "incomplete_details", None)
    reason = getattr(details, "reason", None) if details else None
    return f"{status}:{reason}" if reason else status


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
    client = AsyncOpenAI(timeout=600.0)
    mcp_params, alias = bridge_mcp_tools(mcp_tools)
    wire = Wire(model or DEFAULT_MODEL, effort, mcp_params)

    conversation: list[dict] = [{"role": "user", "content": question}]
    total_in = total_out = cached = 0
    stop_reason = "completed"

    for _ in range(max_turns):
        try:
            response = await _create(client, wire, system=system, conversation=conversation)
        except AuthenticationError as exc:
            # A stack trace here tells the user nothing they do not already know.
            print(f"\n  OpenAI rejected the credentials: {exc.message}")
            print("  " + credential_help().replace("\n", "\n  "))
            stop_reason = "auth_failed"
            break
        except (NotFoundError, APIStatusError) as exc:
            if not isinstance(exc, NotFoundError) and not _is_model_error(exc):
                raise
            replacement, note = await _pick_available_model(client, wire.model)
            print(f"\n  {note}")
            if replacement is None:
                stop_reason = "model_unavailable"
                break
            wire.model = replacement
            wire.effort = clamp_effort(replacement, wire.effort)
            continue

        if response.usage:
            total_in += response.usage.input_tokens or 0
            total_out += response.usage.output_tokens or 0
            details = getattr(response.usage, "input_tokens_details", None)
            cached += getattr(details, "cached_tokens", 0) or 0

        calls = []
        for item in response.output:
            kind = getattr(item, "type", None)
            if kind == "reasoning":
                for part in getattr(item, "summary", None) or []:
                    render.thinking(getattr(part, "text", "") or "", transcript)
                if wire.encrypted and not getattr(item, "encrypted_content", None):
                    # The endpoint is not returning replayable reasoning; stop
                    # asking for it so we do not echo an unreplayable item back.
                    wire.encrypted = False
            elif kind == "message":
                for part in getattr(item, "content", None) or []:
                    render.say(getattr(part, "text", "") or "", transcript)
            elif kind == "function_call":
                calls.append(item)
                render.tool_use(item.name, item.arguments, transcript)

        conversation.extend(_echo(response.output, keep_reasoning=wire.encrypted))

        if not calls:
            stop_reason = _stop_reason(response)
            break

        # Sequentially, on purpose. `coldlineage_execute_plan` blocks on stdin;
        # running two of those concurrently would race for the same terminal.
        for call in calls:
            try:
                arguments = json.loads(call.arguments or "{}")
            except json.JSONDecodeError as exc:
                output = json.dumps({"error": f"arguments were not valid JSON: {exc}"})
            else:
                output = await _invoke(session, alias, call.name, arguments)
            render.tool_result(call.name, output, transcript)
            conversation.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": output}
            )
    else:
        stop_reason = "max_turns"

    render.usage(
        f"openai/{wire.model}",
        stop_reason=stop_reason,
        **{"in": total_in, "out": total_out, "cached": cached},
    )


async def _invoke(session: Any, alias: dict[str, str], name: str, arguments: dict) -> str:
    """Route one call: our executor, or the DataHub MCP server. Nothing else."""
    if name in BY_NAME:
        return await dispatch(name, arguments)
    real = alias.get(name)
    if real is None:
        return json.dumps({"error": f"unknown tool {name!r}"})
    try:
        result = await session.call_tool(real, arguments)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"MCP call {real} failed: {type(exc).__name__}: {exc}"})
    return result_to_text(result)
