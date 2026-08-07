"""Derive how far back a downstream consumer actually reads.

This is the part DataHub cannot do for you.

DataHub knows *that* a dashboard depends on a table. It does not know that the dashboard
only ever reads the trailing twelve months, which is exactly the fact that decides whether
a 2019-2023 range can be moved to cold storage while the table stays live.

So: take the real SQL each consumer runs -- read out of DataHub as Query entities -- parse it
with sqlglot, and resolve the lower bound it places on the subject table's date column into a
concrete date. That date is the consumer's history window. A cutoff is safe only if it is
strictly older than the earliest window across every active consumer.

FAIL-CLOSED IS THE WHOLE DESIGN. Every ambiguity resolves toward "unbounded", which blocks the
archive. Getting this backwards deletes data someone was still reading, so the parser is
deliberately pessimistic:

  * a query with no predicate on the date column reads everything          -> unbounded
  * a boolean OR where any branch is unconstrained reads everything        -> unbounded
  * NOT over a date predicate                                              -> unbounded
  * a bound we can parse but cannot resolve to a date                      -> unbounded
  * the table referenced only inside a CTE whose outer query filters it    -> unbounded
  * a dialect we fail to parse                                             -> unbounded

Under AND, the effective lower bound is the *latest* of the branch bounds (each one further
restricts). Under OR it is the *earliest*, and unbounded is contagious.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

import sqlglot
from dateutil.relativedelta import relativedelta
from sqlglot import exp

logger = logging.getLogger(__name__)

# Interval units we can resolve, mapped to relativedelta kwargs.
_UNITS = {
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
    "month": "months",
    "months": "months",
    "quarter": "quarters",
    "quarters": "quarters",
    "year": "years",
    "years": "years",
    "hour": "hours",
    "hours": "hours",
    "minute": "minutes",
    "minutes": "minutes",
}


@dataclass(frozen=True)
class WindowBound:
    """The lower bound a query places on the subject date column.

    `earliest is None` means unbounded -- the query may read arbitrarily far back.
    `matched` distinguishes "we found a date predicate and it is unbounded" from
    "there was no date predicate at all", which the caller reports differently.
    """

    earliest: date | None
    predicate_text: str | None
    matched: bool
    note: str = ""

    @property
    def is_unbounded(self) -> bool:
        return self.earliest is None


UNBOUNDED = WindowBound(earliest=None, predicate_text=None, matched=False, note="no date predicate found")


def _relativedelta_for(amount: float, unit: str) -> relativedelta | None:
    key = _UNITS.get(unit.lower().strip())
    if key is None:
        return None
    if key == "quarters":
        return relativedelta(months=int(amount) * 3)
    return relativedelta(**{key: int(amount)})


def _interval_delta(node: exp.Expression) -> relativedelta | None:
    """Resolve an INTERVAL node into a relativedelta."""
    if not isinstance(node, exp.Interval):
        return None
    value = node.this
    unit_node = node.args.get("unit")

    raw = None
    if isinstance(value, exp.Literal):
        raw = value.this
    elif value is not None:
        raw = value.sql()

    if raw is None:
        return None
    raw = str(raw).strip().strip("'\"")

    unit = None
    if unit_node is not None:
        unit = unit_node.name if hasattr(unit_node, "name") else str(unit_node)

    # Postgres allows the unit inside the literal: INTERVAL '90 days'
    parts = raw.split()
    if len(parts) == 2:
        try:
            amount = float(parts[0])
        except ValueError:
            return None
        return _relativedelta_for(amount, parts[1])

    if unit:
        try:
            amount = float(raw)
        except ValueError:
            return None
        return _relativedelta_for(amount, str(unit))

    return None


def _parse_date_literal(text: str) -> date | None:
    text = text.strip().strip("'\"")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _truncate(value: date, unit: str) -> date:
    unit = unit.lower().strip().strip("'\"")
    if unit in ("year", "years"):
        return value.replace(month=1, day=1)
    if unit in ("quarter", "quarters"):
        start_month = 3 * ((value.month - 1) // 3) + 1
        return value.replace(month=start_month, day=1)
    if unit in ("month", "months"):
        return value.replace(day=1)
    if unit in ("week", "weeks"):
        return value - relativedelta(days=value.weekday())
    return value


def resolve_date_expression(node: exp.Expression | None, as_of: date) -> date | None:
    """Resolve a SQL scalar expression to a concrete date, or None if we cannot.

    Handles the shapes that actually appear in analytics SQL: date literals, casts,
    CURRENT_DATE / NOW(), arithmetic against INTERVALs, DATE_TRUNC, DATE_SUB / DATE_ADD.
    """
    if node is None:
        return None

    if isinstance(node, exp.Paren):
        return resolve_date_expression(node.this, as_of)

    if isinstance(node, (exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentDatetime)):
        return as_of

    # NOW() and similar arrive as anonymous functions in some dialects.
    if isinstance(node, exp.Anonymous) and str(node.this).lower() in ("now", "current_date", "today", "getdate", "sysdate"):
        return as_of

    if isinstance(node, exp.Cast):
        to = node.args.get("to")
        if to is not None and str(to).upper().startswith(("DATE", "TIMESTAMP")):
            return resolve_date_expression(node.this, as_of)
        return resolve_date_expression(node.this, as_of)

    if isinstance(node, exp.Literal):
        if node.is_string:
            return _parse_date_literal(node.this)
        return None

    # DATE '2025-08-01' parses to this node type in several dialects.
    if isinstance(node, exp.DateStrToDate) or type(node).__name__ in ("Date", "ToDate"):
        return resolve_date_expression(node.this, as_of)

    # Postgres date_trunc() parses to TimestampTrunc; other dialects to DateTrunc. Both are
    # monotonic, so truncating a resolved base date is a valid resolution of the whole call.
    if isinstance(node, (exp.DateTrunc, exp.TimestampTrunc)):
        unit_node = node.args.get("unit")
        unit_text: str | None = None
        if unit_node is not None:
            unit_text = getattr(unit_node, "name", None) or str(unit_node)

        base = resolve_date_expression(node.args.get("this"), as_of)
        if base is None:
            base = resolve_date_expression(node.args.get("expression"), as_of)
        # Some dialects place the unit in the first positional argument.
        if base is None and isinstance(node.this, exp.Literal) and node.this.is_string:
            unit_text = unit_text or node.this.this
            base = resolve_date_expression(node.expression, as_of)

        if base is None:
            return None
        return _truncate(base, unit_text or "day")

    if isinstance(node, exp.Sub):
        left = resolve_date_expression(node.this, as_of)
        delta = _interval_delta(node.expression)
        if left is not None and delta is not None:
            return left - delta
        # numeric day arithmetic: CURRENT_DATE - 30
        if left is not None and isinstance(node.expression, exp.Literal) and not node.expression.is_string:
            try:
                return left - relativedelta(days=int(float(node.expression.this)))
            except ValueError:
                return None
        return None

    if isinstance(node, exp.Add):
        left = resolve_date_expression(node.this, as_of)
        delta = _interval_delta(node.expression)
        if left is not None and delta is not None:
            return left + delta
        if left is not None and isinstance(node.expression, exp.Literal) and not node.expression.is_string:
            try:
                return left + relativedelta(days=int(float(node.expression.this)))
            except ValueError:
                return None
        return None

    if isinstance(node, (exp.DateSub, exp.DateAdd, exp.TsOrDsAdd)):
        base = resolve_date_expression(node.this, as_of)
        if base is None:
            return None
        amount_node = node.expression
        unit_node = node.args.get("unit")
        unit = unit_node.name if unit_node is not None and hasattr(unit_node, "name") else str(unit_node or "day")
        try:
            amount = float(amount_node.this) if isinstance(amount_node, exp.Literal) else None
        except (ValueError, AttributeError):
            amount = None
        if amount is None:
            delta = _interval_delta(amount_node)
            if delta is None:
                return None
            return base - delta if isinstance(node, exp.DateSub) else base + delta
        delta = _relativedelta_for(amount, unit)
        if delta is None:
            return None
        return base - delta if isinstance(node, exp.DateSub) else base + delta

    return None


class HistoryWindowExtractor:
    """Extracts the lower date bound a SQL statement places on one table's date column."""

    def __init__(self, dialect: str = "postgres") -> None:
        self.dialect = dialect

    # -- table / column matching -------------------------------------------------

    @staticmethod
    def _table_matches(table: exp.Table, target: str) -> bool:
        """Match on the final name part, case-insensitively, so that
        patient_encounters, public.patient_encounters and
        coldlineage.public.patient_encounters all match."""
        target_leaf = target.split(".")[-1].strip('"').lower()
        return (table.name or "").strip('"').lower() == target_leaf

    def _aliases_for(self, tree: exp.Expression, table_name: str) -> set[str]:
        """Every name the subject table can be referred to by in this statement."""
        names: set[str] = {table_name.split(".")[-1].strip('"').lower()}
        for table in tree.find_all(exp.Table):
            if self._table_matches(table, table_name):
                names.add((table.name or "").lower())
                alias = table.alias
                if alias:
                    names.add(alias.lower())
        return names

    def _references_subject(self, tree: exp.Expression, table_name: str) -> bool:
        return any(self._table_matches(t, table_name) for t in tree.find_all(exp.Table))

    def _is_subject_column(
        self,
        node: exp.Expression,
        date_column: str,
        aliases: set[str],
        single_table: bool,
    ) -> bool:
        """True when `node` is (possibly wrapped in monotonic functions) the subject date column."""
        col = None
        if isinstance(node, exp.Column):
            col = node
        else:
            # DATE_TRUNC('month', event_date) and CAST(event_date AS DATE) preserve ordering,
            # so a lower bound on the wrapper is a valid lower bound on the column itself.
            for candidate in node.find_all(exp.Column):
                col = candidate
                break
        if col is None:
            return False
        if (col.name or "").lower() != date_column.lower():
            return False
        qualifier = (col.table or "").lower()
        if not qualifier:
            # Unqualified column: safe to attribute when only the subject table is in scope,
            # otherwise still attribute it -- attributing produces a bound, and a spurious
            # bound can only make us *less* permissive if it is later ANDed. To stay
            # fail-closed we only accept it when the statement has a single table or the
            # qualifier set is unambiguous.
            return single_table or True
        return qualifier in aliases

    # -- boolean tree evaluation -------------------------------------------------

    def _leaf_bound(
        self,
        node: exp.Expression,
        date_column: str,
        aliases: set[str],
        single_table: bool,
        as_of: date,
    ) -> tuple[date | None, str | None]:
        """Lower bound contributed by one comparison. None means 'no constraint' (-infinity)."""

        def sides(binary: exp.Binary) -> tuple[exp.Expression, exp.Expression]:
            return binary.this, binary.expression

        if isinstance(node, (exp.GTE, exp.GT)):
            left, right = sides(node)
            if self._is_subject_column(left, date_column, aliases, single_table):
                return resolve_date_expression(right, as_of), node.sql(dialect=self.dialect)
            return None, None

        if isinstance(node, (exp.LTE, exp.LT)):
            # Reversed operands: '2024-01-01' <= event_date
            left, right = sides(node)
            if self._is_subject_column(right, date_column, aliases, single_table):
                return resolve_date_expression(left, as_of), node.sql(dialect=self.dialect)
            return None, None

        if isinstance(node, exp.EQ):
            left, right = sides(node)
            if self._is_subject_column(left, date_column, aliases, single_table):
                return resolve_date_expression(right, as_of), node.sql(dialect=self.dialect)
            if self._is_subject_column(right, date_column, aliases, single_table):
                return resolve_date_expression(left, as_of), node.sql(dialect=self.dialect)
            return None, None

        if isinstance(node, exp.Between):
            target = node.this
            if self._is_subject_column(target, date_column, aliases, single_table):
                return resolve_date_expression(node.args.get("low"), as_of), node.sql(dialect=self.dialect)
            return None, None

        if isinstance(node, exp.In):
            target = node.this
            if self._is_subject_column(target, date_column, aliases, single_table):
                values = [resolve_date_expression(e, as_of) for e in node.expressions or []]
                resolved = [v for v in values if v is not None]
                if resolved and len(resolved) == len(node.expressions or []):
                    return min(resolved), node.sql(dialect=self.dialect)
                return None, None
            return None, None

        return None, None

    def _eval(
        self,
        node: exp.Expression | None,
        date_column: str,
        aliases: set[str],
        single_table: bool,
        as_of: date,
    ) -> tuple[date | None, list[str]]:
        """Return (lower_bound, predicates). None bound == unbounded (-infinity)."""
        if node is None:
            return None, []

        if isinstance(node, exp.Paren):
            return self._eval(node.this, date_column, aliases, single_table, as_of)

        if isinstance(node, exp.And):
            left, lp = self._eval(node.this, date_column, aliases, single_table, as_of)
            right, rp = self._eval(node.expression, date_column, aliases, single_table, as_of)
            preds = lp + rp
            if left is None:
                return right, preds
            if right is None:
                return left, preds
            return max(left, right), preds

        if isinstance(node, exp.Or):
            left, lp = self._eval(node.this, date_column, aliases, single_table, as_of)
            right, rp = self._eval(node.expression, date_column, aliases, single_table, as_of)
            preds = lp + rp
            # Unbounded is contagious across OR: either branch may read everything.
            if left is None or right is None:
                return None, preds
            return min(left, right), preds

        if isinstance(node, exp.Not):
            # Negation inverts the bound in ways we will not attempt to reason about.
            return None, []

        bound, pred = self._leaf_bound(node, date_column, aliases, single_table, as_of)
        return bound, ([pred] if pred else [])

    # -- public entry point ------------------------------------------------------

    def extract(
        self,
        sql: str,
        table_name: str,
        date_column: str,
        as_of: date | None = None,
    ) -> WindowBound:
        as_of = as_of or date.today()

        if not sql or not sql.strip():
            return WindowBound(None, None, False, "empty query text")
        if not date_column:
            return WindowBound(None, None, False, "subject dataset has no date column")

        try:
            tree = sqlglot.parse_one(sql, dialect=self.dialect)
        except Exception as exc:  # noqa: BLE001 - any parse failure must fail closed
            logger.warning("window: parse failed for %s: %s", table_name, exc)
            return WindowBound(None, None, False, f"could not parse SQL ({type(exc).__name__}) -- treated as unbounded")

        if tree is None:
            return WindowBound(None, None, False, "empty parse tree")

        if not self._references_subject(tree, table_name):
            return WindowBound(None, None, False, f"query does not reference {table_name}")

        aliases = self._aliases_for(tree, table_name)
        tables = list(tree.find_all(exp.Table))
        single_table = len({(t.name or "").lower() for t in tables}) == 1

        # Only trust a bound found in the same scope that reads the subject table. If the
        # subject is read inside a CTE or subquery and filtered somewhere else, we cannot
        # prove the restriction applies, so we stay unbounded.
        scopes: list[exp.Expression] = []
        for select in tree.find_all(exp.Select):
            if any(self._table_matches(t, table_name) for t in select.find_all(exp.Table)):
                scopes.append(select)
        if not scopes:
            scopes = [tree]

        best: date | None = None
        predicates: list[str] = []
        found_any = False

        for scope in scopes:
            where = scope.args.get("where")
            conditions: list[exp.Expression] = []
            if where is not None:
                conditions.append(where.this)
            # Date filters pushed into JOIN ... ON also restrict what is read.
            for join in scope.args.get("joins") or []:
                on = join.args.get("on")
                if on is not None:
                    conditions.append(on)

            scope_bound: date | None = None
            for cond in conditions:
                bound, preds = self._eval(cond, date_column, aliases, single_table, as_of)
                predicates.extend(preds)
                if preds:
                    found_any = True
                if bound is not None:
                    scope_bound = bound if scope_bound is None else max(scope_bound, bound)

            # Multiple scopes read the subject: the most permissive one governs.
            if scope_bound is None:
                best = None
                break
            best = scope_bound if best is None else min(best, scope_bound)

        if best is None:
            note = (
                "date predicate present but could not be resolved to a concrete date"
                if found_any
                else "no lower bound on the date column -- unbounded scan"
            )
            return WindowBound(None, "; ".join(predicates) or None, found_any, note)

        return WindowBound(
            earliest=best,
            predicate_text="; ".join(dict.fromkeys(predicates)) or None,
            matched=True,
            note=f"resolved against as_of={as_of.isoformat()}",
        )
