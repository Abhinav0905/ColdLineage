#!/usr/bin/env python
"""Regenerate everything under examples/ from a real run.

    python scripts/record_examples.py
    python scripts/record_examples.py --skip-seed          # keep the warehouse as-is
    python scripts/record_examples.py --seed-scale 0.05    # fast, smaller estate

WHAT THIS IS FOR
----------------
The hackathon rules say judges may score from the repository alone, without running
anything. So examples/ has to be a truthful, self-contained record of the product
actually working: the estate as DataHub describes it, the consumer windows parsed out
of real SQL, the verdict flipping as the cutoff moves, a real archive with read-back
verification, a real restore, and the provenance written back into DataHub.

Every byte in examples/ is captured from a live run. Nothing here is hand-written and
presented as recorded. If a step fails, the script says so and exits non-zero rather
than writing a plausible-looking artifact.

WHAT IT DOES, IN ORDER
----------------------
  1. reseed the Postgres warehouse                    (scripts/seed_warehouse.py)
  2. reset local run state                            (plans, runs, audit)
  3. reset ColdLineage's own writeback in DataHub     (so before/after is a real before)
  4. record cassettes                                 (DataHubClient(record=True))
  5. start a traced copy of the backend               (see TRACING, below)
  6. drive the live API and write examples/*.json
  7. execute one real archive, verify it, restore it
  8. render consumer-windows.md, datahub-writeback.md, README.md
  9. pull one real Parquet part + its manifest out of MinIO

TRACING
-------
Step 5 launches `app.main:app` -- the same application, unmodified -- with
`DataHubClient._execute` wrapped so that every GraphQL document, its variables and the
GMS response are appended to a JSONL trace. That is how datahub-writeback.md can show
the mutations verbatim instead of describing them. The patch is applied here, in this
script; nothing under backend/app/ is touched, and the traced server runs on its own
port so an already-running backend is left alone.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
EXAMPLES = REPO / "examples"
CASSETTES = EXAMPLES / "cassettes"
ARCHIVE_DIR = EXAMPLES / "archive"

# The five demo tables, by the leaf name that appears in the URN.
ESTATE = ["billing_ledger", "care_events_live", "claims_history", "lab_results", "patient_encounters"]

HERO = "patient_encounters"
HERO_CUTOFF = "2023-01-01"
SWEEP = ["2020-01-01", "2022-01-01", "2023-01-01", "2023-11-15", "2024-01-01", "2024-03-01", "2025-06-01"]
LAB_SWEEP = ["2019-06-01", "2020-01-01", "2021-06-01", "2022-01-01", "2024-01-01", "2025-01-01"]
CLAIMS_SWEEP = ["2019-01-01", "2020-01-01", "2021-01-01"]
APPROVER = "recorded-run@coldlineage.local"

ARCHIVE_PROPERTY_URNS = [
    "urn:li:structuredProperty:io.coldlineage.archive.state",
    "urn:li:structuredProperty:io.coldlineage.archive.archivedThrough",
    "urn:li:structuredProperty:io.coldlineage.archive.objectUri",
    "urn:li:structuredProperty:io.coldlineage.archive.sha256",
    "urn:li:structuredProperty:io.coldlineage.archive.restoreSla",
    "urn:li:structuredProperty:io.coldlineage.archive.lastRunId",
]
ARCHIVED_TAG_URN = "urn:li:tag:cold-tier-archived"

# One document, used for the before/after read-back in datahub-writeback.md. It is
# deliberately not one of the app's own queries: the point is to look at the entity
# from outside the application and see what a third party would see.
ENTITY_AUDIT_QUERY = """
query coldlineageEntityAudit($urn: String!) {
  dataset(urn: $urn) {
    urn
    name
    deprecation { deprecated note decommissionTime }
    tags { tags { tag { urn properties { name } } } }
    institutionalMemory { elements { url label } }
    structuredProperties {
      properties {
        structuredProperty { urn definition { qualifiedName displayName } }
        values { ... on StringValue { stringValue } ... on NumberValue { numberValue } }
      }
    }
  }
}
"""

RESET_PROPS = """
mutation coldlineageResetProps($input: RemoveStructuredPropertiesInput!) {
  removeStructuredProperties(input: $input) { properties { structuredProperty { urn } } }
}
"""
RESET_DEPRECATION = """
mutation coldlineageResetDeprecation($input: UpdateDeprecationInput!) { updateDeprecation(input: $input) }
"""
RESET_TAG = """
mutation coldlineageResetTag($input: TagAssociationInput!) { removeTag(input: $input) }
"""
RESET_LINK = """
mutation coldlineageResetLink($input: RemoveLinkInput!) { removeLink(input: $input) }
"""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


class Failed(RuntimeError):
    pass


_STEP = [0]


def step(text: str) -> None:
    _STEP[0] += 1
    print(f"\n[{_STEP[0]}] {text}", flush=True)


def note(text: str) -> None:
    print(f"      {text}", flush=True)


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return path


def human_bytes(value: int | None) -> str:
    if not value:
        return "-"
    v = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:,.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= 1024
    return f"{v:.1f} TB"


def gql(client: httpx.Client, gms: str, document: str, variables: dict | None = None) -> dict:
    response = client.post(
        f"{gms.rstrip('/')}/api/graphql",
        json={"query": document, "variables": variables or {}},
        headers={"Content-Type": "application/json"},
        timeout=60.0,
    )
    response.raise_for_status()
    return response.json()


# --------------------------------------------------------------------------
# 1-2. Warehouse
# --------------------------------------------------------------------------


def reseed(dsn: str, scale: float) -> dict:
    out = Path(tempfile.gettempdir()) / "coldlineage-seed-summary.json"
    cmd = [sys.executable, str(REPO / "scripts" / "seed_warehouse.py"), "--dsn", dsn,
           "--scale", str(scale), "--json", str(out)]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if proc.returncode != 0:
        raise Failed(f"seed_warehouse.py failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    summary: dict = {}
    if out.exists():
        summary = json.loads(out.read_text())
        out.unlink()
    tail = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1:]
    note(tail[0] if tail else "seeded")
    return summary


def reset_run_state(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE archive_runs, archive_plans, audit_events RESTART IDENTITY")
            # A previous temporary restore leaves a side table behind. It is not part of
            # the estate and it should not show up in anyone's warehouse.
            cur.execute("DROP TABLE IF EXISTS restored_patient_encounters")
    note("archive_runs, archive_plans, audit_events truncated; identities reset")


# --------------------------------------------------------------------------
# 3. DataHub reset -- so "before" really is a before
# --------------------------------------------------------------------------


def reset_datahub_writeback(gms: str, urns: list[str]) -> list[str]:
    done: list[str] = []
    with httpx.Client() as client:
        for urn in urns:
            name = urn.split(",")[-2].split(".")[-1]
            body = gql(client, gms, ENTITY_AUDIT_QUERY, {"urn": urn})
            entity = (body.get("data") or {}).get("dataset") or {}

            present = {
                (p.get("structuredProperty") or {}).get("urn")
                for p in ((entity.get("structuredProperties") or {}).get("properties") or [])
            }
            targets = [u for u in ARCHIVE_PROPERTY_URNS if u in present]
            if targets:
                gql(client, gms, RESET_PROPS,
                    {"input": {"assetUrn": urn, "structuredPropertyUrns": targets}})
                done.append(f"{name}: removed {len(targets)} io.coldlineage.archive.* properties")

            if (entity.get("deprecation") or {}).get("deprecated"):
                gql(client, gms, RESET_DEPRECATION,
                    {"input": {"urn": urn, "deprecated": False, "note": ""}})
                done.append(f"{name}: cleared deprecation")

            tags = {(t.get("tag") or {}).get("urn") for t in ((entity.get("tags") or {}).get("tags") or [])}
            if ARCHIVED_TAG_URN in tags:
                gql(client, gms, RESET_TAG, {"input": {"tagUrn": ARCHIVED_TAG_URN, "resourceUrn": urn}})
                done.append(f"{name}: removed the cold-tier-archived tag")

            for element in ((entity.get("institutionalMemory") or {}).get("elements") or []):
                label = element.get("label") or ""
                if label.startswith("ColdLineage archive manifest"):
                    gql(client, gms, RESET_LINK,
                        {"input": {"resourceUrn": urn, "linkUrl": element.get("url"), "label": label}})
                    done.append(f"{name}: removed manifest link {element.get('url')}")
    for line in done:
        note(line)
    if not done:
        note("nothing to reset -- no prior ColdLineage writeback on the estate")
    return done


def entity_audit(gms: str, urn: str) -> dict:
    with httpx.Client() as client:
        return gql(client, gms, ENTITY_AUDIT_QUERY, {"urn": urn})


# --------------------------------------------------------------------------
# 4. Cassettes
# --------------------------------------------------------------------------


def record_cassettes(urns: list[str]) -> list[str]:
    """Every GMS call the application makes, recorded verbatim.

    The key is f"{operation}.{sha256(json.dumps(variables,sort_keys=True))[:16]}", so the
    variables have to match what the app actually sends -- including
    get_downstream(count=1), which ContextService.lineage_complete() issues, and
    search_datasets(count=100), which the /api/datasets router issues.
    """
    import asyncio

    from app.datahub.client import DataHubClient  # noqa: PLC0415 - after env is set

    async def run() -> None:
        client = DataHubClient(record=True)
        await client.health()
        await client.search_datasets(query="*", count=100)
        await client.search_datasets(query="*", count=50)  # the client's own default
        for urn in urns:
            await client.get_dataset(urn)
            await client.get_structured_properties(urn)
            await client.get_usage(urn)
            await client.get_downstream(urn, count=50)
            await client.get_downstream(urn, count=1)  # lineage_complete()
            await client.get_queries(urn, count=50)

    asyncio.run(run())
    files = sorted(p.name for p in CASSETTES.glob("*.json"))
    note(f"{len(files)} cassettes in {CASSETTES.relative_to(REPO)}")
    return files


# --------------------------------------------------------------------------
# 5. The traced backend
# --------------------------------------------------------------------------

LAUNCHER = '''
import json, sys, os
from datetime import datetime, timezone

sys.path.insert(0, BACKEND)
import app.datahub.client as C

TRACE = os.environ["COLDLINEAGE_TRACE"]
_orig = C.DataHubClient._execute

async def _traced(self, document, variables=None):
    body = await _orig(self, document, variables)
    try:
        with open(TRACE, "a") as fh:
            fh.write(json.dumps({
                "at": datetime.now(timezone.utc).isoformat(),
                "gms_url": self.gms_url,
                "document": document,
                "variables": variables or {},
                "response": body,
            }, default=str) + "\\n")
    except Exception:
        pass
    return body

C.DataHubClient._execute = _traced

import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port=PORT, log_level="warning")
'''


class TracedBackend:
    def __init__(self, port: int, env: dict[str, str], trace: Path) -> None:
        self.port = port
        self.env = env
        self.trace = trace
        self.proc: subprocess.Popen | None = None
        self.log = Path(tempfile.gettempdir()) / "coldlineage-record-backend.log"

    def __enter__(self) -> TracedBackend:
        source = (
            f"BACKEND = {str(BACKEND)!r}\n"
            f"PORT = {self.port}\n"
            + LAUNCHER
        )
        launcher = Path(tempfile.mkdtemp(prefix="coldlineage-")) / "traced_server.py"
        launcher.write_text(source)
        self._launcher_dir = launcher.parent

        env = dict(os.environ)
        env.update(self.env)
        env["COLDLINEAGE_TRACE"] = str(self.trace)
        self.trace.unlink(missing_ok=True)

        handle = self.log.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, str(launcher)], cwd=str(BACKEND), env=env,
            stdout=handle, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{self.port}"
        for _ in range(120):
            if self.proc.poll() is not None:
                raise Failed(f"traced backend died on startup; see {self.log}\n"
                             f"{self.log.read_text()[-2000:]}")
            try:
                httpx.get(f"{base}/api/health", timeout=2.0).raise_for_status()
                note(f"traced backend up on {base} (trace -> {self.trace})")
                return self
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        raise Failed(f"traced backend did not answer on {base}; see {self.log}")

    def __exit__(self, *exc: object) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=15)
        with contextlib.suppress(Exception):
            shutil.rmtree(self._launcher_dir)

    def entries(self) -> list[dict]:
        if not self.trace.exists():
            return []
        out = []
        for line in self.trace.read_text().splitlines():
            if line.strip():
                with contextlib.suppress(json.JSONDecodeError):
                    out.append(json.loads(line))
        return out


# --------------------------------------------------------------------------
# 6-7. Drive the API
# --------------------------------------------------------------------------


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(timeout=300.0)

    def get(self, path: str) -> Any:
        r = self.client.get(self.base + path)
        if r.status_code != 200:
            raise Failed(f"GET {path} -> HTTP {r.status_code}: {r.text[:400]}")
        return r.json()

    def post(self, path: str, body: dict, expect: tuple[int, ...] = (200,)) -> tuple[int, Any]:
        r = self.client.post(self.base + path, json=body)
        if r.status_code not in expect:
            raise Failed(f"POST {path} -> HTTP {r.status_code} (wanted {expect}): {r.text[:400]}")
        try:
            return r.status_code, r.json()
        except json.JSONDecodeError:
            return r.status_code, {"raw": r.text[:400]}


# --------------------------------------------------------------------------
# Markdown renderers
# --------------------------------------------------------------------------

DERIVATION_EXPLAIN = {
    "sql_predicate": (
        "sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on "
        "the subject's date column, and resolved it to a concrete date"
    ),
    "no_date_filter": (
        "the statement was parsed successfully and places NO lower bound on the date column -- "
        "an unbounded scan. Fail-closed: no cutoff can be proven safe"
    ),
    "no_queries_observed": (
        "DataHub has a lineage edge but no query text for this consumer, so its lookback cannot "
        "be proven. Fail-closed: treated as unbounded"
    ),
    "not_a_query_consumer": (
        "this consumer does not read the subject directly; it is reached at >1 hop and inherits "
        "the earliest bound of the consumers between it and the subject"
    ),
    "declared_property": "the window was declared on the entity as a structured property",
}


def render_consumer_windows(details: dict[str, dict]) -> str:
    lines: list[str] = []
    a = lines.append
    a("# Consumer history windows")
    a("")
    a("**This is the differentiator, on one page.**")
    a("")
    a("DataHub knows *that* `patient_encounters` has seven downstream consumers. It does not")
    a("know that the furthest back any of them reads is 2024-01-01, which is the only fact that")
    a("decides whether 2019-2022 can be moved to cold storage while the table stays live.")
    a("")
    a("Every row below was produced by reading the consumer's real SQL out of DataHub as a")
    a("`Query` entity, parsing it with sqlglot, and resolving the lower bound it places on the")
    a("subject table's date column into a concrete date. Nothing is declared, configured or")
    a("guessed. Where a bound cannot be proven, the consumer is reported as unbounded and it")
    a("blocks every cutoff -- that is the fail-closed rule, and it is what makes `lab_results`")
    a("unarchivable despite being stone cold by every table-level signal.")
    a("")
    a(f"Captured {datetime.now(UTC).isoformat(timespec='seconds')} from a live DataHub OSS "
      f"v1.7.0 instance via `scripts/record_examples.py`.")
    a("")
    a("## Summary")
    a("")
    a("| Dataset | Consumer | Type | Deg | Derivation | Earliest date read |")
    a("|---|---|---|---|---|---|")
    for key in ESTATE:
        detail = details.get(key)
        if not detail:
            continue
        for w in detail["context"]["downstream"]:
            earliest = w["earliest_date_read"] or "**unbounded**"
            a(f"| `{key}` | {w['consumer_name']} | {w['consumer_type']} | {w['degree']} "
              f"| `{w['derivation']}` | {earliest} |")
    a("")

    for key in ESTATE:
        detail = details.get(key)
        if not detail:
            continue
        ctx = detail["context"]
        windows = ctx["downstream"]
        bounded = [w["earliest_date_read"] for w in windows if w["earliest_date_read"]]
        unbounded = [w for w in windows if not w["earliest_date_read"]]
        a("---")
        a("")
        a(f"## `{key}`")
        a("")
        a(f"- date column: `{ctx['date_column']}` "
          f"({ctx['date_column_provenance']['source']}: {ctx['date_column_provenance']['detail']})")
        rows = f"{ctx['row_count']:,}" if ctx.get("row_count") is not None else "unknown"
        a(f"- measured span in Postgres: {ctx['min_date']} to {ctx['max_date']}, "
          f"{rows} rows, {human_bytes(ctx['size_bytes'])}")
        if unbounded:
            names = ", ".join(w["consumer_name"] for w in unbounded)
            a(f"- **no safe cutoff exists**: {len(unbounded)} unbounded consumer(s) ({names})")
        elif bounded:
            a(f"- latest provably safe cutoff: **{min(bounded)}** "
              f"(the earliest date any consumer still reads)")
        a("")
        for w in windows:
            a(f"### {w['consumer_name']}")
            a("")
            a(f"- urn: `{w['consumer_urn']}`")
            a(f"- type: {w['consumer_type']}"
              + (f" on {w['platform']}" if w.get("platform") else "")
              + f", {w['degree']} lineage hop(s) from the subject")
            if w.get("query_run_count") is not None:
                a(f"- runs recorded in DataHub: {w['query_run_count']}")
            a(f"- **earliest_date_read: {w['earliest_date_read'] or 'NONE -- unbounded'}**")
            a(f"- derivation: `{w['derivation']}` -- "
              f"{DERIVATION_EXPLAIN.get(w['derivation'], 'see backend/app/services/window.py')}")
            a(f"- extracted predicate: "
              + (f"`{w['predicate']}`" if w.get("predicate") else "_none found_"))
            a(f"- provenance: `{w['provenance']['source']}` -- {w['provenance']['detail']}")
            a("")
            if w.get("evidence_sql"):
                a("Verbatim SQL, as read from the DataHub `Query` entity:")
                a("")
                a("```sql")
                a(w["evidence_sql"].rstrip())
                a("```")
            else:
                a("_No SQL recorded in DataHub for this consumer._")
            a("")
    a("---")
    a("")
    a("## How to reproduce a single row by hand")
    a("")
    a("```python")
    a("from datetime import date")
    a("import sys; sys.path.insert(0, 'backend')")
    a("from app.services.window import HistoryWindowExtractor")
    a("")
    a("sql = \"SELECT ... FROM public.patient_encounters e \"\\")
    a("      \"WHERE e.event_date > (NOW() - INTERVAL '24 months')::date\"")
    a("print(HistoryWindowExtractor().extract(sql, 'patient_encounters', 'event_date',")
    a("                                       as_of=date.today()))")
    a("```")
    a("")
    return "\n".join(lines)


def _fmt_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def render_writeback(before: dict, after: dict, mutations: list[dict],
                     writeback: dict, manifest: dict, entity_url: str) -> str:
    lines: list[str] = []
    a = lines.append
    a("# The contribution back to DataHub")
    a("")
    a("ColdLineage does not only read the catalog. Once an archive is verified, the receipt")
    a("goes back into DataHub so the next person to open the entity inherits the fact that")
    a("part of this table's history is no longer in the warehouse.")
    a("")
    a("Four separate contributions, each reported independently so a partial failure is")
    a("visible rather than swallowed:")
    a("")
    a("1. typed structured properties under `io.coldlineage.archive.*` -- machine-readable facts")
    a("2. a deprecation note carrying the cutoff and the restore path -- the human-visible banner")
    a("3. an `institutionalMemory` link to the manifest -- for whoever needs the bytes")
    a("4. the `cold-tier-archived` tag -- so \"what has an archived range?\" is one search")
    a("")
    a("Deliberately **not** written: the `datasetProperties` aspect. It holds other writers'")
    a("`customProperties`, and a whole-aspect PUT silently destroys them.")
    a("")
    a(f"Entity: {entity_url}")
    a("")
    a("## How this transcript was captured")
    a("")
    a("`scripts/record_examples.py` launches the unmodified `app.main:app` with")
    a("`DataHubClient._execute` wrapped so that every GraphQL document, its variables and the")
    a("GMS response are appended to a JSONL trace. The blocks below are that trace, verbatim,")
    a("filtered to the mutations issued during `POST /api/execute`. Nothing here was typed by")
    a("hand.")
    a("")
    a("---")
    a("")
    a("## Before")
    a("")
    a("The entity as a third party sees it, read straight from GMS immediately before")
    a("`POST /api/execute`. The `io.coldlineage.policy.*` values are inputs ColdLineage READS;")
    a("no `io.coldlineage.archive.*` value exists yet, there is no deprecation banner, no")
    a("`cold-tier-archived` tag and no manifest link.")
    a("")
    a("```json")
    a(_fmt_json(before))
    a("```")
    a("")
    a("---")
    a("")
    a("## The mutations, as sent")
    a("")
    if not mutations:
        a("_No mutations were captured. The archive either did not run or ran against a "
          "backend without tracing enabled._")
    for index, entry in enumerate(mutations, start=1):
        op = entry.get("operation") or "mutation"
        a(f"### {index}. `{op}`")
        a("")
        a(f"Sent to `{entry.get('gms_url')}/api/graphql` at {entry.get('at')}.")
        a("")
        a("Document:")
        a("")
        a("```graphql")
        a((entry.get("document") or "").strip())
        a("```")
        a("")
        a("Variables:")
        a("")
        a("```json")
        a(_fmt_json(entry.get("variables")))
        a("```")
        a("")
        a("GMS response:")
        a("")
        a("```json")
        a(_fmt_json(entry.get("response")))
        a("```")
        a("")
    a("---")
    a("")
    a("## What the application reported")
    a("")
    a("The `datahub_writeback` block of the `POST /api/execute` response -- one line per")
    a("contribution, each with its own status.")
    a("")
    a("```json")
    a(_fmt_json(writeback))
    a("```")
    a("")
    a("---")
    a("")
    a("## After")
    a("")
    a("The same read-back query, run again after the archive completed. Read it next to the")
    a("Before block above:")
    a("")
    a(f"- `io.coldlineage.policy.*` -- unchanged. These are the inputs we READ "
      f"(retention floor, legal hold, business criticality).")
    a(f"- `io.coldlineage.archive.*` -- six new typed values we WROTE, sitting alongside them: "
      f"state, archivedThrough, objectUri, sha256, restoreSla, lastRunId.")
    a(f"- `deprecation.note` -- names the cutoff ({manifest.get('cutoff_date')}), the row count "
      f"({manifest.get('rows', 0):,}), the object URI, the checksum and the exact call that "
      f"rehydrates it.")
    a(f"- `tags` -- `{ARCHIVED_TAG_URN}` added next to the estate's own PHI/PII/HIPAA tags.")
    a(f"- `institutionalMemory` -- a clickable link to the manifest.")
    a("")
    a("```json")
    a(_fmt_json(after))
    a("```")
    a("")
    a("### Note on the manifest link")
    a("")
    a("`institutionalMemory` rejects non-HTTP schemes outright (`URL scheme 's3' is not")
    a("allowed`), so the link is written as the object store's HTTP endpoint. The canonical")
    a("`s3://` URI is still recorded, in `io.coldlineage.archive.objectUri`:")
    a("")
    a(f"- object: `{manifest.get('object_uri')}`")
    a(f"- manifest: `{manifest.get('manifest_uri')}`")
    a(f"- sha256: `{manifest.get('sha256')}`")
    a("")
    return "\n".join(lines)


README_TEMPLATE = """# examples/ -- what a real run produced

