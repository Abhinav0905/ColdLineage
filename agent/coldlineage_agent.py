#!/usr/bin/env python3
"""ColdLineage agent — DataHub over MCP, a constrained executor, a human gate.

    reads   ->  DataHub MCP Server (mcp-server-datahub), read-only
    acts    ->  six ColdLineage operations: list, assess, simulate, plan, execute, restore
    gate    ->  a human types "approve" between plan and execute

The trust boundary is the point of this program. The model never receives
database credentials, an S3 client, or the ability to issue SQL. It receives
read-only MCP tools plus six HTTP operations, one of which blocks on a human.
Whatever the model decides, the set of things it *can* do is fixed by
`agent/executor.py` -- a stronger guarantee than any instruction in a prompt.

That guarantee is why the model is swappable. Claude and GPT get the same
prompt, the same tools and the same executor; pick with --provider, or let it
choose whichever key is set.

Run:
    export OPENAI_API_KEY=sk-...        # or ANTHROPIC_API_KEY=sk-ant-...
    .venv-agent/bin/python agent/coldlineage_agent.py \\
        "Can we archive anything from patient_encounters?"

Requires the ColdLineage API (default http://localhost:8000) and a DataHub GMS
(default http://localhost:8090). Both are read from the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

# Support both `python agent/coldlineage_agent.py` and `python -m agent.coldlineage_agent`.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import executor  # noqa: E402
from agent.mcp_datahub import _GMS_NOTE, DATAHUB_GMS_URL, datahub_mcp, resolve_host_url  # noqa: E402
from agent.prompt import DEFAULT_QUESTION, system_prompt  # noqa: E402

# Same container-only-hostname trap as the GMS URL; see resolve_host_url.
executor.COLDLINEAGE_URL, _API_NOTE = resolve_host_url(executor.COLDLINEAGE_URL)

PROVIDERS = {
    "anthropic": "agent.drivers.anthropic_driver",
    "openai": "agent.drivers.openai_driver",
}


def _load(name: str):
    try:
        return importlib.import_module(PROVIDERS[name])
    except ImportError as exc:
        sys.exit(
            f"provider {name!r} is not installed: {exc}\n"
            "Install the agent dependencies:\n"
            "    python3.11 -m venv .venv-agent\n"
            "    .venv-agent/bin/pip install -r agent/requirements.txt"
        )


def _pick_provider(requested: str) -> tuple[str, object]:
    """Resolve --provider, honouring an explicit choice and auto-detecting otherwise."""
    if requested != "auto":
        driver = _load(requested)
        if not driver.available():
            sys.exit(driver.credential_help())
        return requested, driver

    ready = []
    helps = []
    for name in PROVIDERS:
        driver = _load(name)
        helps.append(driver.credential_help())
        if driver.available():
            ready.append((name, driver))

    if not ready:
        sys.exit(
            "No model provider credentials found. Set one of:\n\n  "
            + "\n\n  ".join(helps)
            + "\n\nOr pick explicitly with --provider {anthropic,openai}."
        )
    if len(ready) > 1:
        names = ", ".join(n for n, _ in ready)
        print(f"  note        : keys found for {names}; using {ready[0][0]} (--provider to override)")
    return ready[0]


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="ColdLineage agent (DataHub MCP + constrained executor)"
    )
    parser.add_argument("prompt", nargs="*", help="What to ask the agent.")
    parser.add_argument(
        "--provider",
        choices=["auto", *PROVIDERS],
        default=os.environ.get("COLDLINEAGE_PROVIDER", "auto"),
        help="Which model provider drives the agent. Default: whichever key is set.",
    )
    parser.add_argument("--model", help="Override the provider's default model id.")
    parser.add_argument(
        "--effort",
        default="high",
        help="Reasoning effort. This work is multi-step; lower values under-explore.",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip the human gate. For automated testing only — never for a demo.",
    )
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--save-transcript", metavar="PATH", help="Write the transcript as JSON.")
    args = parser.parse_args()

    executor.set_auto_approve(args.auto_approve)
    question = " ".join(args.prompt) or DEFAULT_QUESTION

    provider, driver = _pick_provider(args.provider)
    model = args.model or getattr(driver, "DEFAULT_MODEL", None)

    print("\033[1mColdLineage agent\033[0m")
    print(f"  provider    : {provider} ({model}, effort={args.effort})")
    for note in (_GMS_NOTE, _API_NOTE):
        if note:
            print(f"  note        : {note}")
    if not await executor.preflight(DATAHUB_GMS_URL):
        print("\n  Preflight failed. Start the stack first (see README) and retry.")
        return 1

    system = system_prompt(executor.COLDLINEAGE_URL, DATAHUB_GMS_URL)
    transcript: list[dict] = [
        {"type": "question", "text": question, "provider": provider, "model": model}
    ]

    try:
        async with datahub_mcp(DATAHUB_GMS_URL) as (session, mcp_tools):
            names = ", ".join(t.name for t in mcp_tools[:6])
            print(
                f"  DataHub MCP : {len(mcp_tools)} tools — {names}"
                f"{' ...' if len(mcp_tools) > 6 else ''}"
            )
            print(
                f"  executor    : {len(executor.TOOLS)} constrained operations "
                "(human gate on execute)"
            )
            print(f"\n\033[1m> {question}\033[0m")

            await driver.run(
                session=session,
                mcp_tools=mcp_tools,
                question=question,
                system=system,
                model=args.model,
                effort=args.effort,
                max_turns=args.max_turns,
                transcript=transcript,
            )
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2

    if args.save_transcript:
        Path(args.save_transcript).parent.mkdir(parents=True, exist_ok=True)
        with open(args.save_transcript, "w") as fh:
            json.dump(
                {
                    "question": question,
                    "provider": provider,
                    "model": model,
                    "effort": args.effort,
                    "transcript": transcript,
                },
                fh,
                indent=2,
                default=str,
            )
        print(f"  transcript -> {args.save_transcript}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130) from None
