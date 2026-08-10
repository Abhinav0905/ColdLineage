# The recording script — read this one, not VIDEO.md

`VIDEO.md` is the director's version: why each beat exists. **This is the actor's version.**
Exact words, exact screen, in order. Keep it open on a phone or second monitor.

---

## Read this first: you are not doing one perfect take

Six clips. **15 to 40 seconds each.** Fumble clip 4, re-record clip 4 — nothing else.
Then join them in iMovie, Clipchamp, or QuickTime.

**And if talking while clicking is the hard part — don't.** Record all six clips silently,
watching your own hands. Then play it back and narrate over the top. Every polished demo
video you have ever seen was made this way. It is not cheating; it is how it is done.

Nobody can tell you read from a script. Read from the script.

---

## Before the first clip

```bash
cd "/Users/mac001/Documents/P.D/coldlineage- DataHub-Hackathon-10thAug"

# One command that tells you if you are ready. All four lines must look right.
curl -s --max-time 6 http://localhost:8090/config >/dev/null && echo "GMS UP" || echo "GMS DOWN"
curl -s http://localhost:8000/api/health | python3 -c "import json,sys;print('datahub reachable =',json.load(sys.stdin)['datahub']['reachable'])"
docker exec coldlineage-datahub-hackathon-10thaug-postgres-1 psql -U coldlineage -d coldlineage -tAc \
  "SELECT count(*) FROM public.patient_encounters;"     # must be 1100000
.venv/bin/python scripts/set_policy.py patient_encounters --show   # must be 14 years
```

**If GMS says DOWN** (it gets OOM-killed; give Docker 10 GB in Settings → Resources):

```bash
docker start datahub-datahub-gms-quickstart-1 && sleep 40
```

Tabs, left to right, in this order:

1. `http://localhost:3100` — Overview
2. `http://localhost:3100/candidates?dataset=5` — Candidates
3. `http://localhost:9002` — DataHub, already searched for `patient_encounters`
4. Terminal, font size up, in the repo root

Screen recording at **1920×1080**. Browser zoom **90%**.

---

# CLIP 1 — the claim  ·  ~15 seconds

**Screen:** Overview, top of page.

> DataHub can tell you a table is cold.
>
> It cannot tell you that **half** a table is cold — and it cannot move a single byte.
>
> ColdLineage does both, and writes the receipt back into DataHub.

*Stop recording.*

---

# CLIP 2 — where the rows sit  ·  ~30 seconds

**Screen:** Overview. Scroll to "Where the rows actually sit".

> Every table's rows split three ways.

*Point at `billing_ledger` — it is the only bar showing all three colours right now
(30% green, 59% amber, 11% blue). Explain the legend against THAT bar.*

> Green is archivable. Amber — the gold band — is provably unread, but held by
> retention policy. Blue is rows a consumer can still reach.

*Now move to `patient_encounters` — 1.1 million rows, scored HOT, and 61% amber.*

> This table is genuinely busy. It scores hot.
>
> And sixty percent of it is provably unread — its first four years are cold even
> though the table isn't. Right now retention is holding all of it.

*Point at `lab_results` — the bar that is entirely blue.*

> This one had zero queries and zero users in thirty days.
>
> Every dataset-level tool on the market archives it.
> Here it's a solid wall of *in use*. In a minute, the agent will tell you why.

*Stop recording.*

> **Why there is no green on `patient_encounters` yet:** retention is at fourteen years,
> so nothing on that table is permitted to move. Releasing it is Clip 3. Do not describe
> green while pointing at that bar — use `billing_ledger` for the legend, as above.

---

# CLIP 3 — the knob, and the wall  ·  ~40 seconds
### This is the most important clip. If you only nail one, nail this one.

**Screen:** terminal on one side, Overview on the other.

Type:

```bash
.venv/bin/python scripts/set_policy.py patient_encounters --years 4
```

> Retention was set to fourteen years, so nothing could move. I'll relax it to four.

**Wait for the script to print `ok`. Count to three. Then hit Refresh.**

*(bar becomes 42% green, 19% amber, 39% blue)*

> Now some is archivable, some is still held by policy, and thirty-nine percent is in use.

Type:

```bash
.venv/bin/python scripts/set_policy.py patient_encounters --years 2
```

**Wait for `ok`. Count to three. Refresh.**

> Two years. The amber band disappears — everything policy was holding is free to move.

Type:

```bash
.venv/bin/python scripts/set_policy.py patient_encounters --months 4
```

**Wait for `ok`. Count to three. Refresh. Then say nothing for two seconds. Let them see it.**

> Four months. And nothing happens.
>
> Sixty percent archivable, thirty-nine percent still in use — the same
> four hundred and thirty-three thousand rows as when retention was fourteen years.
>
> Because past a point, the limit stops being policy and becomes this:

*Switch to the Candidates tab (`patient_encounters` is already selected).*

**Where to look — the page is long, so this is the exact route:**

1. Scroll past the red **DO NOT ARCHIVE** banner and past the timeline chart.
2. Stop at the table headed **"Consumer windows"**.
3. **Second row: `Quarterly Compliance Dashboard`.** Read across it — `READS BACK TO`
   says **2024-01-01**, `STATE` says **BLOCKED**, and the **EVIDENCE** column holds
   the predicate:

   ```
   e.event_date BETWEEN CAST('2024-01-01' AS DATE) AND CURRENT_DATE
   ```

4. Click **`</> the query we parsed`** just under it. The full original query unfolds.