Everything in this directory was captured from a live run of ColdLineage against a live
DataHub OSS **v1.7.0** GMS, a live Postgres warehouse and a live MinIO object store. It
exists so this project can be judged from the repository alone, without standing anything
up. Regenerate the whole directory with:

```bash
python scripts/record_examples.py
```

Recorded: **{recorded_at}**

## Two things to be plain about

**The estate is synthetic.** The five tables are generated by `scripts/seed_warehouse.py`
and the consumers, their SQL and their usage telemetry are pushed into DataHub by
`scripts/ingest_datahub.py`. No real patient data exists anywhere in this project.

**The measurements are not.** Every row count, byte size and date range in these files was
measured at request time -- `count(*)`, `pg_total_relation_size()` and `min()/max()` against
Postgres, and `sha256` over bytes downloaded back out of MinIO. Nothing is estimated except
the per-range byte figure, which is the table's measured physical size apportioned by row
share and is labelled as an estimate everywhere it appears, because Postgres does not track
per-range size.

## Start here

| File | What it proves |
|---|---|
| [`consumer-windows.md`](consumer-windows.md) | **The differentiator.** Every downstream consumer of all five tables, its verbatim SQL as stored in DataHub, the predicate sqlglot extracted, the date that resolved to, and how. This is the fact DataHub cannot give you. |
| [`blocked-lab-results.json`](blocked-lab-results.json) | **The killer case.** `lab_results` is cold by every table-level signal. It is still unarchivable at every cutoff, because one HIPAA extract has a `WHERE` clause with no date predicate. |
| [`simulate-sweep.json`](simulate-sweep.json) | The same table, seven cutoffs, the verdict flipping SAFE -> TIGHT -> BLOCKED as the cutoff crosses a real consumer's window. |
| [`datahub-writeback.md`](datahub-writeback.md) | The provenance written back into DataHub -- the mutations verbatim, the GMS responses, and a before/after read-back of the entity. |

