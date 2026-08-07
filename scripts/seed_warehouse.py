#!/usr/bin/env python
"""Create the ColdLineage demo estate in Postgres.

Builds the five tables declared in scripts/estate.py with real DDL, real date
columns and enough volume that the measured physical sizes are credible, then
reports what was actually created.

    python scripts/seed_warehouse.py
    python scripts/seed_warehouse.py --scale 0.02        # fast smoke run
    python scripts/seed_warehouse.py --only lab_results  # rebuild one table
    python scripts/seed_warehouse.py --json out.json     # machine-readable summary

MEASUREMENT, NOT ESTIMATION
---------------------------
The row data is synthetic. The SIZES ARE NOT. Every byte figure printed here comes
from pg_total_relation_size / pg_relation_size / pg_indexes_size after the table is
written and analysed. Nothing in this script declares how big a table is; Postgres
is asked. If you cannot measure it, do not print it -- that rule is why the summary
table has no "estimated" column.

Idempotent: each table is dropped and recreated. Re-running is safe and produces the
same estate for the same --seed and the same COLDLINEAGE_ANCHOR_DATE.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from estate import ANCHOR, PG_SCHEMA, TABLES, TableSpec  # noqa: E402

try:
    import psycopg
except ImportError:  # pragma: no cover
    print(
        "psycopg is not installed.\n"
        "  pip install -r scripts/requirements-seed.txt",
        file=sys.stderr,
    )
    raise SystemExit(2)


DEFAULT_DSN = os.environ.get(
    "COLDLINEAGE_PG_DSN",
    "postgresql://coldlineage:coldlineage@localhost:5433/coldlineage",
)


# --------------------------------------------------------------------------
# Value generation
# --------------------------------------------------------------------------


class RowFactory:
    """Builds tuples for one table's COPY stream.

    Deliberately closure-based: resolving the generator kind per column once and
    then calling a list of thunks per row is roughly 4x faster than dispatching on
    a string inside the loop, and 3.6M rows makes that difference visible.
    """

    def __init__(self, spec: TableSpec, rng: random.Random) -> None:
        self.spec = spec
        self.rng = rng
        self.span_days = (spec.end - spec.start).days
        if self.span_days < 1:
            raise ValueError(f"{spec.key}: end date must be after start date")
        self.start_ord = spec.start.toordinal()
        self.growth = spec.growth
        # Inverse CDF for the linear-growth density f(t) = 1 + g*t on [0,1].
        # More recent years hold more rows, which is how estates actually look and
        # which is what makes an old-history cutoff interesting rather than trivial.
        self._norm = 1.0 + self.growth / 2.0
        self._date_col_index = next(
            i for i, c in enumerate(spec.columns) if c.name == spec.date_column
        )
        self._builders = [self._builder(c) for c in spec.columns]

    def _sample_offset(self) -> int:
        u = self.rng.random()
        if self.growth <= 0:
            t = u
        else:
            g = self.growth
            t = (-1.0 + math.sqrt(1.0 + 2.0 * g * u * self._norm)) / g
        return int(t * self.span_days)

    def _builder(self, col):  # noqa: ANN001, C901
        rng = self.rng
        kind = col.gen
        args = col.gen_args

        if kind == "date":
            return None  # handled specially: drives every date-derived column
        if kind == "choice":
            pool = args[0]
            n = len(pool)
            if n == 1:
                only = pool[0]
                return lambda _o: only
            return lambda _o: pool[rng.randrange(n)]
        if kind == "paired_choice":
            # Columns that must agree row-to-row (loinc_code / analyte / units share
            # an index) draw from a shared per-row index stashed on the factory.
            pool = args[0]
            return lambda _o: pool[self._paired_idx]
        if kind == "int":
            lo, hi = args
            return lambda _o: rng.randint(lo, hi)
        if kind == "money":
            lo, hi = args
            return lambda _o: round(rng.uniform(lo, hi), 2)
        if kind == "bool":
            p = args[0]
            return lambda _o: rng.random() < p
        if kind == "patient":
            return lambda _o: f"P-{rng.randrange(1, 240_000):06d}"
        if kind == "npi":
            return lambda _o: f"{rng.randrange(1_000_000_000, 1_999_999_999)}"
        if kind == "claimno":
            return lambda _o: f"CLM-{rng.randrange(10_000_000, 99_999_999)}"
        if kind == "specimen":
            return lambda _o: f"SP{rng.randrange(100_000_000, 999_999_999)}"
        if kind == "account":
            return lambda _o: f"ACCT-{rng.randrange(1, 900_000):06d}"
        if kind == "invoice":
            return lambda _o: f"INV-{rng.randrange(1_000_000, 9_999_999)}"
        if kind == "date_offset":
            lo, hi = args
            return lambda o: date.fromordinal(self.start_ord + o + rng.randint(lo, hi))
        if kind == "ts_from_date":
            return lambda o: datetime.combine(
                date.fromordinal(self.start_ord + o),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ) + timedelta(seconds=rng.randrange(0, 86_400))
        return lambda _o: None

    def rows(self, count: int):
        rng = self.rng
        builders = self._builders
        date_idx = self._date_col_index
        start_ord = self.start_ord
        n_paired = 12  # len(LOINC_CODES) == len(ANALYTES) == len(RESULT_UNITS)
        for _ in range(count):
            offset = self._sample_offset()
            self._paired_idx = rng.randrange(n_paired)
            row = []
            for i, b in enumerate(builders):
                if i == date_idx:
                    row.append(date.fromordinal(start_ord + offset))
                else:
                    row.append(b(offset))
            yield tuple(row)


# --------------------------------------------------------------------------
# Build / measure
# --------------------------------------------------------------------------


def build_table(conn, spec: TableSpec, rows: int, rng: random.Random, batch: int) -> float:
    started = time.perf_counter()
    cols = spec.copy_columns
    collist = ", ".join(f'"{c}"' for c in cols)
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{PG_SCHEMA}"."{spec.key}" CASCADE')
        cur.execute(spec.create_sql)

    factory = RowFactory(spec, rng)
    written = 0
    # Progress only when someone is watching; keeps piped logs clean.
    show_progress = sys.stdout.isatty() and rows > batch
    with conn.cursor() as cur:
        copy_sql = f'COPY "{PG_SCHEMA}"."{spec.key}" ({collist}) FROM STDIN'
        with cur.copy(copy_sql) as copy:
            for row in factory.rows(rows):
                copy.write_row(row)
                written += 1
                if show_progress and written % batch == 0:
                    print(f"    ... {written:,}/{rows:,}".ljust(48), end="\r", flush=True)
    if show_progress:
        print(" " * 48, end="\r", flush=True)

    with conn.cursor() as cur:
        for stmt in spec.index_sql:
            cur.execute(stmt)
        cur.execute(f'ANALYZE "{PG_SCHEMA}"."{spec.key}"')
    conn.commit()
    return time.perf_counter() - started


def measure(conn, spec: TableSpec) -> dict:
    """Ask Postgres. Never guess."""
    dc = spec.date_column
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT count(*), min("{dc}"), max("{dc}") FROM "{PG_SCHEMA}"."{spec.key}"'
        )
        row_count, min_date, max_date = cur.fetchone()
        cur.execute(
            "SELECT pg_total_relation_size(%s), pg_relation_size(%s), pg_indexes_size(%s)",
            (f"{PG_SCHEMA}.{spec.key}",) * 3,
        )
        total_bytes, heap_bytes, index_bytes = cur.fetchone()
    return {
        "table": spec.key,
        "qualified_table": spec.dotted,
        "datahub_urn": spec.urn,
        "date_column": dc,
        "row_count": int(row_count),
        "min_date": min_date.isoformat() if min_date else None,
        "max_date": max_date.isoformat() if max_date else None,
        "total_bytes": int(total_bytes),
        "heap_bytes": int(heap_bytes),
        "index_bytes": int(index_bytes),
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "measurement_source": "pg_total_relation_size / pg_relation_size / pg_indexes_size",
        "demo_role": spec.demo_role,
        "synthetic_rows": True,
    }


def rows_before(conn, spec: TableSpec, cutoff: date) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f'SELECT count(*) FROM "{PG_SCHEMA}"."{spec.key}" WHERE "{spec.date_column}" < %s',
            (cutoff,),
        )
        return int(cur.fetchone()[0])


def human_bytes(n: int) -> str:
    step = 1024.0
    v = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if v < step or unit == "TiB":
            return f"{v:,.1f} {unit}" if unit != "B" else f"{int(v)} B"
        v /= step
    return f"{v:.1f} TiB"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=DEFAULT_DSN,
                    help=f"Postgres DSN (default: {DEFAULT_DSN})")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="Multiply every table's row target. 0.02 gives a fast smoke run.")
    ap.add_argument("--only", action="append", default=None, metavar="TABLE",
                    help="Rebuild only these tables. Repeatable.")
    ap.add_argument("--seed", type=int, default=20260810, help="RNG seed.")
    ap.add_argument("--batch", type=int, default=100_000, help="Progress print interval.")
    ap.add_argument("--json", default=None, metavar="PATH",
                    help="Also write the measurement summary as JSON.")
    args = ap.parse_args()

    selected = TABLES
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {t.key for t in TABLES}
        if unknown:
            print(f"Unknown table(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            print(f"Known: {', '.join(t.key for t in TABLES)}", file=sys.stderr)
            return 2
        selected = tuple(t for t in TABLES if t.key in wanted)

    print("ColdLineage -- seeding the demo warehouse")
    print(f"  dsn          : {args.dsn.split('@')[-1]}")
    print(f"  anchor date  : {ANCHOR.isoformat()}"
          + ("" if os.environ.get("COLDLINEAGE_ANCHOR_DATE")
             else "  (today; set COLDLINEAGE_ANCHOR_DATE to pin it)"))
    print(f"  scale        : {args.scale}")
    print(f"  tables       : {', '.join(t.key for t in selected)}")
    print()

    try:
        conn = psycopg.connect(args.dsn, autocommit=False, connect_timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot reach Postgres at {args.dsn.split('@')[-1]}: {exc}", file=sys.stderr)
        print("\nStart it with:  docker compose up -d postgres", file=sys.stderr)
        return 1

    results: list[dict] = []
    total_started = time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{PG_SCHEMA}"')
            cur.execute("SELECT version()")
            server = cur.fetchone()[0].split(",")[0]
        conn.commit()
        print(f"  server       : {server}\n")

        for spec in selected:
            target = max(1, int(spec.rows * args.scale))
            print(f"  {spec.key} -- {target:,} rows, {spec.date_column} "
                  f"{spec.start.isoformat()} .. {spec.end.isoformat()}")
            rng = random.Random(f"{args.seed}:{spec.key}")
            elapsed = build_table(conn, spec, target, rng, args.batch)
            m = measure(conn, spec)
            m["build_seconds"] = round(elapsed, 2)
            results.append(m)
            print(f"    built in {elapsed:,.1f}s -> {m['row_count']:,} rows, "
                  f"{human_bytes(m['total_bytes'])} total "
                  f"({human_bytes(m['heap_bytes'])} heap + "
                  f"{human_bytes(m['index_bytes'])} indexes)")
            print()
    finally:
        pass

    # ---------------- summary ----------------
    hdr = ("table", "rows", "measured bytes", "heap", "indexes",
           "date column", "min date", "max date")
    body = [
        (
            r["table"],
            f"{r['row_count']:,}",
            f"{r['total_bytes']:,}",
            human_bytes(r["heap_bytes"]),
            human_bytes(r["index_bytes"]),
            r["date_column"],
            r["min_date"] or "-",
            r["max_date"] or "-",
        )
        for r in results
    ]
    widths = [max(len(str(x[i])) for x in (*body, hdr)) for i in range(len(hdr))]
    sep = "-+-".join("-" * w for w in widths)
    print("MEASURED ESTATE  (sizes from pg_total_relation_size -- nothing here is estimated)")
    print(" | ".join(h.ljust(w) for h, w in zip(hdr, widths)))
    print(sep)
    for r in body:
        print(" | ".join(str(v).ljust(w) for v, w in zip(r, widths)))
    print(sep)
    tot_rows = sum(r["row_count"] for r in results)
    tot_bytes = sum(r["total_bytes"] for r in results)
    print(f"{'TOTAL'.ljust(widths[0])} | {format(tot_rows, ',').ljust(widths[1])} | "
          f"{format(tot_bytes, ',').ljust(widths[2])} | "
          f"{human_bytes(tot_bytes)}")
    print()

    # ---------------- what the demo will hinge on ----------------
    print("ARCHIVABLE HISTORY IN SCOPE  (rows strictly before the illustrative cutoff)")
    probes: list[tuple[str, date, str]] = [
        ("patient_encounters", date(2023, 1, 1),
         "hero cutoff; earliest consumer read is 2024-01-01"),
        ("claims_history", date(2020, 1, 1),
         "range looks clear -- ACTIVE legal hold must still veto"),
        ("care_events_live", ANCHOR - timedelta(days=730),
         "2-year retention floor lands before the table even starts"),
        ("lab_results", date(2023, 1, 1),
         "looks archivable; unbounded HIPAA extract must block it"),
        ("billing_ledger", date(2022, 1, 1),
         "consumers clear it; 7-year retention floor must not"),
    ]
    by_key = {r["table"]: r for r in results}
    for key, cutoff, why in probes:
        if key not in by_key:
            continue
        spec = next(t for t in TABLES if t.key == key)
        n = rows_before(conn, spec, cutoff)
        total = by_key[key]["row_count"]
        pct = (n / total * 100.0) if total else 0.0
        approx = int(by_key[key]["total_bytes"] * (n / total)) if total else 0
        print(f"  {key:<20} < {cutoff.isoformat()}  {n:>9,} rows "
              f"({pct:5.1f}%, ~{human_bytes(approx)})   {why}")
    print()

    conn.close()

    print(f"Done in {time.perf_counter() - total_started:,.1f}s. "
          f"{len(results)} table(s), {tot_rows:,} rows, {human_bytes(tot_bytes)} measured.")
    print("Rows are synthetic. Sizes, counts and date ranges above are measured from Postgres.")

    if args.json:
        payload = {
            "anchor_date": ANCHOR.isoformat(),
            "seed": args.seed,
            "scale": args.scale,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tables": results,
        }
        Path(args.json).write_text(json.dumps(payload, indent=2))
        print(f"Wrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