*Point at that predicate as you say:*

> A compliance dashboard reads from January 2024. ColdLineage found that by pulling
> the query's real SQL out of DataHub and parsing it.
>
> **The config is a floor, not a permission slip.**

*Stop recording.*

> **No-scroll fallback.** If moving around the page mid-take feels risky, the red
> **DO NOT ARCHIVE** banner at the top of the same page already says it in words:
> *"Blocked by Quarterly Compliance Dashboard: it reads back to 2024-01-01, which is
> 217 days inside the proposed cutoff."* Point at that instead. Less striking than the
> raw SQL, but no navigation and the same claim.

---

# CLIP 4 — the agent says no  ·  ~30 seconds

**Screen:** terminal, full width.

```bash
.venv-agent/bin/python agent/coldlineage_agent.py "lab_results looks cold. Can we archive it?"
```

> There's an agent on top of this. It reads DataHub through the official MCP Server,
> and acts through six constrained operations.
>
> It holds no database credentials and cannot issue SQL.

*Let the tool calls scroll past.*

> It checks the estate, cross-checks the catalog over MCP, and tests a cutoff.

*When the answer lands, read the first line straight off your screen.*

> **No — lab_results cannot currently be archived safely.** Zero rows out of seven hundred thousand.
>
> One HIPAA disclosure extract reads the table with no date bound, so not a single row
> can be *proved* unread.
>
> It's the coldest table in the estate, and the agent still says no —
> and it tells you what would have to change for the answer to be yes.

*Stop recording.*

---

# CLIP 5 — move the bytes, prove it  ·  ~35 seconds

**Screen:** Candidates tab, `patient_encounters`, cutoff `2024-01-01`.
Click Simulate → Plan → Execute. Approve when it asks.

> Plan, then execute. Approval is a plan hash, binding dataset, cutoff, row count and verdict.
> If live state moved since the plan was shown, execution is refused.

*While it runs:*

> It streams the range out to Parquet, uploads it — then downloads it back and recomputes
> the SHA-256 on the retrieved bytes before deleting anything.
>
> Hashing the buffer you were about to upload proves nothing about what landed.

*Switch to the DataHub tab. Reload the entity.*

> And here's the receipt, back in DataHub. Archive properties, a deprecation note with the
> cutoff and the restore path, a link to the manifest, and a searchable tag.
>
> The archive is also its own catalog entity now, with lineage back to the table it came from.

*Stop recording.*

---

# CLIP 6 — give it back, and close  ·  ~20 seconds

**Screen:** terminal, then Overview.

> **Clip 6 undoes clip 5.** It restores the range clip 5 archived, so clip 5 must have run
> first. And do not hardcode a run id — every archive makes a new one. Let the shell find
> the unrestored run:

```bash
RUN=$(curl -s http://localhost:8000/api/runs \
  | python3 -c "import json,sys;r=[x for x in json.load(sys.stdin) if not x.get('restored_at')];print(r[0]['id'] if r else '')")
echo "restoring run $RUN"

curl -s -X POST http://localhost:8000/api/restore \
  -H 'Content-Type: application/json' -d "{\"run_id\":$RUN,\"temporary\":false}"
```

*(If `$RUN` comes back empty, nothing is archived — go shoot clip 5 first.)*

> And it's reversible. Checksum-verified against the manifest on the way back,
> or it refuses to restore at all.

*Back to Overview. Refresh. Bars whole again.*

> Honest about scale: at demo size the saving is about a cent a month, and we report
> the measured figure rather than inflating it.
>
> What transfers is the fraction. Sixty percent of a live table was provably unread —
> and nothing moved until the evidence said it could.

*Stop recording. You're done.*

---

## Resetting between takes

Re-record clip 3 (the policy sweep):

```bash
.venv/bin/python scripts/set_policy.py patient_encounters --years 14
```

Re-record clip 5 (the archive):

```bash
# put the rows back first; the plan then becomes executable again
RUN=$(curl -s http://localhost:8000/api/runs \
  | python3 -c "import json,sys;r=[x for x in json.load(sys.stdin) if not x.get('restored_at')];print(r[0]['id'] if r else '')")
[ -n "$RUN" ] && curl -s -X POST http://localhost:8000/api/restore \
  -H 'Content-Type: application/json' -d "{\"run_id\":$RUN,\"temporary\":false}" || echo "nothing to restore"
```

Full reset to the start line:

```bash
.venv/bin/python scripts/set_policy.py patient_encounters --years 14
docker exec coldlineage-datahub-hackathon-10thaug-postgres-1 psql -U coldlineage \
  -d coldlineage -tAc "SELECT count(*) FROM public.patient_encounters;"   # want 1100000
```

## If something breaks mid-take

Stop. Don't narrate around it — it always shows.

- **Estate page errors** → GMS died. `docker start datahub-datahub-gms-quickstart-1`, wait 40s.
- **Bands didn't change** → you refreshed too fast. Wait three seconds, refresh again.
- **Agent says "No model provider credentials found"** → the key is not in that shell.
  `export OPENAI_API_KEY=$(grep -E '^OPENAI_API_KEY=' .env | cut -d= -f2-)`, then rerun.

## If you are running out of time

Clips 1, 2, 3 and 4 alone are a complete, honest submission — the claim, the evidence,
the config-versus-evidence beat, and an agent that refuses. That is ninety seconds and it
is the whole argument. Clips 5 and 6 are proof you can add if the clock allows.

**Do not skip clip 3.**
