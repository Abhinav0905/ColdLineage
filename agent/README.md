# The ColdLineage agent

An agent that **reads DataHub through the official MCP Server** and acts through a
constrained executor, with a human between the plan and the delete.

It runs on **Claude or GPT**, and that is not a checkbox — it is the argument. If the
safety of a system depends on which model you plugged in, it was never safe. Here the
guardrail is the tool list, so the model is the swappable part.

```
   Claude (Anthropic)          GPT (OpenAI)          ← swap freely
           │                        │
           └───────── one ──────────┘
                  system prompt
                  tool schema
                  executor
                      │
        ┌─────────────┴──────────────┐
        │                            │
   reads via MCP                acts via HTTP
        │                            │
 mcp-server-datahub          ColdLineage executor
 (stdio, read-only)          list · assess · simulate
 search · get_lineage        plan · execute* · restore
 get_dataset_queries                        └── * blocks on a human
 get_entities · list_schema_fields
 get_lineage_paths_between
```

## Why the tool list is the security model

The agent has **no database credentials, no object-store client, and no ability to
issue SQL.** It has read-only MCP tools and six HTTP operations. Whatever the model
decides to do, the set of things it *can* do is fixed by
[`executor.py`](executor.py) — a far stronger guarantee than any instruction in a
system prompt, and it holds even if the model is wrong or the prompt is attacked.

Three properties make that claim checkable rather than rhetorical:

- **The executor imports no model SDK.** Enforced by a test that greps it.
- **Both providers are handed identical tools.** One JSON Schema definition is
  mechanically converted to Anthropic's `input_schema` and OpenAI's `parameters`;
  a test asserts the two sets are equal. A provider cannot quietly gain a capability.
- **The approval gate lives in the neutral executor**, not in a provider's tool
  wrapper — so there is no code path to the delete that routes around it. Decline,
  and the tool returns a declined result telling the model to stop.

The DataHub MCP server is started with mutation tools **off** (its default), so the
agent physically cannot write to the catalog on its own. Provenance writeback happens
only inside `/api/execute`, after the archive has been verified.

## Run it

```bash
python3.11 -m venv .venv-agent
.venv-agent/bin/pip install -r agent/requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY=sk-...
export COLDLINEAGE_URL=http://localhost:8000     # default
export DATAHUB_GMS_URL=http://localhost:8090     # default

.venv-agent/bin/python agent/coldlineage_agent.py \
  "Can we archive anything from patient_encounters? Show me the evidence."
```

The provider is auto-detected from whichever key is set. Force one with
`--provider anthropic` or `--provider openai`.

A separate virtualenv is deliberate: the model SDKs and the backend disagree on
pinned `pydantic` and `starlette` versions, and the agent is a client of the API
rather than part of it.

### Neither vendor

The OpenAI driver is built on the plain Responses API rather than a framework, so
anything that speaks that API works — Azure OpenAI, vLLM, Ollama, OpenRouter, or a
model on your own hardware:

```bash
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_MODEL=<whatever that endpoint serves>
export OPENAI_API_KEY=unused          # most local servers ignore it but the SDK wants it
```

Compatible endpoints often reject `reasoning`, `include` or `store`. The driver
retries without whichever parameter was refused rather than failing the run.

## Questions worth asking it

| Prompt | What it exercises |
|---|---|
| *"Which datasets have an archivable historical range?"* | Surveys the estate, ranks candidates |
| *"Can we archive anything from patient_encounters?"* | The hero case — finds a safe cutoff on a HOT table |
| *"lab_results looks cold. Can we archive it?"* | **The killer case.** It should refuse, and explain the unbounded HIPAA extract |
| *"Archive claims_history before 2020."* | Should refuse on the ACTIVE legal hold, without proposing a workaround |
| *"What's the most aggressive cutoff for billing_ledger?"* | Searches cutoffs, then hits the 7-year retention floor |

Running the same prompt under both providers is a legitimate experiment, because the
prompt and the tools are genuinely identical:

```bash
for p in anthropic openai; do
  .venv-agent/bin/python agent/coldlineage_agent.py --provider $p \
    --save-transcript examples/agent-$p.json \
    "lab_results looks cold. Can we archive it?"
done
```

## Flags

| Flag | |
|---|---|
| `--provider {auto,anthropic,openai}` | Which model drives it. Default: whichever key is set |
| `--model ID` | Override the provider's default model |
| `--effort LEVEL` | Reasoning effort, default `high` |
| `--auto-approve` | Skips the human gate. **Testing only** — never for a demo; the gate is the point |
| `--save-transcript PATH` | Writes the run as JSON |
| `--max-turns N` | Caps tool-calling iterations (default 40) |

## Tests

Both suites run offline — no API key, no network, no running stack.