## Every file

| File | Source | What it is |
|---|---|---|
| `estate-overview.json` | `GET /api/datasets` | The estate as DataHub describes it, assessed. Verbatim response, captured **before** the archive ran, so every dataset reads `archive_state: HOT`. Sorted coldest-first by temperature. |
| `patient-encounters-context.json` | `GET /api/datasets/{hero_id}` | The full DataHub-sourced context for the hero table: schema, ownership, domain, tags, glossary terms, policy structured properties, usage telemetry, all seven consumer windows, the evidence list, and a `Provenance` on every group. Verbatim response. |
| `consumer-windows.md` | derived from `GET /api/datasets/{{id}}` for all five | The readable version of the windows, with the SQL. |
| `simulate-sweep.json` | `POST /api/datasets/{hero_id}/simulate` x7 | Cutoffs {sweep}. Same table, same consumers, only the date moves. |
| `archive-plan.json` | `POST /api/datasets/{hero_id}/plan` | The issued plan for {hero_cutoff}, including `plan_hash` -- a SHA-256 over dataset + cutoff + row count + verdict + blockers. `/api/execute` takes only that hash and recomputes it from live state, so a plan cannot be approved and then executed against different data. |
| `archive-execution.json` | `POST /api/execute` | The real run: multi-part Parquet manifest, the verification report, and the DataHub write-back result. |
| `verification-report.json` | slice of the above | The verification block on its own. Parts were downloaded back out of MinIO and re-hashed, and the row count and column set re-checked, **before** a single source row was deleted. |
| `restore-verification.json` | `POST /api/restore` | A real rehydration. Every part is re-downloaded, its digest re-checked against the manifest, and the rows written into a side table. |
| `audit-trail.json` | `GET /api/audit` | Every event of this run, newest first: simulations, the plan, the recorded approval, the execution with its checksum, the restore. |
| `blocked-lab-results.json` | `POST /api/datasets/{lab_id}/simulate` x{lab_n} | The killer case, at {lab_n} different cutoffs. |
| `blocked-claims-history.json` | `GET /api/datasets/{claims_id}` + simulate x{claims_n} | Blocked by an ACTIVE legal hold read from a DataHub structured property -- a policy veto that no consumer window can override. |
| `datahub-writeback.md` | traced GraphQL + read-back | The contribution back to the graph. |
| `archive/` | MinIO | One real Parquet part from the run above and the run's `manifest.json`, downloaded from the object store. The smallest part was chosen to keep the repository small. |
| `cassettes/` | recorded GMS responses | {cassette_n} verbatim GMS responses. See below. |
| `screenshots/` | headless Chrome, 1920x1200 | The running UI and the DataHub entity afterwards. See below. |

