# The ColdLineage agent

An agent that **reads DataHub through the official MCP Server** and acts through a
constrained executor, with a human between the plan and the delete.

```
Claude (claude-opus-5, adaptive thinking, effort=high)
  │
  ├── reads ──▶ mcp-server-datahub  (stdio subprocess, read-only)
  │               search · get_lineage · get_dataset_queries
  │               get_entities · list_schema_fields · get_lineage_paths_between
  │
  └── acts ──▶ ColdLineage executor  (5 HTTP operations, nothing else)
                  list · assess · simulate · plan · execute* · restore
                                                    └── * blocks on a human
```

## Why the tool list is the security model

The agent has **no database credentials, no object-store client, and no ability to
issue SQL.** It has read-only MCP tools and five HTTP operations. Whatever the model
decides to do, the set of things it *can* do is fixed by [that tool
list](coldlineage_agent.py) — which is a far stronger guarantee than any instruction
in a system prompt, and it holds even if the model is wrong or the prompt is attacked.

The DataHub MCP server is started with mutation tools **off** (its default), so the
agent physically cannot write to the catalog on its own. Provenance writeback happens
only inside `/api/execute`, after the archive has been verified.

`coldlineage_execute_plan` prints the plan and blocks on `input()`. Decline and the
tool returns a declined result telling the model to stop — it does not get to retry
or route around it.

## Run it

```bash
python3.11 -m venv .venv-agent
.venv-agent/bin/pip install -r agent/requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
export COLDLINEAGE_URL=http://localhost:8000     # default
export DATAHUB_GMS_URL=http://localhost:8090     # default

.venv-agent/bin/python agent/coldlineage_agent.py \
  "Can we archive anything from patient_encounters? Show me the evidence."
```

A separate virtualenv is deliberate: `anthropic[mcp]` and the backend disagree on
pinned `pydantic` and `starlette` versions, and the agent is a client of the API
rather than part of it.

## Questions worth asking it

| Prompt | What it exercises |
|---|---|
| *"Which datasets have an archivable historical range?"* | Surveys the estate, ranks candidates |
| *"Can we archive anything from patient_encounters?"* | The hero case — finds a safe cutoff on a HOT table |
| *"lab_results looks cold. Can we archive it?"* | **The killer case.** It should refuse, and explain the unbounded HIPAA extract |
| *"Archive claims_history before 2020."* | Should refuse on the ACTIVE legal hold, without proposing a workaround |
| *"What's the most aggressive cutoff for billing_ledger?"* | Binary-searches cutoffs, then hits the 7-year retention floor |

## Flags

| Flag | |
|---|---|
| `--auto-approve` | Skips the human gate. **Testing only** — never for a demo; the gate is the point |
| `--save-transcript PATH` | Writes the run as JSON |
| `--max-turns N` | Caps tool-calling iterations (default 40) |

## Model configuration

`claude-opus-5` with adaptive thinking (`display: "summarized"`, so reasoning is
visible on camera) and `effort: "high"` — this is multi-step agentic work where the
model chains catalog reads into a cutoff search, and lower effort under-explores.

## Relationship to the DataHub Skill

[`skills/assess-data-temperature/`](../skills/assess-data-temperature/) encodes the
same decision procedure for a **skills runtime** (Claude Code, Cursor, and anything
else that speaks the format), driving the `datahub` CLI. This directory is the
**standalone** agent: same procedure, same executor, MCP instead of the CLI, and no
runtime required beyond Python. Ship both because they serve different users — one
for people already living in an agent harness, one for people who want to run it.