```bash
.venv-agent/bin/python agent/test_agent.py             # 32 tests: parity, negotiation, trust boundary
.venv-agent/bin/python agent/test_loop_integration.py  # 12 tests: the OpenAI loop, end to end
```

`test_loop_integration.py` stands a scripted OpenAI-compatible server in for the
model and runs the **real** driver, MCP bridge, executor dispatch and approval gate
against it, then asserts on what the driver actually put on the wire — that parallel
tool calls are all answered, that results come back as `function_call_output` with
matching `call_id`, that reasoning items round-trip, and that declining the gate
never reaches `/api/execute`.

What that proves is the loop. It does not prove any particular model behaves well on
this task; only a key can show that.

### What has and has not touched a live API

Being precise about this, because "provider-agnostic" is easy to claim and easy to fake:

| | |
|---|---|
| Verified against live DataHub | The MCP handshake, `list_tools`, and catalog reads |
| Verified against the live OpenAI API | The model catalogue, effort validation, and that a run reaches the provider and fails only on billing |
| Verified against a scripted endpoint | The whole OpenAI loop — tool calls, results, reasoning replay, the gate |
| Verified against the installed SDKs | Every request shape, from the generated types on disk |
| **Not yet verified** | **That a real model completes this task well** — the available key had no credits |

Two guesses were wrong and are now corrected from live responses:

- **There is no `gpt-5.6` alias.** `models.list()` serves `gpt-5.6-sol`, `-terra` and `-luna` and no
  bare alias, so the default is the concrete `gpt-5.6-sol`. Defaulting to the alias would have 404'd
  into the fallback ladder and still worked, which is exactly what the ladder is for — but a wasted
  round-trip for a name that does not exist is not a design.
- **`effort: "minimal"` is a hard 400 on that family**, answered with *"Unsupported value: 'minimal'
  is not supported with the 'gpt-5.6-sol' model. Supported values are: 'none', 'low', 'medium',
  'high', 'xhigh', and 'max'."* Note it never says "effort" — so the negotiator matches the quoted
  value too. `clamp_effort` means asking for `minimal` downgrades rather than fails.

Still unconfirmed, each with a coded fallback: that `include=["reasoning.encrypted_content"]` with
`store=False` returns populated `encrypted_content`, and that `mcp-server-datahub`'s real schemas
are accepted with `strict: false`. A fallback firing is not the same as a guess being right.

## Layout

| | |
|---|---|
| `executor.py` | The six operations, their schemas, the approval gate. **No model SDK.** |
| `prompt.py` | The system prompt — one text, both providers |
| `mcp_datahub.py` | The DataHub MCP stdio session |
| `render.py` | Terminal + transcript output, shared so runs are comparable |
| `drivers/anthropic_driver.py` | Claude via the SDK's tool runner |
| `drivers/openai_driver.py` | GPT via the Responses API and a hand-written loop |
| `coldlineage_agent.py` | CLI, provider detection, preflight |

Adding a provider means adding one file under `drivers/`. If it needs anything from
outside `executor.py` to do its job, that is a finding, not a feature.

## Model configuration

Anthropic runs `claude-opus-5` with adaptive thinking (`display: "summarized"`, so
reasoning is visible on camera) and `effort: "high"`. OpenAI runs `gpt-5.6` with
`reasoning: {effort: "high", summary: "auto"}`. This is multi-step agentic work where
the model chains catalog reads into a cutoff search, and lower effort under-explores.

Override with `--model`, `ANTHROPIC_MODEL` or `OPENAI_MODEL`. Model lineups move faster
than hackathon deadlines, so nothing here is load-bearing: if the default has aged out,
the OpenAI driver catches the rejection — 404 from OpenAI, 400 from some gateways — asks
`models.list()` what the key can actually reach, and retries on the best known-good match.
If it recognises nothing in that list it stops and says so rather than picking something
at random, because silently driving an unknown model through a destructive tool surface
is worse than failing.

The same instinct covers request parameters. Compatible endpoints reject different things;
when a 400 names one, the driver drops or downgrades exactly that parameter and remembers
the answer for the rest of the run. An unsupported `effort` value downgrades the effort —
it does not silently discard reasoning altogether, which is the tempting shortcut and
costs you summaries and continuity for every remaining turn.

## Relationship to the DataHub Skill

[`skills/assess-data-temperature/`](../skills/assess-data-temperature/) encodes the
same decision procedure for a **skills runtime** (Claude Code, Cursor, and anything
else that speaks the format), driving the `datahub` CLI. This directory is the
**standalone** agent: same procedure, same executor, MCP instead of the CLI, and no
runtime required beyond Python. Ship both because they serve different users — one
for people already living in an agent harness, one for people who want to run it.
