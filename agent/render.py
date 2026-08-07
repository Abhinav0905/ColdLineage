"""Shared terminal rendering, so both providers produce a comparable transcript.

If Claude's run and GPT's run were formatted differently, side-by-side review
would be about the formatting. Same renderer, same shapes, same colours.
"""

from __future__ import annotations

import json
from typing import Any

DIM = "\033[2m"
CYAN = "\033[36m"
BOLD = "\033[1m"
RESET = "\033[0m"


def thinking(text: str, transcript: list[dict]) -> None:
    if not text or not text.strip():
        return
    print(f"\n{DIM}[thinking] {text}{RESET}")
    transcript.append({"type": "thinking", "text": text})


def say(text: str, transcript: list[dict]) -> None:
    if not text or not text.strip():
        return
    print(f"\n{text}")
    transcript.append({"type": "text", "text": text})


def tool_use(name: str, arguments: Any, transcript: list[dict]) -> None:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments, default=str)
    shown = args if len(args) <= 160 else args[:157] + "..."
    print(f"\n{CYAN}  -> {name}({shown}){RESET}")
    transcript.append({"type": "tool_use", "name": name, "input": arguments})


def tool_result(name: str, output: str, transcript: list[dict]) -> None:
    transcript.append({"type": "tool_result", "name": name, "output": output})


def usage(label: str, **fields: Any) -> None:
    parts = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    print("\n" + "-" * 72)
    print(f"  {label}  {parts}")