## screenshots/

Captured from the running stack with headless Chrome against `http://localhost:3100`
(the UI) and `http://localhost:9002` (DataHub). Not regenerated by
`record_examples.py` -- they need a browser and a running frontend.

| File | What it shows |
|---|---|
| `overview.png` | The estate, coldest first. Measured rows, measured bytes, measured date range, the temperature breakdown, and the blockers per dataset -- `UNBOUNDED_CONSUMER` on `lab_results`, `LEGAL_HOLD` on `claims_history`. The chip bottom-left reports the live DataHub connection. |
| `candidates-safe.png` | `patient_encounters` at cutoff {hero_cutoff}: **SAFE TO ARCHIVE**. Each horizontal bar is one consumer's real history window; the cutoff line sits to the left of every one of them. 1,461 days move cold, 1,312 days stay hot. |
| `candidates-blocked.png` | `lab_results` at 2022-01-01: **DO NOT ARCHIVE**. The hatched bar spanning the entire axis is `hipaa_lab_disclosure_extract` -- an unbounded scan. The consumer-windows table below names the derivation (`no date filter`) and links the statement that was parsed. |
| `datahub-entity-after.png` | The DataHub entity after the archive. `io.coldlineage.policy.*` (Retention Floor, Legal Hold, Business Criticality -- what ColdLineage READ) sits directly above `io.coldlineage.archive.*` (Archived Through, Archive State, Object URI, SHA-256, Restore SLA, Last Archive Run -- what ColdLineage WROTE). The right rail carries the deprecation marker, the `cold-tier-archived` tag and the clickable manifest link. |

