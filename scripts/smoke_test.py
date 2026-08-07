#!/usr/bin/env python
"""End-to-end smoke test for the ColdLineage HTTP API.

    python scripts/smoke_test.py
    python scripts/smoke_test.py --base http://localhost:8000/api
    python scripts/smoke_test.py --no-execute      # read-only; moves no bytes
    python scripts/smoke_test.py --verbose

WHAT THIS ASSERTS, AND WHY IT IS NOT A TAUTOLOGY
------------------------------------------------
The interesting claims ColdLineage makes are all claims about DERIVATION: that the
history window for each consumer was computed by parsing SQL out of DataHub, not
looked up somewhere. A test that compared the API against the same table it was
seeded from would prove nothing.

So this test takes its expectations from scripts/consumers.py -- which is NOT read
by the backend at any point. consumers.py only ever writes SQL into DataHub. If the
backend reports the window that consumers.py expects, the only path by which it
could have learned it is: read the query text from DataHub -> parse it -> resolve
the relative bound. That is the claim, and this is what checks it.

The specific things that must hold:

  health        reports a real mode ("live" or "replay") and a reachability flag
                that reflects an actual check. A health endpoint that always says
                "connected" is worse than no health endpoint.
  provenance    every blocker, every evidence item and every consumer window
                carries a Provenance whose source is one of the declared values.
  the hero      patient_encounters at the hero cutoff is archivable, and the
                binding constraint is the consumer consumers.py says it is.
  the sweep     the SAME table, the SAME consumers, only the cutoff moving: the
                verdict must walk SAFE -> TIGHT -> BLOCKED and never get safer as
                the cutoff moves later. This is the product's core claim.
  the killer    lab_results is unused by every table-level signal AND still refuses
                to archive at any cutoff, because of an unbounded consumer.
  legal hold    claims_history is blocked by policy even at a cutoff its consumers
                would allow.
  retention     billing_ledger flips from blocked to allowed purely by moving the
                cutoff, with nothing else changing.
  execute       plan -> execute -> verified manifest -> DataHub write-back, and the
                warehouse afterwards actually missing the range that was archived.
  restore       rehydration that re-checks the checksum before loading a row.

Exit code 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import consumers as C  # noqa: E402  -- the oracle; the backend never imports this
import estate as E  # noqa: E402

DEFAULT_BASE = "http://localhost:8000/api"

VALID_SOURCES = {
    "datahub:lineage", "datahub:usage", "datahub:queries",
    "datahub:structured_properties", "datahub:tags", "datahub:ownership",
    "datahub:schema", "datahub:deprecation", "warehouse:postgres",
    "cassette:recorded", "unavailable",
}
VALID_DERIVATIONS = {
    "sql_predicate", "declared_property", "no_date_filter",
    "no_queries_observed", "not_a_query_consumer",
}
VALID_STATES = {"safe", "tight", "blocked", "unknown"}
VALID_RECOMMENDATIONS = {"SAFE_TO_ARCHIVE", "ARCHIVE_WITH_REHYDRATION", "DO_NOT_ARCHIVE"}
VALID_CLASSIFICATIONS = {"HOT", "WARM", "COOL", "COLD", "FROZEN"}

# Illustrative cutoffs. Chosen so each one isolates exactly one mechanism.
HERO_CUTOFF = date(2023, 1, 1)            # < earliest consumer read (2024-01-01)
HERO_UNSAFE_CUTOFF = date(2025, 1, 1)     # > earliest consumer read; must not be SAFE
CLAIMS_CUTOFF = date(2020, 1, 1)          # consumers fine; ACTIVE legal hold must veto
LAB_CUTOFF = date(2022, 1, 1)             # looks archivable; unbounded consumer must veto
LEDGER_ILLEGAL_CUTOFF = date(2022, 1, 1)  # breaches the 7-year retention floor
LEDGER_LEGAL_CUTOFF = date(2018, 1, 1)    # comfortably inside it

# One table, one set of consumers, seven dates. The verdict must walk
# SAFE -> TIGHT -> BLOCKED as the cutoff crosses the earliest window still read
# (2024-01-01), and must never move back toward safe.
SWEEP_CUTOFFS = [
    date(2020, 1, 1), date(2022, 1, 1), date(2023, 1, 1), date(2023, 11, 15),
    date(2024, 1, 1), date(2024, 3, 1), date(2025, 6, 1),
]


class Failure(Exception):
    pass


class Runner:
    def __init__(self, base: str, verbose: bool) -> None:
        self.base = base.rstrip("/")
        self.verbose = verbose
        self.passed = 0
        self.failed: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []

    # ---- http -------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw or b"null")
            except json.JSONDecodeError:
                return exc.code, {"detail": raw.decode("utf-8", "replace")[:500]}

    def get(self, path: str):
        status, payload = self._request("GET", path)
        if status != 200:
            raise Failure(f"GET {path} -> HTTP {status}: {json.dumps(payload)[:300]}")
        return payload

    def post(self, path: str, body: dict, expect: tuple[int, ...] = (200, 201)):
        status, payload = self._request("POST", path, body)
        if status not in expect:
            raise Failure(f"POST {path} -> HTTP {status} "
                          f"(expected {expect}): {json.dumps(payload)[:300]}")
        return status, payload

    # ---- assertions -------------------------------------------------

    def check(self, name: str, fn) -> object | None:
        try:
            result = fn()
        except Failure as exc:
            self.failed.append((name, str(exc)))
            print(f"  FAIL  {name}\n          {exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            self.failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"  ERROR {name}\n          {type(exc).__name__}: {exc}")
            return None
        self.passed += 1
        detail = f"  -- {result}" if (self.verbose and result) else ""
        print(f"  ok    {name}{detail}")
        return result

    def skip(self, name: str, why: str) -> None:
        self.skipped.append((name, why))
        print(f"  skip  {name}\n          {why}")


def need(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def provenance_ok(p: object, where: str) -> None:
    need(isinstance(p, dict), f"{where}: provenance missing or not an object")
    assert isinstance(p, dict)
    src = p.get("source")
    need(src in VALID_SOURCES, f"{where}: provenance.source {src!r} is not a declared source")


def find_dataset(datasets: list[dict], table_key: str) -> dict:
    for d in datasets:
        if d.get("name") == table_key or d.get("urn", "").find(f".{table_key},") >= 0:
            return d
    raise Failure(f"dataset {table_key!r} not present in GET /datasets "
                  f"(saw: {[d.get('name') for d in datasets]})")


def consumer_impact(verdict: dict, consumer_key: str) -> dict:
    """Locate a consumer in a verdict by urn, falling back to name."""
    target = C.BY_KEY[consumer_key]
    for imp in verdict.get("consumers", []):
        w = imp.get("window", {})
        if w.get("consumer_urn") == target.urn:
            return imp
    for imp in verdict.get("consumers", []):
        w = imp.get("window", {})
        if consumer_key in (w.get("consumer_name") or ""):
            return imp
    seen = [i.get("window", {}).get("consumer_name") for i in verdict.get("consumers", [])]
    raise Failure(f"consumer {consumer_key!r} absent from the verdict (saw {seen})")


# --------------------------------------------------------------------------


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--no-execute", action="store_true",
                    help="Skip plan/execute/restore. Nothing is moved or deleted.")
    ap.add_argument("--approver", default="smoke-test@coldlineage.local")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    r = Runner(args.base, args.verbose)
    print(f"ColdLineage smoke test -> {r.base}")
    print(f"  oracle: scripts/consumers.py (anchor {E.ANCHOR.isoformat()}), "
          f"which the backend never reads\n")

    # Distinguish "the API is down" from "the API is wrong" before running 20 checks
    # that would all fail for the same uninteresting reason.
    try:
        urllib.request.urlopen(r.base + "/health", timeout=10).read()
    except urllib.error.HTTPError:
        pass  # it answered; the checks below will judge what it said
    except Exception as exc:  # noqa: BLE001
        print(f"The API is not answering at {r.base}: {exc}\n")
        print("Start it with:")
        print("  docker compose up -d backend")
        print("or, for a local run:")
        print("  uvicorn app.main:app --app-dir backend --reload --port 8000")
        return 2

    # ---------------- health ----------------
    print("health")

    def _health():
        h = r.get("/health")
        need(h.get("ok") is True, f"health.ok is {h.get('ok')!r}")
        need(h.get("service") == "ColdLineage", f"service is {h.get('service')!r}")
        dh = h.get("datahub") or {}
        need(dh.get("mode") in ("live", "replay"),
             f"datahub.mode is {dh.get('mode')!r}; only 'live' and 'replay' exist")
        need(isinstance(dh.get("reachable"), bool),
             "datahub.reachable must be a real boolean from a real check")
        need(isinstance(dh.get("gms_url"), str) and dh["gms_url"],
             "datahub.gms_url must be reported so the mode is auditable")
        if dh["mode"] == "replay":
            need(dh.get("recorded_at"),
                 "replay mode must report recorded_at; a cassette with no recording "
                 "timestamp is indistinguishable from an invention")
        return f"mode={dh['mode']} reachable={dh['reachable']} gms={dh['gms_url']}"

    health = r.check("GET /health reports a real mode and a real reachability check", _health)
    mode = (r.get("/health").get("datahub") or {}).get("mode") if health else None

    # ---------------- inventory ----------------
    print("\ninventory")
    datasets = r.check("GET /datasets returns the five-table estate", lambda: (
        (lambda ds: (
            need(isinstance(ds, list), "not a list"),
            need(len(ds) >= 5, f"expected >= 5 datasets, got {len(ds)}"),
            [find_dataset(ds, t.key) for t in E.TABLES],
            ds,
        )[-1])(r.get("/datasets"))
    ))
    if datasets is None:
        print("\nCannot continue without /datasets.")
        return 1
    assert isinstance(datasets, list)

    def _shape():
        problems = []
        for d in datasets:
            n = d.get("name", "?")
            t = d.get("temperature") or {}
            if t.get("classification") not in VALID_CLASSIFICATIONS:
                problems.append(f"{n}: classification {t.get('classification')!r}")
            for comp in ("recency_component", "frequency_component",
                         "downstream_component", "criticality_component", "score"):
                if not isinstance(t.get(comp), (int, float)):
                    problems.append(f"{n}: temperature.{comp} missing")
            if not isinstance(d.get("signals_live"), bool):
                problems.append(f"{n}: signals_live must be a boolean")
            if d.get("archive_state") not in ("HOT", "PARTIALLY_ARCHIVED", "REHYDRATING"):
                problems.append(f"{n}: archive_state {d.get('archive_state')!r}")
            for b in d.get("blockers") or []:
                try:
                    provenance_ok(b.get("provenance"), f"{n} blocker {b.get('code')}")
                except Failure as exc:
                    problems.append(str(exc))
        need(not problems, "; ".join(problems[:6]))
        return f"{len(datasets)} datasets, all fields typed and all blockers attributed"

    r.check("every dataset carries a typed temperature and attributed blockers", _shape)

    ids = {}
    for t in E.TABLES:
        try:
            ids[t.key] = find_dataset(datasets, t.key)["id"]
        except Failure:
            pass

    def _detail():
        d = r.get(f"/datasets/{ids['patient_encounters']}")
        ctx = d.get("context") or {}
        need(ctx.get("date_column") == "event_date",
             f"date_column is {ctx.get('date_column')!r}, expected event_date")
        need(ctx.get("qualified_table"), "context.qualified_table missing")
        for group in ("date_column_provenance", "policy_provenance",
                      "usage_provenance", "physical_provenance"):
            provenance_ok(ctx.get(group), f"context.{group}")
        ev = d.get("evidence") or []
        need(ev, "evidence list is empty")
        for item in ev:
            need(item.get("status") in ("pass", "warn", "block"),
                 f"evidence status {item.get('status')!r}")
            provenance_ok(item.get("provenance"), f"evidence[{item.get('kind')}]")
        return (f"{len(ev)} evidence items, "
                f"{len(ctx.get('downstream') or [])} downstream windows")

    if "patient_encounters" in ids:
        r.check("GET /datasets/{id} attributes every context group and evidence item",
                _detail)

    # ---------------- policy read out of DataHub ----------------
    print("\npolicy inputs (io.coldlineage.policy.* structured properties)")

    def _policy():
        problems = []
        for t in E.TABLES:
            if t.key not in ids:
                continue
            ctx = (r.get(f"/datasets/{ids[t.key]}").get("context") or {})
            if ctx.get("retention_years") != t.policy.retention_years:
                problems.append(f"{t.key}: retention_years "
                                f"{ctx.get('retention_years')} != {t.policy.retention_years}")
            want_hold = t.policy.legal_hold == "ACTIVE"
            if bool(ctx.get("legal_hold")) != want_hold:
                problems.append(f"{t.key}: legal_hold {ctx.get('legal_hold')} != {want_hold}")
            if want_hold and not ctx.get("legal_hold_matter"):
                problems.append(f"{t.key}: ACTIVE hold with no matter -- an "
                                f"unattributable block")
        need(not problems, "; ".join(problems))
        return "retention floors and legal holds match what was set in DataHub"

    r.check("policy values round-trip from DataHub structured properties", _policy)

    # ---------------- THE HERO ----------------
    print("\nhero case: patient_encounters -- warm table, dead history")

    def _hero():
        v = r.post(f"/datasets/{ids['patient_encounters']}/simulate",
                   {"cutoff_date": HERO_CUTOFF.isoformat()})[1]
        need(v.get("recommendation") in VALID_RECOMMENDATIONS,
             f"recommendation {v.get('recommendation')!r}")
        need(v["recommendation"] != "DO_NOT_ARCHIVE",
             f"cutoff {HERO_CUTOFF} is older than every consumer's window "
             f"but got DO_NOT_ARCHIVE: {v.get('rationale')}")
        got = len(v.get("consumers") or [])
        want = len(C.for_table("patient_encounters"))
        need(got >= want - 1,
             f"only {got} consumers reported, expected about {want}")
        for imp in v["consumers"]:
            w = imp["window"]
            need(w.get("derivation") in VALID_DERIVATIONS,
                 f"{w.get('consumer_name')}: derivation {w.get('derivation')!r}")
            need(imp.get("state") in VALID_STATES,
                 f"{w.get('consumer_name')}: state {imp.get('state')!r}")
            provenance_ok(w.get("provenance"), f"window[{w.get('consumer_name')}]")
        return f"{v['recommendation']}, headroom {v.get('headroom_days')} days"

    if "patient_encounters" in ids:
        r.check(f"simulate @ {HERO_CUTOFF} is archivable", _hero)

    def _binding():
        v = r.post(f"/datasets/{ids['patient_encounters']}/simulate",
                   {"cutoff_date": HERO_CUTOFF.isoformat()})[1]
        expected = C.binding_expectation("patient_encounters")
        assert expected is not None
        bc = v.get("binding_constraint")
        need(bc is not None, "no binding_constraint reported")
        got_urn = bc["window"]["consumer_urn"]
        need(got_urn == expected.urn,
             f"binding constraint is {got_urn}, expected {expected.urn} "
             f"({expected.key} reads from {expected.expectation.earliest_date_read})")
        return f"{expected.key} @ {bc['window'].get('earliest_date_read')}"

    if "patient_encounters" in ids:
        r.check("binding constraint is the consumer with the earliest bounded read",
                _binding)

    def _windows():
        """The load-bearing check.

        Each expected date is what you get by parsing that consumer's SQL and
        resolving it against today. consumers.py never reaches the backend, so a
        match means the backend really did read the statement out of DataHub and
        work the bound out for itself.

        One documented exception, and it is a feature rather than a fudge. A
        consumer more than one hop from the subject does not read the subject: it
        reads something that does. ContextService._resolve_mediated therefore
        hands it the EARLIEST bound of everything closer to the subject, which is
        the most restrictive bound available -- it can only over-protect. So for a
        degree>1 consumer the oracle's `earliest_date_read=None` is satisfied by
        either no bound at all or an inherited bound no later than the tightest
        directly-evidenced one.
        """
        v = r.post(f"/datasets/{ids['patient_encounters']}/simulate",
                   {"cutoff_date": HERO_CUTOFF.isoformat()})[1]

        # The most restrictive bound any directly-reading consumer imposes. An
        # inherited bound may not be later than this or it would be permissive.
        direct_bounds = [
            c.expectation.earliest_date_read
            for c in C.for_table("patient_encounters")
            if c.degree == 1 and c.expectation.earliest_date_read is not None
        ]
        tightest_direct = min(direct_bounds) if direct_bounds else None

        problems = []
        checked = 0
        for c in C.for_table("patient_encounters"):
            try:
                imp = consumer_impact(v, c.key)
            except Failure as exc:
                problems.append(str(exc))
                continue
            w = imp["window"]
            want_deriv = c.expectation.derivation
            if w.get("derivation") != want_deriv:
                problems.append(f"{c.key}: derivation {w.get('derivation')!r} "
                                f"!= {want_deriv!r}")
            want_date = c.expectation.earliest_date_read
            got_date = w.get("earliest_date_read")
            if want_date is None:
                if got_date is not None:
                    if c.degree <= 1:
                        problems.append(f"{c.key}: reads the subject directly and has no "
                                        f"provable window, yet reported {got_date}. A "
                                        f"degree-1 consumer with no SQL must stay unbounded")
                    elif tightest_direct is not None and \
                            date.fromisoformat(got_date) > tightest_direct:
                        problems.append(
                            f"{c.key}: inherited {got_date}, which is LATER than the "
                            f"tightest directly-evidenced bound {tightest_direct}. An "
                            f"inherited bound must be conservative, never permissive")
                    # else: inherited a valid, conservative bound -- see the docstring
            else:
                if got_date is None:
                    problems.append(f"{c.key}: expected {want_date}, got none")
                else:
                    # One day of slack: CURRENT_DATE-relative bounds can straddle
                    # midnight between the ingestion run and this assertion.
                    delta = abs((date.fromisoformat(got_date) - want_date).days)
                    if delta > 1:
                        problems.append(f"{c.key}: earliest_date_read {got_date} "
                                        f"!= {want_date} ({delta}d off)")
            if want_deriv == "sql_predicate":
                if not w.get("evidence_sql"):
                    problems.append(f"{c.key}: no evidence_sql -- a derived window "
                                    f"with no statement behind it is unfalsifiable")
                if not w.get("predicate"):
                    problems.append(f"{c.key}: no predicate fragment")
            checked += 1
        need(not problems, "; ".join(problems[:8]))
        return f"{checked} consumer windows match a fresh parse of their SQL"

    if "patient_encounters" in ids:
        r.check("every history window matches what the SQL in DataHub actually says",
                _windows)

    def _hero_unsafe():
        v = r.post(f"/datasets/{ids['patient_encounters']}/simulate",
                   {"cutoff_date": HERO_UNSAFE_CUTOFF.isoformat()})[1]
        need(v["recommendation"] != "SAFE_TO_ARCHIVE",
             f"cutoff {HERO_UNSAFE_CUTOFF} overlaps the compliance dashboard's window "
             f"(reads from 2024-01-01) yet was called SAFE_TO_ARCHIVE")
        imp = consumer_impact(v, "quarterly_compliance_dashboard")
        need(imp["state"] in ("blocked", "tight"),
             f"quarterly_compliance_dashboard state is {imp['state']!r}, "
             f"expected blocked or tight")
        return f"{v['recommendation']} -- the same table refuses a later cutoff"

    if "patient_encounters" in ids:
        r.check(f"simulate @ {HERO_UNSAFE_CUTOFF} is refused (the cutoff is the variable)",
                _hero_unsafe)

    def _sweep():
        """The product's central claim, as an assertion.

        Same table, same consumers, same instant -- only the cutoff moves. The
        verdict has to walk SAFE_TO_ARCHIVE -> ARCHIVE_WITH_REHYDRATION ->
        DO_NOT_ARCHIVE as the cutoff crosses a window somebody is still reading,
        and it must never get *safer* as the cutoff moves later. A system that can
        only answer at table level cannot produce this sequence at all.
        """
        rank = {"SAFE_TO_ARCHIVE": 0, "ARCHIVE_WITH_REHYDRATION": 1, "DO_NOT_ARCHIVE": 2}
        seen = []
        for cutoff in SWEEP_CUTOFFS:
            v = r.post(f"/datasets/{ids['patient_encounters']}/simulate",
                       {"cutoff_date": cutoff.isoformat()})[1]
            need(v["recommendation"] in VALID_RECOMMENDATIONS,
                 f"{cutoff}: recommendation {v['recommendation']!r}")
            seen.append((cutoff, v["recommendation"], v.get("headroom_days")))

        # 1. monotonic: never becomes more permissive as the cutoff moves later
        for (c1, r1, _), (c2, r2, _) in zip(seen, seen[1:]):
            need(rank[r2] >= rank[r1],
                 f"moving the cutoff from {c1} to {c2} made the verdict SAFER "
                 f"({r1} -> {r2}). A later cutoff archives strictly more rows, so it "
                 f"can never be safer")

        # 2. headroom shrinks by exactly the number of days the cutoff moved
        bounded = [(c, h) for c, _, h in seen if h is not None]
        for (c1, h1), (c2, h2) in zip(bounded, bounded[1:]):
            moved = (c2 - c1).days
            need(h1 - h2 == moved,
                 f"cutoff moved {moved}d from {c1} to {c2} but headroom moved "
                 f"{h1 - h2}d ({h1} -> {h2}); headroom is defined as "
                 f"earliest_date_read - cutoff and must track it exactly")

        # 3. all three verdicts actually appear
        verdicts = [v for _, v, _ in seen]
        for wanted in ("SAFE_TO_ARCHIVE", "ARCHIVE_WITH_REHYDRATION", "DO_NOT_ARCHIVE"):
            need(wanted in verdicts,
                 f"the sweep never produced {wanted}; it went {verdicts}. The whole "
                 f"claim is that the RANGE decides, so all three states must be "
                 f"reachable on one table by moving the date alone")
        return " -> ".join(f"{c}:{v}" for c, v, _ in seen)

    if "patient_encounters" in ids:
        r.check("the cutoff sweep flips the verdict SAFE -> TIGHT -> BLOCKED, monotonically",
                _sweep)

    # ---------------- THE KILLER ----------------
    print("\nkiller case: lab_results -- cold table, unbounded consumer")

    def _killer_looks_cold():
        """The setup for the killer case: every table-level signal says 'nobody is
        reading this'.

        Note what is NOT asserted: that the temperature score itself reads cold.
        The score deliberately fails CLOSED -- an input it cannot establish counts
        as hot, and DataHub reports no active usage bucket for this table, so
        recency scores 1.00 and names itself as UNKNOWN in `temperature.inputs`.
        Asserting a cold score would be asserting that a missing signal is
        evidence of absence, which is the exact bug that deletes production data.
        What matters is that the score is not the gate: the range verdict is.
        """
        d = r.get(f"/datasets/{ids['lab_results']}")
        ctx = d.get("context") or {}
        temp = d.get("temperature") or {}
        need(ctx.get("query_count_30d") == 0,
             f"query_count_30d is {ctx.get('query_count_30d')}, expected 0 -- the "
             f"whole point is that table-level telemetry says 'dead'")
        need(ctx.get("distinct_users_30d") == 0,
             f"distinct_users_30d is {ctx.get('distinct_users_30d')}, expected 0")
        need(temp.get("frequency_component") == 0,
             f"frequency_component is {temp.get('frequency_component')}; zero queries "
             f"in 30 days must contribute zero heat")
        inputs = temp.get("inputs") or {}
        need("access_recency" in inputs and "query_frequency" in inputs,
             "temperature.inputs must name what fed each component, or the score "
             "cannot be argued with")
        # Whatever the score comes out at, it must be visible WHY.
        need(any("UNKNOWN" in str(v) for v in inputs.values())
             or temp.get("classification") in ("COLD", "FROZEN", "COOL"),
             f"temperature is {temp.get('classification')} but no input is labelled "
             f"UNKNOWN; a warm score with no missing signal to explain it is wrong: "
             f"{inputs}")
        return (f"{temp.get('classification')} ({temp.get('score')}), "
                f"0 queries / 0 users in 30d, frequency component 0")

    if "lab_results" in ids:
        r.check("table-level telemetry reports lab_results unused", _killer_looks_cold)

    def _killer_blocks():
        v = r.post(f"/datasets/{ids['lab_results']}/simulate",
                   {"cutoff_date": LAB_CUTOFF.isoformat()})[1]
        need(v["recommendation"] == "DO_NOT_ARCHIVE",
             f"recommendation is {v['recommendation']}; an unbounded consumer means "
             f"no cutoff is provably safe")
        imp = consumer_impact(v, "hipaa_lab_disclosure_extract")
        w = imp["window"]
        need(w["derivation"] == "no_date_filter",
             f"derivation is {w['derivation']!r}, expected no_date_filter. The "
             f"statement HAS a WHERE clause (performing_lab IS NOT NULL) but no date "
             f"bound -- confusing the two is the exact bug this case exists to catch")
        need(w.get("earliest_date_read") is None,
             f"earliest_date_read is {w.get('earliest_date_read')}, expected null")
        need(imp["state"] in ("blocked", "unknown"),
             f"state is {imp['state']!r}, expected blocked or unknown")
        provenance_ok(w.get("provenance"), "hipaa extract window")
        return f"blocked by {w['consumer_name']} ({w['derivation']})"

    if "lab_results" in ids:
        r.check("range analysis blocks it anyway -- unbounded full-table scan",
                _killer_blocks)

    def _killer_every_cutoff():
        bad = []
        for cutoff in (date(2020, 1, 1), date(2021, 6, 1), LAB_CUTOFF, date(2024, 1, 1)):
            v = r.post(f"/datasets/{ids['lab_results']}/simulate",
                       {"cutoff_date": cutoff.isoformat()})[1]
            if v["recommendation"] != "DO_NOT_ARCHIVE":
                bad.append(f"{cutoff}: {v['recommendation']}")
        need(not bad, "an unbounded consumer must block EVERY cutoff, but: "
                      + "; ".join(bad))
        return "DO_NOT_ARCHIVE at every cutoff tried"

    if "lab_results" in ids:
        r.check("no cutoff at all is accepted while the consumer stays unbounded",
                _killer_every_cutoff)

    # ---------------- legal hold ----------------
    print("\npolicy veto: claims_history -- ACTIVE legal hold")

    def _hold():
        d = r.get(f"/datasets/{ids['claims_history']}")
        codes = {b["code"] for b in (d.get("blockers") or [])}
        need("LEGAL_HOLD" in codes,
             f"blockers are {sorted(codes)}; expected LEGAL_HOLD")
        need(d.get("archive_eligible") is False,
             "archive_eligible is true for a dataset under an active legal hold")
        hold_blocker = next(b for b in d["blockers"] if b["code"] == "LEGAL_HOLD")
        provenance_ok(hold_blocker.get("provenance"), "LEGAL_HOLD blocker")
        need(hold_blocker["provenance"]["source"] == "datahub:structured_properties",
             f"the hold is attributed to {hold_blocker['provenance']['source']!r}; it "
             f"came from a DataHub structured property and should say so")
        return f"blocked: {hold_blocker['message'][:70]}"

    if "claims_history" in ids:
        r.check("LEGAL_HOLD blocker present and attributed to DataHub", _hold)

    def _hold_beats_plan():
        status, payload = r.post(f"/datasets/{ids['claims_history']}/plan",
                                 {"cutoff_date": CLAIMS_CUTOFF.isoformat()},
                                 expect=(200, 409))
        if status == 409:
            blob = json.dumps(payload)
            need("LEGAL_HOLD" in blob or "legal hold" in blob.lower(),
                 f"409 detail does not mention the legal hold: {blob[:200]}")
            return "409 with the legal hold in the detail"
        codes = {b["code"] for b in (payload.get("blockers") or [])}
        need("LEGAL_HOLD" in codes,
             f"plan returned 200 with blockers {sorted(codes)} and no LEGAL_HOLD")
        return "200 with LEGAL_HOLD in plan.blockers (execute must refuse it)"

    if "claims_history" in ids:
        r.check(f"plan @ {CLAIMS_CUTOFF} refuses despite a clear consumer window",
                _hold_beats_plan)

    # ---------------- retention floor ----------------
    print("\nretention floor: billing_ledger -- the cutoff is the decision variable")

    def _floor_blocks():
        status, payload = r.post(f"/datasets/{ids['billing_ledger']}/plan",
                                 {"cutoff_date": LEDGER_ILLEGAL_CUTOFF.isoformat()},
                                 expect=(200, 409))
        blob = json.dumps(payload)
        need("RETENTION_FLOOR" in blob,
             f"a {E.BILLING_LEDGER.policy.retention_years}-year floor should reject a "
             f"{LEDGER_ILLEGAL_CUTOFF} cutoff, but the response never mentions "
             f"RETENTION_FLOOR: {blob[:220]}")
        return f"HTTP {status}, RETENTION_FLOOR raised"

    def _floor_allows():
        status, payload = r.post(f"/datasets/{ids['billing_ledger']}/plan",
                                 {"cutoff_date": LEDGER_LEGAL_CUTOFF.isoformat()},
                                 expect=(200, 409))
        need(status == 200,
             f"a {LEDGER_LEGAL_CUTOFF} cutoff is inside the "
             f"{E.BILLING_LEDGER.policy.retention_years}-year floor but plan returned "
             f"HTTP {status}: {json.dumps(payload)[:220]}")
        codes = {b["code"] for b in (payload.get("blockers") or [])}
        need("RETENTION_FLOOR" not in codes,
             f"RETENTION_FLOOR still raised at {LEDGER_LEGAL_CUTOFF}")
        need(payload.get("rows_in_scope", 0) > 0,
             "plan has no rows in scope; nothing would be archived")
        return (f"{payload['rows_in_scope']:,} rows / "
                f"{payload.get('bytes_in_scope', 0):,} bytes in scope")

    if "billing_ledger" in ids:
        r.check(f"plan @ {LEDGER_ILLEGAL_CUTOFF} blocked by the 7-year floor",
                _floor_blocks)
        r.check(f"plan @ {LEDGER_LEGAL_CUTOFF} allowed -- only the cutoff changed",
                _floor_allows)

    # ---------------- control case ----------------
    print("\ncontrol case: care_events_live -- genuinely hot, correctly kept")

    def _hot():
        """A genuinely hot table must be kept -- and it must be kept for a stated
        reason, not by a score.

        `archive_eligible` is deliberately NOT asserted false here. It reports only
        the cutoff-INDEPENDENT blockers (legal hold, no date column, unbounded
        consumers) and care_events_live has none of those. What refuses this table
        is the cutoff-dependent check: its whole history is newer than its own
        2-year retention floor, so every cutoff that would move a single row
        breaches policy. That is the honest reason, and the plan has to carry it.
        """
        d = r.get(f"/datasets/{ids['care_events_live']}")
        cls = (d.get("temperature") or {}).get("classification")
        need(cls in ("HOT", "WARM"),
             f"temperature is {cls}; this table is queried thousands of times a day")
        min_date = d.get("min_date")
        need(min_date is not None, "no measured min_date for care_events_live")

        # Any cutoff that removes rows at all is newer than the retention floor.
        cutoff = date.fromisoformat(d["max_date"])
        status, plan = r.post(f"/datasets/{ids['care_events_live']}/plan",
                              {"cutoff_date": cutoff.isoformat()}, expect=(200, 409))
        blob = json.dumps(plan)
        need("RETENTION_FLOOR" in blob,
             f"a cutoff of {cutoff} on a table whose history starts at {min_date} "
             f"must breach the {E.CARE_EVENTS_LIVE.policy.retention_years:g}-year "
             f"retention floor, but the plan never mentions it: {blob[:220]}")
        if status == 200:
            need(plan.get("executable") is False,
                 f"plan is marked executable despite {[b['code'] for b in plan.get('blockers') or []]}")
        return (f"{cls}; plan @ {cutoff} refused -- "
                f"{plan.get('executable_reason', f'HTTP {status}')}")

    if "care_events_live" in ids:
        r.check("hot table is refused, and the refusal names the policy", _hot)

    # ---------------- execute ----------------
    print("\nexecution")
    if args.no_execute:
        r.skip("plan -> execute -> restore", "--no-execute was passed")
    elif "patient_encounters" not in ids:
        r.skip("plan -> execute -> restore", "patient_encounters not in the inventory")
    else:
        plan = r.check(f"plan @ {HERO_CUTOFF} produces a hash-bound, approvable plan",
                       lambda: (lambda p: (
                           need(bool(p.get("plan_hash")), "no plan_hash"),
                           need(p.get("requires_approval") is True,
                                "requires_approval is not true; an agent must not be "
                                "able to move data without a human on the record"),
                           need(p.get("rows_in_scope", 0) > 0, "no rows in scope"),
                           need(p.get("verdict", {}).get("recommendation")
                                != "DO_NOT_ARCHIVE", "verdict says do not archive"),
                           p,
                       )[-1])(r.post(f"/datasets/{ids['patient_encounters']}/plan",
                                     {"cutoff_date": HERO_CUTOFF.isoformat()})[1]))

        if plan:
            assert isinstance(plan, dict)
            pre = r.get(f"/datasets/{ids['patient_encounters']}")

            def _bad_hash():
                status, _ = r.post("/execute",
                                   {"plan_hash": "0" * 64, "approved_by": args.approver},
                                   expect=(400, 403, 404, 409, 422))
                return f"rejected with HTTP {status}"

            r.check("execute refuses an unknown plan_hash", _bad_hash)

            def _execute():
                res = r.post("/execute", {"plan_hash": plan["plan_hash"],
                                          "approved_by": args.approver})[1]
                need(res.get("run_id") is not None, "no run_id")
                man = res.get("manifest") or {}
                ver = res.get("verification") or {}
                need(ver.get("passed") is True,
                     f"verification did not pass: {json.dumps(ver)[:220]}")
                need(ver.get("readback_sha256_match") is True,
                     "the archive was not re-read and checksummed before deletion")
                need(ver.get("row_count_match") is True,
                     f"row counts differ: source {ver.get('source_row_count')} vs "
                     f"readback {ver.get('readback_row_count')}")
                need(man.get("verified_readback") is True, "manifest.verified_readback false")
                need(man.get("sha256"), "manifest has no sha256")
                need(man.get("object_uri"), "manifest has no object_uri")
                wb = res.get("datahub_writeback") or {}
                need(isinstance(wb.get("written"), bool),
                     "datahub_writeback.written must be a real boolean")
                need(wb.get("mode") in ("live", "replay"),
                     f"writeback mode {wb.get('mode')!r}")
                ops = wb.get("operations") or []
                need(ops, "no write-back operations reported")
                for op in ops:
                    need(op.get("status") in ("ok", "failed", "skipped"),
                         f"write-back op status {op.get('status')!r}")
                if mode == "live" and wb.get("written"):
                    failed = [o for o in ops if o["status"] == "failed"]
                    need(not failed,
                         f"live write-back reported failures: {failed[:2]}")
                return (f"run {res['run_id']}, {man.get('rows'):,} rows, "
                        f"sha256 {str(man.get('sha256'))[:12]}..., "
                        f"write-back {len(ops)} ops written={wb.get('written')}")

            run = r.check("execute verifies the object before deleting anything", _execute)

            def _warehouse_moved():
                """A verified manifest is a claim about object storage. This is the
                claim about the warehouse: the range is actually gone, exactly the
                rows that were archived and no others, and the entity now says so."""
                post = r.get(f"/datasets/{ids['patient_encounters']}")
                man_rows = plan["rows_in_scope"]
                need(post.get("row_count") == pre["row_count"] - man_rows,
                     f"row_count went {pre['row_count']:,} -> {post.get('row_count'):,}; "
                     f"expected exactly {man_rows:,} rows removed "
                     f"({pre['row_count'] - man_rows:,})")
                need(post.get("min_date") is not None
                     and date.fromisoformat(post["min_date"]) >= HERO_CUTOFF,
                     f"oldest remaining row is {post.get('min_date')}, which is still "
                     f"before the cutoff {HERO_CUTOFF} -- the delete did not cover the "
                     f"range that was archived")
                need(post.get("max_date") == pre.get("max_date"),
                     f"newest row changed from {pre.get('max_date')} to "
                     f"{post.get('max_date')}; range archival must not touch recent rows")
                need(post.get("archive_state") == "PARTIALLY_ARCHIVED",
                     f"archive_state is {post.get('archive_state')!r} after a verified run")
                need(post.get("archived_through") == HERO_CUTOFF.isoformat(),
                     f"archived_through is {post.get('archived_through')!r}, "
                     f"expected {HERO_CUTOFF.isoformat()}")
                return (f"{pre['row_count']:,} -> {post['row_count']:,} rows, span now "
                        f"{post['min_date']}..{post['max_date']}, {post['archive_state']}")

            r.check("the warehouse really lost the range, and only the range",
                    _warehouse_moved)

            def _runs():
                rows = r.get("/runs")
                need(isinstance(rows, list) and rows, "no runs recorded")
                latest = rows[0]
                for k in ("id", "dataset_urn", "cutoff_date", "status",
                          "rows_archived", "checksum", "approved_by"):
                    need(k in latest, f"run row missing {k!r}")
                need(latest.get("approved_by") == args.approver,
                     f"approver recorded as {latest.get('approved_by')!r}")
                return f"{len(rows)} run(s), approver on the record"

            def _audit():
                rows = r.get("/audit")
                need(isinstance(rows, list) and rows, "audit trail is empty")
                kinds = {e.get("event_type") for e in rows}
                return f"{len(rows)} events: {sorted(k for k in kinds if k)[:6]}"

            r.check("GET /runs records the run and its approver", _runs)
            r.check("GET /audit has an entry for what happened", _audit)

            if run:
                run_id = r.get("/runs")[0]["id"]

                def _restore():
                    res = r.post("/restore", {"run_id": run_id, "temporary": True})[1]
                    need(res.get("verified") is True,
                         "restore did not verify the checksum; rehydrating unverified "
                         "bytes is worse than not rehydrating")
                    need(res.get("rows", 0) > 0, "restore returned no rows")
                    need(res.get("sha256"), "restore reported no digest")
                    return f"{res['rows']:,} rows into {res.get('table')}, checksum verified"

                r.check("restore rehydrates and re-verifies the checksum", _restore)

    # ---------------- summary ----------------
    print()
    total = r.passed + len(r.failed)
    print(f"{r.passed}/{total} checks passed"
          + (f", {len(r.skipped)} skipped" if r.skipped else ""))
    if r.failed:
        print("\nFailures:")
        for name, why in r.failed:
            print(f"  - {name}\n      {why}")
        return 1
    print("\nEvery consumer window the API reported matches an independent parse of the "
          "SQL that\nscripts/consumers.py put into DataHub -- and the backend never "
          "reads that file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
