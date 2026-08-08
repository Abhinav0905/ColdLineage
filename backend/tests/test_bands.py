#!/usr/bin/env python3
"""Tests for the three-band row split.

The invariant that matters: **whatever blocks a cutoff must also keep rows out of
the archivable band.** If the chart is greener than the simulator is willing to
act on, the picture is wrong in the unsafe direction, and a viewer trusts it.

    docker cp backend/tests/test_bands.py <api>:/app/ && \
    docker exec <api> python test_bands.py
"""

from __future__ import annotations

import sys
from datetime import date

from app.domain.models import (
    ConsumerWindow,
    DatasetContext,
    Provenance,
    Source,
    WindowDerivation,
)
from app.services.bands import compute, evidence_bound
from app.services.simulation import _state_for

PROV = Provenance(source=Source.DATAHUB_QUERIES, detail="test", observed_at=None)
TODAY = date(2026, 8, 8)


def _window(name: str, earliest: date | None, derivation: WindowDerivation) -> ConsumerWindow:
    return ConsumerWindow(
        consumer_urn=f"urn:li:dataset:({name})",
        consumer_name=name,
        consumer_type="DATASET",
        degree=1,
        earliest_date_read=earliest,
        derivation=derivation,
        provenance=PROV,
    )


def _context(**kwargs) -> DatasetContext:
    base = dict(
        urn="urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.t,PROD)",
        name="t",
        platform="postgres",
        qualified_table='"public"."t"',
        date_column="event_date",
        date_column_provenance=PROV,
        policy_provenance=PROV,
        usage_provenance=PROV,
        physical_provenance=PROV,
        row_count=1000,
        downstream=[],
        retention_years=2.0,
        legal_hold=False,
    )
    base.update(kwargs)
    return DatasetContext(**base)


# --- the invariant: the chart may never be greener than the simulator ------


def test_every_derivation_the_simulator_blocks_also_blocks_the_bands():
    """`is_unbounded` is True only for NO_DATE_FILTER and NO_QUERIES_OBSERVED,
    but the simulator treats EVERY unresolved window as blocking. Banding on
    `is_unbounded` would paint rows archivable that /simulate refuses."""
    unresolved = [
        WindowDerivation.NO_DATE_FILTER,
        WindowDerivation.NO_QUERIES_OBSERVED,
        WindowDerivation.NOT_A_QUERY_CONSUMER,
    ]
    for derivation in unresolved:
        window = _window("mystery", None, derivation)
        state, _, _ = _state_for(window, date(2020, 1, 1))
        assert state.value == "unknown", f"{derivation} should block in the simulator"

        bound, blocked = evidence_bound(_context(downstream=[window]))
        assert bound is None, f"{derivation} must leave no provable bound"
        assert blocked, f"{derivation} must produce a blocking reason"


def test_a_single_unresolved_consumer_poisons_an_otherwise_bounded_table():
    """One consumer whose reach cannot be proven blocks the whole table, even
    when every other consumer is neatly bounded."""
    windows = [
        _window("tidy", date(2024, 1, 1), WindowDerivation.SQL_PREDICATE),
        _window("mystery", None, WindowDerivation.NOT_A_QUERY_CONSUMER),
    ]
    bound, blocked = evidence_bound(_context(downstream=windows))
    assert bound is None and blocked


def test_a_consumer_with_an_inherited_bound_is_not_treated_as_unproven():
    """NOT_A_QUERY_CONSUMER *with* a bound inherited through lineage is proven,
    and must not be swept up by the fail-closed rule."""
    window = _window("kpi dashboard", date(2024, 1, 1), WindowDerivation.NOT_A_QUERY_CONSUMER)
    bound, blocked = evidence_bound(_context(downstream=[window]))
    assert bound == date(2024, 1, 1)
    assert blocked is None


def test_no_consumers_at_all_leaves_policy_in_charge():
    bound, blocked = evidence_bound(_context(downstream=[]))
    assert bound is None and blocked is None, "absence of consumers is not a blocker"


def test_earliest_bound_wins_across_consumers():
    windows = [
        _window("a", date(2025, 1, 1), WindowDerivation.SQL_PREDICATE),
        _window("b", date(2023, 6, 1), WindowDerivation.SQL_PREDICATE),
        _window("c", date(2024, 1, 1), WindowDerivation.SQL_PREDICATE),
    ]
    bound, _ = evidence_bound(_context(downstream=windows))
    assert bound == date(2023, 6, 1)


# --- labelling honesty ----------------------------------------------------


