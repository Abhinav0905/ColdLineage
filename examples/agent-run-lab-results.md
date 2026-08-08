# A live agent run: the agent says no

Captured verbatim from `agent/coldlineage_agent.py` against a live DataHub OSS v1.7.0
and a live ColdLineage API. Reproduce with:

```bash
export OPENAI_API_KEY=sk-...          # or ANTHROPIC_API_KEY, --provider anthropic
.venv-agent/bin/python agent/coldlineage_agent.py \
  "lab_results looks cold. Can we archive it?"
```

| | |
|---|---|
| provider | `openai` |
| model | `gpt-5.6-sol` |
| reasoning effort | `high` |

## Why this is the run worth reading

`lab_results` is **stone cold by every table-level signal**: temperature score 10.8, zero
queries and zero distinct users in the last 30 days. Every dataset-level tool on the market
archives it. The agent refuses, and refuses for the right reason — one HIPAA disclosure
extract reads it with no date predicate at all, so not one row can be *proved* unread.

It also chose the cutoff to test on its own (`2023-08-07`, the retention-compliant date),
reported the other consumer's 1,006 days of headroom, and said exactly what would have to
change for the answer to become yes. Nobody scripted any of that.

## The transcript

### The question

> lab_results looks cold. Can we archive it?

<details><summary><b>reasoning</b> (summarised by the model)</summary>

```
**Inspecting datasets for archiving**

I need to inspect datasets to figure out the specific named tables. I think searching
the Coldlineage list might help, and maybe using DataHub for parallel searches as well.
The user is asking if we can archive these datasets, so first, I should identify them
before moving forward. I'll need to call up the list and conduct the requisite searches
to assess what we have.
```

</details>

**1. `coldlineage_list_datasets`**  —  via the ColdLineage executor

```json
{}
```

**2. `search`**  —  via the DataHub MCP Server

```json
{"query": "/q lab+results", "filter": "entity_type = dataset", "num_results": 10}
```

<details><summary><b>reasoning</b> (summarised by the model)</summary>

```
**Assessing archiving constraints**

It looks like the results indicate I can't archive any cutoff due to unbounded issues. I
need to evaluate all the evidence and the constraints involved. The hard rule here
suggests that it's not safe to proceed without further checks. The user is asking if
archiving is possible, so I think I should take a closer look at these factors to
provide a clear answer. It's definitely important to be careful!
```

</details>

**3. `coldlineage_assess_dataset`**  —  via the ColdLineage executor

```json
{"dataset_id": 4}
```

<details><summary><b>reasoning</b> (summarised by the model)</summary>

```
**Considering data retention**

The user asked about archiving and how to report the exact headroom, possibly by
simulating a candidate from August 7, 2023. The retention policy indicates a cutoff on
or before that date. I think simulating that cutoff might help quantify the number of
rows. However, it seems like the simulation might not yield any rows. So, I really need
to be careful not to build something unsafe while trying to get an accurate result and
plan.
```

</details>

**4. `coldlineage_simulate_cutoff`**  —  via the ColdLineage executor

```json
{"dataset_id": 4, "cutoff_date": "2023-08-07"}
```

### The answer

**No—`lab_results` cannot currently be archived safely.**

- **Recommended cutoff:** None
- **Rows approved to move:** 0 of 700,000
- **Binding consumer:** `hipaa_lab_disclosure_extract`
- **Headroom:** Not measurable—the job has no `collected_date` lower bound and reads the full history, so every cutoff would truncate data it may consume.

I tested the retention-compliant cutoff **2023-08-07**; the verdict was **DO_NOT_ARCHIVE**. The other consumer, `lab_abnormal_flags`, only reads from **2026-05-09** onward and has **1,006 days of headroom**, but it does not remove the unbounded-consumer blocker.

Although the table scores **COLD (10.8)** and has no observed direct queries in 30 days, range safety is governed by downstream SQL. `hipaa_lab_disclosure_extract` must first gain a proven lower-bound date predicate; then we can reassess and calculate the exact rows and bytes. No archive plan was created.

## What the tool list allowed it to do

Six ColdLineage operations and six read-only DataHub MCP tools. No database credentials,
no object-store client, no ability to issue SQL. `coldlineage_execute_plan` was never
reached — and had it been, it blocks on a human typing `approve`.

Note which surfaces the agent actually used: it read the estate through the **executor**,
cross-checked the catalog through the **MCP Server**, then simulated a cutoff. Two
independent reads of DataHub, one decision, no writes.
