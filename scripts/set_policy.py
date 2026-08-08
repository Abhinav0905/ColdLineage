#!/usr/bin/env python3
"""Change a dataset's minimum-age-before-cold policy IN DATAHUB.

    python scripts/set_policy.py patient_encounters --years 14
    python scripts/set_policy.py patient_encounters --months 4
    python scripts/set_policy.py --all --years 2
    python scripts/set_policy.py patient_encounters --show

Why this is a script and not a button in the UI
-----------------------------------------------
Retention policy belongs to a governance owner, not to the tool that benefits
from relaxing it. ColdLineage *reads* `io.coldlineage.policy.retentionYears` from
DataHub at request time and never writes it during normal operation; putting a
knob in the product would imply the product owns its own retention rules, which
is exactly backwards.

So this script stands in for that governance owner. It writes to DataHub, and the
dashboard follows on the next refresh -- which is the point worth showing: the
catalog is the source of truth, and the product obeys it.

What the demo demonstrates
--------------------------
Only ONE of the three bands answers to this value:

    --years 14   nothing archivable; the whole table is inside retention
    --years 4    all three bands visible at once
    --years 2    archivable reaches its maximum
    --months 4   NOTHING FURTHER UNLOCKS

That last step is the argument. Past a point the binding constraint stops being
policy and becomes the SQL that downstream consumers actually run, and no
configuration value can move it. The retention floor is a floor, not a permission
slip.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

GMS = "http://localhost:8090"
PROPERTY = "io.coldlineage.policy.retentionYears"
PROPERTY_URN = f"urn:li:structuredProperty:{PROPERTY}"

DEMO_DATASETS = [
    "patient_encounters",
    "lab_results",
    "claims_history",
    "billing_ledger",
    "care_events_live",
]


def dataset_urn(name: str) -> str:
    if name.startswith("urn:li:dataset:"):
        return name
    return (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,"
        f"coldlineage.public.{name},PROD)"
    )


def graphql(gms: str, query: str, variables: dict, token: str | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {"Content-Type": "application/json", "X-RestLi-Protocol-Version": "2.0.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{gms}/api/graphql", data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach DataHub at {gms}: {exc}")


READ = """
query($urn: String!) {
  dataset(urn: $urn) {
    name
    structuredProperties {
      properties {
        structuredProperty { urn }
        values { ... on NumberValue { numberValue } ... on StringValue { stringValue } }
      }
    }
  }
}
"""

WRITE = """
mutation($input: UpsertStructuredPropertiesInput!) {
  upsertStructuredProperties(input: $input) {
    properties { structuredProperty { urn } }
  }
}
"""


def read_years(gms: str, urn: str, token: str | None) -> float | None:
    body = graphql(gms, READ, {"urn": urn}, token)
    dataset = (body.get("data") or {}).get("dataset")
    if not dataset:
        errors = body.get("errors")
        sys.exit(f"dataset not found in DataHub: {urn}\n{json.dumps(errors)[:300] if errors else ''}")
    for entry in ((dataset.get("structuredProperties") or {}).get("properties") or []):
        if (entry.get("structuredProperty") or {}).get("urn") != PROPERTY_URN:
            continue
        for value in entry.get("values") or []:
            if value.get("numberValue") is not None:
                return float(value["numberValue"])
            if value.get("stringValue") is not None:
                return float(value["stringValue"])
    return None


def write_years(gms: str, urn: str, years: float, token: str | None) -> None:
    # The property is typed NUMBER in properties.yaml, so the value has to go in
    # as numberValue. Sending stringValue is accepted by the mutation and then
    # fails validation server-side, which reads as a silent no-op.
    payload = {
        "assetUrn": urn,
        "structuredPropertyInputParams": [
            {"structuredPropertyUrn": PROPERTY_URN, "values": [{"numberValue": years}]}
        ],
    }
    body = graphql(gms, WRITE, {"input": payload}, token)
    errors = body.get("errors")
    if errors:
        sys.exit(f"write refused by DataHub: {errors[0].get('message', '')[:400]}")


def describe(years: float | None) -> str:
    if years is None:
        return "unset"
    months = round(years * 12)
    if months % 12 == 0:
        return f"{years:g} years"
    return f"{years:g} years ({months} months)"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set io.coldlineage.policy.retentionYears on a dataset, in DataHub."
    )
    parser.add_argument("dataset", nargs="?", help="Dataset name (or full URN).")
    parser.add_argument("--all", action="store_true", help="Apply to every demo dataset.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--years", type=float, help="Minimum age before cold, in years.")
    group.add_argument("--months", type=float, help="Same thing in months (18 -> 1.5 years).")
    parser.add_argument("--show", action="store_true", help="Read the current value and exit.")
    parser.add_argument("--gms", default=GMS)
    parser.add_argument("--wait-attempts", type=int, default=12,
                        help="How many times to poll for the write to become readable.")
    parser.add_argument("--wait-seconds", type=float, default=1.0,
                        help="Seconds between readback polls.")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    if not args.dataset and not args.all:
        parser.error("name a dataset, or pass --all")

    targets = DEMO_DATASETS if args.all else [args.dataset]

    if args.show or (args.years is None and args.months is None):
        print(f"{PROPERTY}  (read from {args.gms})\n")
        for name in targets:
            current = read_years(args.gms, dataset_urn(name), args.token)
            print(f"  {name:22s} {describe(current)}")
        print("\nSet one with --years 2   or   --months 4")
        return 0

    years = args.years if args.years is not None else args.months / 12.0
    if years < 0:
        parser.error("a negative retention floor is not a thing")

    print(f"{PROPERTY} -> {describe(years)}   ({args.gms})\n")
    failed = 0
    for name in targets:
        urn = dataset_urn(name)
        before = read_years(args.gms, urn, args.token)
        write_years(args.gms, urn, years, args.token)

        # Read it back, with patience. A structured-property write is not
        # readable the instant the mutation returns -- it travels through
        # DataHub's change log before a read reflects it. Querying too early
        # returns the OLD value, which during a live demo looks exactly like the
        # feature not working. So poll until the new value is actually visible,
        # and only then tell the operator to refresh.
        after = None
        for attempt in range(args.wait_attempts):
            after = read_years(args.gms, urn, args.token)
            if after is not None and abs(after - years) < 1e-6:
                break
            time.sleep(args.wait_seconds)
        ok = after is not None and abs(after - years) < 1e-6
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {name:22s} {describe(before)}  ->  {describe(after)}")
        if not ok:
            failed += 1

    if failed:
        print(f"\n{failed} dataset(s) did not take the new value. Nothing was assumed.")
        return 1

    print(
        "\nDataHub is the source of truth, so nothing needs restarting -- ColdLineage reads\n"
        "this at request time. Reload the estate page (or hit Refresh) to see the bands move.\n"
        "The 'in use' band will not move: that one is fixed by consumer SQL, not by policy."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
