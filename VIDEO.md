# Demo video script — 170 seconds

Hard limit is 3:00 and judges are not required to watch past it. Target **2:50**.
Must be public on YouTube/Vimeo — **test the link in an incognito window before submitting.**

The spine of this cut is one idea, shown three times in three different ways:

> **Configuration is a floor, not a permission slip.** What makes an archive safe is
> evidence about what downstream consumers actually read — and no setting can move it.

Anybody can demo a config knob. Almost nobody can demo a system that *refuses to obey a
loosened config* because it holds independent proof. That contrast is the whole video.

---

## Before you hit record

```bash
# 1. Stack up. DataHub needs ~8 GB given to Docker or GMS dies waiting on search.
docker compose up -d
docker start datahub-opensearch-1 datahub-kafka-broker-1 && sleep 45
docker start datahub-datahub-gms-quickstart-1 datahub-frontend-quickstart-1

# 2. The estate must be WHOLE: 1,100,000 rows spanning 2019-01-01 -> 2026-08-05.
docker exec coldlineage-datahub-hackathon-10thaug-postgres-1 psql -U coldlineage \
  -d coldlineage -tAc \
  "SELECT count(*), min(event_date), max(event_date) FROM public.patient_encounters;"
#    If it is short, restore the unrestored run rather than reseeding:
#    curl -s http://localhost:8000/api/runs | python3 -m json.tool | grep -A3 restored_at
#    curl -s -X POST http://localhost:8000/api/restore -H 'Content-Type: application/json' \
#         -d '{"run_id":<N>,"temporary":false}'

# 3. Start Act 1 with retention wide open.
.venv/bin/python scripts/set_policy.py patient_encounters --years 14

# 4. Confirm the agent's key works, so you are not discovering a 429 on camera.
set -a && . ./.env && set +a
.venv-agent/bin/python -c "import os;print('key set:', bool(os.environ.get('OPENAI_API_KEY')))"
```

Open these tabs, in this order, so you never fumble:

1. **Overview** — http://localhost:3100
2. **Candidates** — http://localhost:3100/candidates?dataset=5
3. **DataHub entity** — http://localhost:9002 → search `patient_encounters`
4. **Terminal**, font size up, in the repo root

Record at **1920×1080**, browser at ~90% zoom so the band chart and the estate table both fit
without horizontal scroll.

> **The one thing that will bite you.** A structured-property write is not readable the instant
> the mutation returns — it travels through DataHub's change log first. `set_policy.py` polls
> until the new value is visible, but **still leave a beat before you hit Refresh.** If you
> refresh too fast you will show the old numbers and it will look like nothing happened.

---

## 0:00–0:15 — The claim

**On screen:** Overview. Let it land. Say nothing about the KPI tiles.

> "DataHub can tell you a table is cold. It cannot tell you that **half** a table is cold — and
> it cannot move a single byte. ColdLineage does both, and writes the receipt back into DataHub."

---

## 0:15–0:45 — Where the rows actually sit

**On screen:** the band chart. Cursor moving slowly along one bar at a time.

> "Every table's rows split three ways. Green is archivable. Amber is provably unread but held
> by retention policy. Blue is rows a consumer can still reach."

Point at `patient_encounters` — **1.1 million rows, and it scores HOT, 81.**

> "This table is genuinely busy. And yet sixty percent of it is provably unread, because its
> first four years are cold even though the table isn't."

Point at `lab_results` — the bar that is **entirely blue**.

> "This one had zero queries and zero users in thirty days. Every dataset-level tiering tool on
> the market archives it. Here it's a solid wall of *in use* — and in a moment the agent will
> tell you why."

---

## 0:45–1:25 — The knob, and the wall it hits

**On screen:** terminal beside the browser. Run each command, leave a beat, hit Refresh.

```bash
.venv/bin/python scripts/set_policy.py patient_encounters --years 4
```

> "Retention was fourteen years, so nothing could move. I'll set it to four."

*(bar splits three ways: 42% green, 19% amber, 39% blue)*

> "Now some of it is archivable, some is still held by policy, and thirty-nine percent is in use."

```bash
.venv/bin/python scripts/set_policy.py patient_encounters --years 2
```

> "Two years. The amber band disappears — everything policy was holding is now free to move."

```bash
.venv/bin/python scripts/set_policy.py patient_encounters --months 4
```