def test_legal_hold_does_not_claim_the_rows_are_being_read():
    """The blue band's legend says a consumer still reads it. Rows frozen by a
    court order are not being read, and saying so would be a false statement on
    a chart people are asked to trust."""
    ctx = _context(
        legal_hold=True,
        legal_hold_matter="MDL-2291",
        downstream=[_window("tidy", date(2024, 1, 1), WindowDerivation.SQL_PREDICATE)],
    )
    bands = compute(_FakeDB({"< 2024-01-01": 600, ">= 2024-01-01": 400}), ctx, now=TODAY)
    assert bands.archivable == 0, "a legal hold must stop the archive dead"
    assert bands.policy_held == 600, "unread-but-frozen rows belong in the held band"
    assert bands.in_use == 400, "only rows a consumer reaches are 'in use'"
    assert bands.binding == "legal_hold"
    assert "frozen" in bands.reason and "MDL-2291" in bands.reason


def test_unproven_consumer_puts_everything_in_use():
    ctx = _context(downstream=[_window("hipaa", None, WindowDerivation.NO_DATE_FILTER)])
    bands = compute(_FakeDB({}), ctx, now=TODAY)
    assert (bands.archivable, bands.policy_held, bands.in_use) == (0, 0, 1000)
    assert bands.binding == "unbounded"


def test_missing_date_column_puts_everything_in_use():
    bands = compute(_FakeDB({}), _context(date_column=None), now=TODAY)
    assert bands.in_use == 1000 and bands.binding == "unmeasured"


# --- arithmetic -----------------------------------------------------------


def test_bands_always_sum_to_the_measured_total():
    """Derived by subtraction rather than three FILTER clauses, precisely so this
    cannot drift at a boundary."""
    ctx = _context(downstream=[_window("a", date(2024, 1, 1), WindowDerivation.SQL_PREDICATE)])
    bands = compute(_FakeDB({"< 2024-01-01": 666, ">= 2024-01-01": 334}), ctx, now=TODAY)
    assert bands.total == 1000
    assert bands.archivable + bands.policy_held + bands.in_use == 1000


def test_evidence_binds_when_it_is_earlier_than_the_floor():
    """retention 2y -> floor 2024-08-08; consumers read from 2024-01-01, which is
    earlier, so evidence is what caps the cutoff."""
    ctx = _context(
        retention_years=2.0,
        downstream=[_window("a", date(2024, 1, 1), WindowDerivation.SQL_PREDICATE)],
    )
    bands = compute(_FakeDB({"< 2024-01-01": 666, ">= 2024-01-01": 334}), ctx, now=TODAY)
    assert bands.cutoff == date(2024, 1, 1)
    assert bands.binding == "evidence"
    assert bands.policy_held == 0, "when evidence binds, nothing is merely policy-held"


def test_policy_binds_when_the_floor_is_earlier_than_the_evidence():
    """retention 14y -> floor 2012-08-08, far earlier than any read, so policy
    holds the middle band and nothing is archivable on this span."""
    ctx = _context(
        retention_years=14.0,
        downstream=[_window("a", date(2024, 1, 1), WindowDerivation.SQL_PREDICATE)],
    )
    bands = compute(_FakeDB({"< 2012-08-08": 0, ">= 2024-01-01": 334}), ctx, now=TODAY)
    assert bands.binding == "policy"
    assert bands.archivable == 0
    assert bands.policy_held == 666
    assert bands.in_use == 334


def test_in_use_is_identical_whatever_the_retention_setting():
    """The demo's whole claim, as an assertion: turning the knob never changes
    how many rows a consumer can reach."""
    seen = set()
    for years in (14.0, 7.0, 4.0, 2.0, 1.0, 0.3333):
        ctx = _context(
            retention_years=years,
            downstream=[_window("a", date(2024, 1, 1), WindowDerivation.SQL_PREDICATE)],
        )
        bands = compute(_FakeDB({">= 2024-01-01": 334}), ctx, now=TODAY)
        seen.add(bands.in_use)
    assert seen == {334}, f"in_use moved with the retention knob: {seen}"


def test_fractional_years_are_honoured_in_the_floor():
    """int() truncation made four months mean no floor at all."""
    ctx = _context(retention_years=0.3333, downstream=[])
    bands = compute(_FakeDB({}), ctx, now=TODAY)
    assert bands.policy_floor == date(2026, 4, 8), bands.policy_floor

    ctx18 = _context(retention_years=1.5, downstream=[])
    assert compute(_FakeDB({}), ctx18, now=TODAY).policy_floor == date(2025, 2, 8)


# --- a fake session that answers only the two edge counts -----------------


class _FakeDB:
    """Answers `count(*) WHERE col < :d` / `>= :d` from a dict keyed "<op> <date>"."""

    def __init__(self, counts: dict[str, int]):
        self.counts = counts

    def execute(self, statement, params=None):
        sql = str(statement)
        op = "<" if "< :d" in sql else ">="
        key = f"{op} {params['d'].isoformat()}"
        value = self.counts.get(key, 0)
        return _Scalar(value)

    def rollback(self):
        pass


class _Scalar:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {name}\n      {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}\n      {type(exc).__name__}: {exc}")
        else:
            print(f"ok    {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
