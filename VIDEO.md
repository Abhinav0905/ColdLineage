# Demo video script — 170 seconds

Hard limit is 3:00; judges are not required to watch past it. Target 2:50.
Must be public on YouTube/Vimeo — **test it in an incognito window before submitting.**

## Before you hit record

```bash
# 1. Everything up and warm
datahub docker quickstart                       # or: already running
make demo                                       # seeds + ingests + starts the stack
make examples                                   # confirms the whole path works end to end

# 2. Reset to a clean pre-archive state so the execute step is live on camera
docker exec coldlineage-datahub-hackathon-10thaug-postgres-1 \
  psql -U coldlineage -d coldlineage -c \
  "TRUNCATE archive_runs, archive_plans, audit_events RESTART IDENTITY;"
.venv/bin/python scripts/seed_warehouse.py
```

Open four tabs in this order so you never fumble:
1. ColdLineage Overview — http://localhost:3100
2. ColdLineage Candidates — http://localhost:3100/candidates?dataset=5
3. DataHub entity — http://localhost:9002 → search `patient_encounters`
4. A terminal, font size up, in the repo root

Record at **1920×1080**. The Candidates page is dense — zoom the browser to ~90% so the timeline
and the consumer table both fit without horizontal scroll.

---

## 0:00–0:18 — The claim

> "DataHub can tell you a table is cold. It cannot tell you that **half** a table is cold — and it
> cannot move a single byte. ColdLineage does both, and writes the receipt back into DataHub."

**On screen:** Overview page. Let the estate table land. Do not narrate the tiles.

---

## 0:18–0:42 — The two rows that are the whole argument

Point at `patient_encounters`: **HOT, 81.2** — and the archive-eligible badge.

> "This table is genuinely in active use. Sixty-six queries in the last thirty days. And yet
> forty-seven percent of it is provably unread — because its first four years are cold even though
> the table isn't."

Point at `lab_results`: **zero queries, zero users** — and the red `UNBOUNDED_CONSUMER` chip.

> "This one has had no queries at all in thirty days. Every dataset-level tiering tool archives it.
> It's blocked — and in a moment I'll show you why."

> "Dataset-level temperature gets both of these wrong. That's the gap."

---

## 0:42–1:05 — Where the context comes from

**Cut to terminal.** Make `DATAHUB_GMS_URL` visible on screen.

```bash
curl -s localhost:8000/api/health | jq .datahub
```

> "Nothing here is seeded. Lineage, usage, retention policy, legal hold — all read from a live
> DataHub at request time."

```bash
curl -s localhost:8000/api/datasets/5 | jq '.context.downstream[] | {consumer_name, earliest_date_read, derivation}'
```

> "Seven downstream consumers. For each one, DataHub holds the actual SQL it runs. We parse it with
> sqlglot and resolve how far back it really reads."

---

## 1:05–1:35 — The hero moment. Do not rush this.

**Cut to Candidates.** The Range Safety Timeline is on screen with seven consumer bars.

> "Each bar is the history one consumer still reads, derived from its real query — a `BETWEEN`, a
> ninety-day interval, a `date_trunc`. Now watch the cutoff."

**Drag the cutoff slowly left to right.** Stop at three points:

| Stop | What to say |
|---|---|
| ~2022 | "Two years of clearance. Safe." |
| ~2023-11 | "Now it's tight — forty-seven days from what the compliance dashboard reads." |
| ~2024-03 | *(let the bar go red first, then speak)* "And there it refuses. The cutoff has crossed into data the Quarterly Compliance Dashboard still reads. Sixty days inside its window." |

> "The date picker isn't decoration. The server recomputes the verdict against every consumer's real
> query window on every change."

---

## 1:35–1:52 — The killer case

**Switch the dataset picker to `lab_results`.** Point at the hatched full-width bar.

> "Here's why the quiet table was blocked. This HIPAA disclosure extract runs
> `WHERE performing_lab IS NOT NULL`. It **has** a filter — so a 'does this query filter?' check
> passes it. But there's no date bound, so it reads every row ever written. No cutoff is safe, and
> only looking at the actual SQL catches that."

---

## 1:52–2:22 — Execute, with the verification called out

**Back to `patient_encounters`, cutoff 2023-01-01.** Build plan → point at the plan hash.

> "Approval is bound to a plan hash — dataset, cutoff, row count, verdict. If anything drifts, the
> execute is refused instead of running against different data."

Click **Approve & execute**. When the result lands, point at the verification block.

> "Five hundred sixteen thousand rows, eleven Parquet parts. Then it **downloads the object back**
> from storage and recomputes the checksum on the retrieved bytes — because hashing what you're
> about to upload proves nothing about what landed. Only after that passes does it delete."

Show the row count drop: 1,100,000 → 583,912.

---

## 2:22–2:45 — The receipt lands in DataHub

**Cut to the DataHub entity page.** Refresh.

> "And the context goes back where the next reader will find it."

Point at each, briefly:
- the **deprecation banner** carrying the cutoff and the restore path
- the **`cold-tier-archived`** tag
- the **structured properties** — scroll so both groups are visible

> "Note what's here: the policy values we **read** — retention, legal hold — sitting next to the
> archive values we **wrote**. We patch typed structured properties, we never overwrite
> `datasetProperties`, so nobody else's metadata gets clobbered."

> "So the next agent that queries this table knows an unqualified scan won't return the full
> history, and knows exactly how to get it back."

---

## 2:45–2:55 — Close

**Restore tab.** Click restore. Let "checksum verified" appear.

> "Reversible at the data layer, not just the metadata layer. That's ColdLineage."

---

## Things to avoid on camera

- **Don't show the dollar figure.** At demo scale it's about a cent a month and it undercuts you.
  Lead with rows and the 46.9% fraction — that's the number that transfers.
- Don't claim the estate is real. It's synthetic and every entity is stamped
  `coldlineage.synthetic=true`. Saying so costs three seconds and buys credibility.
- Don't say "agentic" without showing the Skill. If you want to claim the agent, spend five seconds
  on `skills/assess-data-temperature/SKILL.md` instead of using the word.
- Don't let a loading spinner sit on screen. `/api/datasets` takes ~0.4s; give it a beat before
  you start talking.
- Don't demo restore of the full 516k rows live — it takes ~75 seconds. Either cut away, or restore
  a smaller run.