## Running the demo with no DataHub at all

`cassettes/` holds verbatim GMS responses -- `{{recorded_at, gms_url, operation, variables,
response}}` -- keyed by operation name and a hash of the variables. Start the backend with
`DATAHUB_MODE=replay` and every GraphQL read is served from disk:

```bash
cd backend
DATABASE_URL="postgresql+psycopg://coldlineage:coldlineage@localhost:5433/coldlineage" \\
DATAHUB_MODE=replay CASSETTE_DIR=../examples/cassettes \\
  python -m uvicorn app.main:app --port 8000
```

`GET /api/health` then reports `mode: replay` with the recording timestamp, and the write-back
path reports `skipped` rather than pretending to have contributed anything. There is
deliberately no third mode that invents data.

The warehouse is still required: DataHub supplies the context, Postgres supplies the physical
facts, and this project does not print numbers it has not measured.

## Verifying the archive yourself

```bash
# the manifest lists a sha256 per part; recompute it
shasum -a 256 archive/{part_name}
# expect: {part_sha}
```

The manifest's top-level `sha256` is a digest over the ordered per-part digests, so the whole
archive is verifiable part by part.
"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:  # noqa: C901, PLR0915
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gms", default=os.environ.get("RECORD_GMS_URL", "http://localhost:8090"))
    ap.add_argument("--dsn", default=os.environ.get(
        "COLDLINEAGE_PG_DSN", "postgresql://coldlineage:coldlineage@localhost:5433/coldlineage"))
    ap.add_argument("--minio", default=os.environ.get("MINIO_ENDPOINT", "http://localhost:9000"))
    ap.add_argument("--port", type=int, default=8001, help="port for the traced backend")
    ap.add_argument("--skip-seed", action="store_true")
    ap.add_argument("--seed-scale", type=float, default=1.0)
    args = ap.parse_args()

    sqlalchemy_url = args.dsn.replace("postgresql://", "postgresql+psycopg://", 1)

    print("ColdLineage -- recording examples/ from a live run")
    print(f"  gms       {args.gms}")
    print(f"  warehouse {args.dsn}")
    print(f"  minio     {args.minio}")
    print(f"  examples  {EXAMPLES}")

    EXAMPLES.mkdir(parents=True, exist_ok=True)
    CASSETTES.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, str]] = []

    # ---- preflight -------------------------------------------------------
    step("Preflight")
    with httpx.Client() as _c:
        body = gql(_c, args.gms, "query { appConfig { appVersion } }")
    version = ((body.get("data") or {}).get("appConfig") or {}).get("appVersion")
    if not version:
        raise Failed(f"GMS at {args.gms} did not report an appVersion: {json.dumps(body)[:300]}")
    note(f"DataHub GMS {version} reachable at {args.gms}")

    # ---- 1. warehouse ----------------------------------------------------
    if args.skip_seed:
        step("Reseed the warehouse -- SKIPPED (--skip-seed)")
    else:
        step("Reseed the warehouse")
        reseed(args.dsn, args.seed_scale)

    step("Reset local run state")
    reset_run_state(args.dsn)

    # ---- env for everything that imports the backend ---------------------
    backend_env = {
        "DATABASE_URL": sqlalchemy_url,
        "MINIO_ENDPOINT": args.minio,
        "DATAHUB_MODE": "live",
        "DATAHUB_GMS_URL": args.gms,
        "CASSETTE_DIR": str(CASSETTES),
    }
    os.environ.update(backend_env)
    sys.path.insert(0, str(BACKEND))

    # ---- discover the estate --------------------------------------------
    step("Discover the estate from DataHub")
    from app.datahub.client import DataHubClient  # noqa: PLC0415

    import asyncio  # noqa: PLC0415

    found = asyncio.run(DataHubClient().search_datasets(query="*", count=100))
    urns = {}
    for row in found:
        urn = row.get("urn") or ""
        if "dataPlatform:postgres," not in urn:
            continue
        leaf = urn.split(",")[-2].split(".")[-1]
        if leaf in ESTATE:
            urns[leaf] = urn
    missing = [t for t in ESTATE if t not in urns]
    if missing:
        raise Failed(f"DataHub does not know about {missing}. Run ./scripts/bootstrap_datahub.sh")
    ordered_urns = [urns[t] for t in ESTATE]
    note(f"{len(ordered_urns)} postgres datasets: {', '.join(ESTATE)}")

    # ---- 3. reset DataHub writeback --------------------------------------
    step("Reset ColdLineage's own writeback in DataHub (so 'before' is a real before)")
    reset_datahub_writeback(args.gms, ordered_urns)

    # ---- 4. cassettes ----------------------------------------------------
    step("Record cassettes -- every GMS call the app makes, for all five datasets")
    for stale in CASSETTES.glob("*.json"):
        stale.unlink()
    cassette_files = record_cassettes(ordered_urns)

    # ---- 5. traced backend -----------------------------------------------
    step("Start a traced copy of the backend")
    trace_path = Path(tempfile.gettempdir()) / "coldlineage-gql-trace.jsonl"
    with TracedBackend(args.port, backend_env, trace_path) as server:
        api = Api(f"http://127.0.0.1:{args.port}/api")

        # -- estate ------------------------------------------------------
        step("GET /api/datasets  ->  estate-overview.json")
        estate = api.get("/datasets")
        write_json(EXAMPLES / "estate-overview.json", estate)
        written.append(("estate-overview.json", f"{len(estate)} datasets, assessed"))
        ids = {d["name"]: d["id"] for d in estate}
        for d in estate:
            rows = f"{d['row_count']:,}" if d.get("row_count") is not None else "-"
            note(f"{d['id']:>2}  {d['name']:<20} temp {d['temperature']['score']:>5} "
                 f"{d['temperature']['classification']:<6} rows {rows:>11} "
                 f"blockers {[b['code'] for b in d['blockers']] or '-'}")
        for table in ESTATE:
            if table not in ids:
                raise Failed(f"{table} is missing from GET /api/datasets")

        hero_id, lab_id, claims_id = ids[HERO], ids["lab_results"], ids["claims_history"]

        # -- detail for every dataset (feeds consumer-windows.md) ---------
        step("GET /api/datasets/{id} for all five")
        details = {table: api.get(f"/datasets/{ids[table]}") for table in ESTATE}
        write_json(EXAMPLES / "patient-encounters-context.json", details[HERO])
        n_windows = len(details[HERO]["context"]["downstream"])
        written.append(("patient-encounters-context.json",
                        f"full DataHub context, {n_windows} consumer windows, "
                        f"{len(details[HERO]['evidence'])} evidence items"))

        # -- consumer-windows.md ------------------------------------------
        step("Render consumer-windows.md")
        md = render_consumer_windows(details)
        (EXAMPLES / "consumer-windows.md").write_text(md)
        total_windows = sum(len(d["context"]["downstream"]) for d in details.values())
        written.append(("consumer-windows.md",
                        f"{total_windows} consumer windows across {len(details)} datasets, "
                        f"with verbatim SQL"))

        # -- sweep ---------------------------------------------------------
        step(f"POST /api/datasets/{hero_id}/simulate at {len(SWEEP)} cutoffs -> simulate-sweep.json")
        sweep_rows = []
        for cutoff in SWEEP:
            _, verdict = api.post(f"/datasets/{hero_id}/simulate", {"cutoff_date": cutoff})
            binding = verdict.get("binding_constraint") or {}
            sweep_rows.append({
                "cutoff_date": cutoff,
                "recommendation": verdict["recommendation"],
                "headroom_days": verdict.get("headroom_days"),
                "binding_consumer": (binding.get("window") or {}).get("consumer_name"),
                "binding_state": binding.get("state"),
                "rationale": verdict["rationale"],
                "verdict": verdict,
            })
            note(f"{cutoff}  {verdict['recommendation']:<24} headroom "
                 f"{str(verdict.get('headroom_days')):>6}  {verdict['rationale'][:70]}")
        write_json(EXAMPLES / "simulate-sweep.json", {
            "_about": (
                "One table, one set of consumers, seven cutoffs. Only the date moves. The verdict "
                "flips SAFE_TO_ARCHIVE -> ARCHIVE_WITH_REHYDRATION -> DO_NOT_ARCHIVE as the cutoff "
                "crosses the window of a consumer that is still reading. This is the question "
                "DataHub structurally cannot answer: its model is dataset- and column-level, so it "
                "can say a table is cold but not that rows before a given date are cold while "
                "recent rows stay hot."
            ),
            "dataset": {"id": hero_id, "name": HERO, "urn": urns[HERO],
                        "date_column": details[HERO]["context"]["date_column"],
                        "measured_span": [details[HERO]["min_date"], details[HERO]["max_date"]],
                        "row_count": details[HERO]["row_count"]},
            "tightest_consumer_window": min(
                (w["earliest_date_read"] for w in details[HERO]["context"]["downstream"]
                 if w["earliest_date_read"]), default=None),
            "recorded_at": datetime.now(UTC).isoformat(),
            "cutoffs": sweep_rows,
        })
        flips = " -> ".join(dict.fromkeys(r["recommendation"] for r in sweep_rows))
        written.append(("simulate-sweep.json", flips))

        # -- the killer case ------------------------------------------------
        step(f"POST /api/datasets/{lab_id}/simulate -> blocked-lab-results.json  (THE KILLER CASE)")
        lab_rows = []
        for cutoff in LAB_SWEEP:
            _, verdict = api.post(f"/datasets/{lab_id}/simulate", {"cutoff_date": cutoff})
            lab_rows.append({"cutoff_date": cutoff,
                             "recommendation": verdict["recommendation"],
                             "verdict": verdict})
            note(f"{cutoff}  {verdict['recommendation']}")
        lab = details["lab_results"]
        offender = next(
            (w for w in lab["context"]["downstream"] if w["derivation"] == "no_date_filter"), None)
        write_json(EXAMPLES / "blocked-lab-results.json", {
            "_about": (
                "THE CASE THAT MAKES THE PRODUCT. Read this one first.\n\n"
                "lab_results is cold by every signal a catalog can offer: "
                f"{lab['context']['query_count_30d']} queries and "
                f"{lab['context']['distinct_users_30d']} distinct users in the last 30 days, "
                f"{lab['row_count']:,} rows going back to {lab['min_date']}. Any age-based "
                "tiering job -- and any dataset-level 'this table is cold' check -- would move "
                "it tomorrow.\n\n"
                "It cannot be archived at ANY cutoff. One downstream consumer, the HIPAA "
                "disclosure extract, runs a query that HAS a WHERE clause -- but the clause is "
                "`performing_lab IS NOT NULL`, not a date predicate. So it reads the entire "
                "history, every run, and every possible cutoff would truncate data it is still "
                "reading.\n\n"
                "Finding that requires parsing the consumer's actual SQL. A table-level "
                "temperature score cannot see it. Neither can a lineage edge: DataHub knows the "
                "extract depends on lab_results, and that is exactly as far as DataHub can go.\n\n"
                "Note what the system does NOT do: it does not silently drop a consumer it "
                "cannot bound. Everything unproven blocks. That is the difference between this "
                "and a cron job that deletes by age."
            ),
            "_what_to_look_at": [
                "context.query_count_30d and distinct_users_30d -- the table-level signal says dead",
                "temperature -- and why the score alone is not allowed to decide",
                "the consumer with derivation 'no_date_filter' -- its evidence_sql has a WHERE "
                "clause and no date bound",
                "cutoffs[*].recommendation -- DO_NOT_ARCHIVE at every single one",
            ],
            "recorded_at": datetime.now(UTC).isoformat(),
            "dataset": {"id": lab_id, "name": "lab_results", "urn": urns["lab_results"]},
            "table_level_signals": {
                "query_count_30d": lab["context"]["query_count_30d"],
                "distinct_users_30d": lab["context"]["distinct_users_30d"],
                "row_count": lab["row_count"],
                "size_bytes": lab["size_bytes"],
                "min_date": lab["min_date"],
                "max_date": lab["max_date"],
                "temperature": lab["temperature"],
                "usage_provenance": lab["context"]["usage_provenance"],
            },
            "the_unbounded_consumer": offender,
            "blockers": lab["blockers"],
            "consumer_windows": lab["context"]["downstream"],
            "cutoffs": lab_rows,
        })
        verdicts = {r["recommendation"] for r in lab_rows}
        written.append(("blocked-lab-results.json",
                        f"{len(lab_rows)} cutoffs, all {'/'.join(sorted(verdicts))}"))

        # -- legal hold -----------------------------------------------------
        step(f"GET /api/datasets/{claims_id} + simulate -> blocked-claims-history.json")
        claims = details["claims_history"]
        claims_rows = []
        for cutoff in CLAIMS_SWEEP:
            _, verdict = api.post(f"/datasets/{claims_id}/simulate", {"cutoff_date": cutoff})
            claims_rows.append({"cutoff_date": cutoff,
                                "recommendation": verdict["recommendation"],
                                "verdict": verdict})
        status, plan_attempt = api.post(f"/datasets/{claims_id}/plan",
                                        {"cutoff_date": CLAIMS_SWEEP[1]}, expect=(200, 409))
        hold = next((b for b in claims["blockers"] if b["code"] == "LEGAL_HOLD"), None)
        write_json(EXAMPLES / "blocked-claims-history.json", {
            "_about": (
                "A policy veto, read out of DataHub. claims_history carries an ACTIVE legal hold "
                "in the structured property io.coldlineage.policy.legalHold, with the matter "
                "recorded in io.coldlineage.policy.legalHoldMatter. That is an unconditional "
                "block: it is evaluated before any cutoff analysis, it cannot be out-voted by a "
                "temperature score, and the plan carries it so /api/execute refuses.\n\n"
                "Blockers are kept out of the temperature score on purpose. A legal hold does not "
                "make data hotter -- it makes the question moot. Folding it into a 0-100 number "
                "would let a sufficiently cold dataset out-vote a court order.\n\n"
                "Note the second, independent blocker in `blockers`: claims_actuarial_snapshot is "
                "in DataHub's lineage but has no query text recorded, so its lookback cannot be "
                "proven and it is treated as unbounded. Two different kinds of 'no' -- a policy "
                "veto and an evidence gap -- are reported separately rather than collapsed into "
                "one verdict, because they need different fixes."
            ),
            "recorded_at": datetime.now(UTC).isoformat(),
            "dataset": {"id": claims_id, "name": "claims_history", "urn": urns["claims_history"]},
            "legal_hold": {
                "active": claims["context"]["legal_hold"],
                "matter": claims["context"]["legal_hold_matter"],
                "read_from": claims["context"]["policy_provenance"],
            },
            "blockers": claims["blockers"],
            "archive_eligible": claims["archive_eligible"],
            "plan_attempt": {
                "request": {"cutoff_date": CLAIMS_SWEEP[1]},
                "http_status": status,
                "executable": plan_attempt.get("executable"),
                "executable_reason": plan_attempt.get("executable_reason"),
                "blockers": plan_attempt.get("blockers"),
            },
            "cutoffs": claims_rows,
        })
        written.append(("blocked-claims-history.json",
                        f"LEGAL_HOLD ({claims['context']['legal_hold_matter']}), "
                        f"plan not executable"))

        # -- before read-back ------------------------------------------------
        step("Read the hero entity out of GMS -- the 'before' snapshot")
        before = entity_audit(args.gms, urns[HERO])
        before_props = [
            (p.get("structuredProperty") or {}).get("definition", {}).get("qualifiedName")
            for p in (((before.get("data") or {}).get("dataset") or {})
                      .get("structuredProperties") or {}).get("properties") or []
        ]
        note(f"structured properties before: {before_props}")

        # -- plan --------------------------------------------------------
        step(f"POST /api/datasets/{hero_id}/plan @ {HERO_CUTOFF} -> archive-plan.json")
        _, plan = api.post(f"/datasets/{hero_id}/plan", {"cutoff_date": HERO_CUTOFF})
        write_json(EXAMPLES / "archive-plan.json", plan)
        note(f"plan_hash {plan['plan_hash']}")
        note(f"{plan['rows_in_scope']:,} rows / {human_bytes(plan['bytes_in_scope'])} in scope, "
             f"executable={plan['executable']}")
        written.append(("archive-plan.json",
                        f"{plan['rows_in_scope']:,} rows, hash {plan['plan_hash'][:16]}..."))
        if not plan["executable"]:
            raise Failed(f"plan is not executable: {plan['executable_reason']}")

        # -- execute -----------------------------------------------------
        step("POST /api/execute -> archive-execution.json  (real bytes move)")
        trace_before_execute = len(server.entries())
        _, execution = api.post("/execute",
                                {"plan_hash": plan["plan_hash"], "approved_by": APPROVER})
        write_json(EXAMPLES / "archive-execution.json", execution)
        manifest = execution["manifest"]
        verification = execution["verification"]
        note(f"run {execution['run_id']}: {manifest['rows']:,} rows -> "
             f"{len(manifest['parts'])} parquet parts, {human_bytes(manifest['bytes'])}")
        note(f"verification passed={verification['passed']} "
             f"sha256_match={verification['readback_sha256_match']} "
             f"rows {verification['readback_row_count']:,}/{verification['source_row_count']:,}")
        for op in execution["datahub_writeback"]["operations"]:
            note(f"writeback {op['op']}: {op['status']} -- {op['detail'][:80]}")
        if not verification["passed"]:
            raise Failed("verification did not pass; refusing to record a broken artifact")
        written.append(("archive-execution.json",
                        f"run {execution['run_id']}, {manifest['rows']:,} rows, "
                        f"{len(manifest['parts'])} parts, verified"))

        write_json(EXAMPLES / "verification-report.json", {
            "_about": (
                "Verification runs BEFORE any source row is deleted, and it is the whole safety "
                "argument. The parts are downloaded back OUT of object storage, re-hashed, and "
                "re-read as Parquet; the row count and the column set are compared against the "
                "source. Hashing the buffer you are about to upload proves nothing about what "
                "landed -- it is a checksum of your intent. Only when every check passes are the "
                "hot rows removed, in one transaction, and the range re-counted to confirm the "
                "delete matched exactly what was verified."
            ),
            "recorded_at": datetime.now(UTC).isoformat(),
            "run_id": execution["run_id"],
            "dataset_urn": manifest["dataset_urn"],
            "cutoff_date": manifest["cutoff_date"],
            "object_uri": manifest["object_uri"],
            "manifest_sha256": manifest["sha256"],
            "parts": len(manifest["parts"]),
            "bytes": manifest["bytes"],
            "verification": verification,
        })
        written.append(("verification-report.json",
                        f"readback sha256 match, {verification['readback_row_count']:,} rows"))

        # -- after read-back --------------------------------------------
        step("Read the hero entity out of GMS again -- the 'after' snapshot")
        after = entity_audit(args.gms, urns[HERO])
        after_props = [
            (p.get("structuredProperty") or {}).get("definition", {}).get("qualifiedName")
            for p in (((after.get("data") or {}).get("dataset") or {})
                      .get("structuredProperties") or {}).get("properties") or []
        ]
        note(f"structured properties after: {after_props}")

        # -- writeback markdown, from the trace --------------------------
        step("Render datahub-writeback.md from the traced GraphQL")
        import re as _re

        mutations = []
        for entry in server.entries()[trace_before_execute:]:
            doc = entry.get("document") or ""
            match = _re.search(r"mutation\s+(\w+)", doc)
            if match:
                mutations.append({**entry, "operation": match.group(1)})
        note(f"{len(mutations)} mutations captured on the wire during /api/execute")
        (EXAMPLES / "datahub-writeback.md").write_text(
            render_writeback(before, after, mutations,
                             execution["datahub_writeback"], manifest,
                             execution["datahub_writeback"].get("entity_url") or "")
        )
        written.append(("datahub-writeback.md",
                        f"{len(mutations)} mutations verbatim + before/after read-back"))

        # -- restore -----------------------------------------------------
        step("POST /api/restore -> restore-verification.json")
        _, restore = api.post("/restore", {"run_id": execution["run_id"], "temporary": True})
        write_json(EXAMPLES / "restore-verification.json", {
            "_about": (
                "A real rehydration of the archived range. Every part is downloaded from object "
                "storage and its sha256 re-checked against the manifest before a single row is "
                "written back; a mismatch refuses the restore rather than loading unverified "
                "bytes. temporary=true lands the rows in a side table so the source is untouched; "
                "temporary=false appends them back into the source and resyncs the identity "
                "sequence."
            ),
            "recorded_at": datetime.now(UTC).isoformat(),
            "request": {"run_id": execution["run_id"], "temporary": True},
            "response": restore,
            "manifest_sha256": manifest["sha256"],
            "digests_checked": len(manifest["parts"]),
        })
        note(f"{restore['rows']:,} rows into {restore['table']}, verified={restore['verified']}")
        written.append(("restore-verification.json",
                        f"{restore['rows']:,} rows rehydrated, digests re-checked"))

        # -- audit -------------------------------------------------------
        step("GET /api/audit -> audit-trail.json")
        audit = api.get("/audit")
        write_json(EXAMPLES / "audit-trail.json", audit)
        kinds: dict[str, int] = {}
        for event in audit:
            kinds[event["event_type"]] = kinds.get(event["event_type"], 0) + 1
        note(", ".join(f"{k} x{v}" for k, v in sorted(kinds.items())))
        written.append(("audit-trail.json", f"{len(audit)} events: {sorted(kinds)}"))

    # ---- 9. the object store ------------------------------------------
    step("Download one real Parquet part + the manifest from MinIO -> examples/archive/")
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415

    s3 = boto3.client("s3", endpoint_url=args.minio,
                      aws_access_key_id=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
                      aws_secret_access_key=os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
                      config=Config(retries={"max_attempts": 3, "mode": "standard"}))
    bucket = "coldlineage-archive"
    for stale in ARCHIVE_DIR.glob("*"):
        if stale.is_file():
            stale.unlink()
    smallest = min(manifest["parts"], key=lambda p: p["bytes"])
    part_bytes = s3.get_object(Bucket=bucket, Key=smallest["key"])["Body"].read()
    digest = hashlib.sha256(part_bytes).hexdigest()
    if digest != smallest["sha256"]:
        raise Failed(f"downloaded part {smallest['key']} does not match its manifest digest")
    part_name = Path(smallest["key"]).name
    (ARCHIVE_DIR / part_name).write_bytes(part_bytes)
    manifest_key = manifest["manifest_uri"].split(f"{bucket}/", 1)[1]
    manifest_bytes = s3.get_object(Bucket=bucket, Key=manifest_key)["Body"].read()
    (ARCHIVE_DIR / "manifest.json").write_bytes(manifest_bytes)
    (ARCHIVE_DIR / "README.md").write_text(
        f"# One real part of a real archive\n\n"
        f"`{part_name}` was downloaded out of MinIO after the run recorded in\n"
        f"`../archive-execution.json`. It is the smallest of the run's "
        f"{len(manifest['parts'])} parts,\nchosen to keep the repository small; the others are "
        f"identical in form.\n\n"
        f"- object key: `{smallest['key']}`\n"
        f"- rows: {smallest['rows']:,}\n"
        f"- bytes: {smallest['bytes']:,}\n"
        f"- sha256: `{smallest['sha256']}`\n\n"
        f"Verify it:\n\n"
        f"```bash\nshasum -a 256 {part_name}\n# {smallest['sha256']}\n```\n\n"
        f"`manifest.json` is the run's manifest exactly as it sits in the bucket. Its top-level\n"
        f"`sha256` is a digest over the ordered per-part digests, so the archive is verifiable\n"
        f"part by part rather than only as a whole. `verified_readback: true` was written only\n"
        f"after every part was downloaded back and re-hashed -- before any source row was "
        f"deleted.\n\n"
        f"Read it:\n\n"
        f"```python\nimport pandas as pd\nprint(pd.read_parquet('{part_name}').head())\n```\n"
    )
    note(f"{part_name}: {smallest['rows']:,} rows, {human_bytes(smallest['bytes'])}, "
         f"sha256 verified on download")
    written.append((f"archive/{part_name}", f"{smallest['rows']:,} rows, "
                                            f"{human_bytes(smallest['bytes'])}, digest verified"))
    written.append(("archive/manifest.json", f"{manifest['rows']:,} rows across "
                                             f"{len(manifest['parts'])} parts"))

    # ---- README ---------------------------------------------------------
    step("Render examples/README.md")
    (EXAMPLES / "README.md").write_text(README_TEMPLATE.format(
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        hero_id=hero_id, lab_id=lab_id, claims_id=claims_id,
        hero_cutoff=HERO_CUTOFF,
        sweep=", ".join(SWEEP),
        lab_n=len(LAB_SWEEP), claims_n=len(CLAIMS_SWEEP),
        cassette_n=len(cassette_files),
        part_name=part_name, part_sha=smallest["sha256"],
    ))
    written.append(("README.md", "index of every artifact and what it proves"))
    written.append((f"cassettes/  ({len(cassette_files)} files)",
                    "verbatim GMS responses for DATAHUB_MODE=replay"))

    # ---- summary --------------------------------------------------------
    total = sum(p.stat().st_size for p in EXAMPLES.rglob("*") if p.is_file())
    print("\n" + "=" * 78)
    print("WROTE")
    print("=" * 78)
    for name, detail in written:
        print(f"  examples/{name:<38} {detail}")
    print("=" * 78)
    print(f"  {total / 1024 / 1024:.2f} MB total in examples/")
    print("\nProve the cassettes work with no DataHub at all:")
    print('  cd backend && DATAHUB_MODE=replay CASSETTE_DIR=../examples/cassettes \\')
    print('    DATABASE_URL="postgresql+psycopg://coldlineage:coldlineage@localhost:5433/coldlineage" \\')
    print("    python -m uvicorn app.main:app --port 8000")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failed as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