**Beat. Let the refresh land. Let the viewer see that nothing changed.**

> "Four months. And *nothing happens*. Sixty percent archivable, thirty-nine percent still in
> use — the same four hundred and thirty-three thousand rows as when retention was fourteen
> years. Because past a point the limit stops being policy and starts being this:"

**Switch to Candidates tab**, point at the extracted predicate:

```sql
WHERE e.event_date BETWEEN DATE '2024-01-01' AND CURRENT_DATE
```

> "A compliance dashboard reads from January 2024. ColdLineage got that by pulling the query's
> real SQL out of DataHub and parsing it. **The config is a floor, not a permission slip.**"

---

## 1:25–1:55 — The agent says no

**On screen:** terminal, full width.

```bash
.venv-agent/bin/python agent/coldlineage_agent.py \
  "lab_results looks cold. Can we archive it?"
```

> "There's an agent on top of this. It reads DataHub through the official MCP Server and acts
> through six constrained operations. It holds no database credentials and cannot issue SQL."

Let the tool calls scroll — `coldlineage_list_datasets`, then `search` via MCP, then
`coldlineage_assess_dataset`, then `coldlineage_simulate_cutoff`.

> "It checks the estate, cross-checks the catalog over MCP, and tests a cutoff."

When the answer lands, read the first line off the screen:

> "**No — lab_results cannot currently be archived safely.** Zero rows of seven hundred thousand.
> One HIPAA disclosure extract reads the table with no date bound, so not one row can be *proved*
> unread. It's the coldest table in the estate and the agent still says no — and it tells you what
> would have to change for the answer to be yes."

*(If you have both keys: `--provider anthropic` runs the identical prompt and tool set on Claude.
Worth one sentence — the guardrail is the tool list, so the model is the swappable part.)*

---

## 1:55–2:30 — Move the bytes, and prove it

**On screen:** Candidates tab, `patient_encounters`, cutoff `2024-01-01`. Click through
Simulate → Plan → Execute. Approve when it asks.

> "Plan, then execute. Approval is a **plan hash** binding dataset, cutoff, row count and
> verdict — if live state moved since the plan was shown, execution is refused rather than run
> against different data."

While it runs:

> "It streams the range out to Parquet, uploads it, then **downloads it back and recomputes the
> SHA-256 on the retrieved bytes**, re-reads the Parquet and asserts the row count and schema.
> Only then does it delete, in one transaction. Hashing the buffer you were about to upload
> proves nothing about what landed."

**Switch to the DataHub tab. Reload the entity.**

> "And here's the receipt, back in DataHub: six typed archive properties, a deprecation note
> carrying the cutoff and the restore path, a link to the manifest, and a searchable tag."

Search DataHub for `cold-tier-frozen-copy`:

> "The archive is also its own catalog entity now — same schema, with lineage back to the table
> it came from. So the next person who opens either one inherits the whole story."

---

## 2:30–2:50 — Give it back, and close

```bash
curl -s -X POST http://localhost:8000/api/restore \
  -H 'Content-Type: application/json' -d '{"run_id":1,"temporary":false}'
```

> "And it's reversible. Checksum verified against the manifest on the way back, or it refuses to
> restore at all."

**Back to Overview, refresh, bars whole again.**

> "Honest about scale: at demo size the saving is about a cent a month, and we report the measured
> figure rather than inflating it. What transfers is the fraction — **sixty percent of a live
> table was provably unread** — and the fact that nothing moved until the evidence said it could."

---

## If you overrun

Cut in this order. Never cut the 0:45–1:25 block; it is the video.

1. The `cold-tier-frozen-copy` search (2:20–2:30) — it's in `examples/` anyway.
2. The restore (2:30–2:40) — say "and it's reversible, checksum-verified" over the Overview.
3. The `--provider anthropic` aside.
4. The narration over execute — let the progress speak and just say "verified before delete".

## Do not say

- **"Saves you money at scale."** You measured a cent a month. Say the fraction, not the dollars.
- **"AI decides what to archive."** The model proposes and explains; sqlglot derives the bound;
  a human approves; the executor verifies. Claiming otherwise invites the obvious objection.
- **"Replaces DataHub's tiering."** It doesn't. Metadata Tests already find cold tables. What is
  new is sub-table date ranges and touching the data plane at all.
- **"Fully automated."** The human gate is a feature. Say so.
