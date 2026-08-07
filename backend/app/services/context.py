"""Assemble a DatasetContext from DataHub plus the warehouse.

Division of labour, and it is the point of the project:

  DataHub answers *should* this move            -- lineage, usage, policy, classification
  the warehouse answers *what* would move       -- row counts, physical bytes, date span

Nothing here invents a value. Every read is independent and individually recoverable, so
a DataHub version that lacks one aspect costs exactly that one signal; the field comes
back as None with `Source.UNAVAILABLE` attached and the UI shows a gap rather than a
plausible-looking number. A missing input has to be visible, because the whole claim of
the product is that the decision is defensible.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.datahub.client import DataHubClient, DataHubError
from app.domain.models import (
    ConsumerWindow,
    DatasetContext,
    Provenance,
    Source,
    WindowDerivation,
)
from app.services.window import HistoryWindowExtractor

logger = logging.getLogger(__name__)

POLICY_NS = "io.coldlineage.policy"
ARCHIVE_NS = "io.coldlineage.archive"

# Consumer entity types that never issue SQL against the subject themselves.
_NON_QUERY_TYPES = {"DATA_FLOW"}

_URN_TABLE = re.compile(r"urn:li:dataset:\(urn:li:dataPlatform:[^,]+,([^,]+),[^)]+\)")


def _now() -> datetime:
    return datetime.now(UTC)


def table_from_urn(urn: str) -> tuple[str | None, str]:
    """`...,coldlineage.public.patient_encounters,PROD)` -> ("public", "patient_encounters")."""
    match = _URN_TABLE.search(urn or "")
    if not match:
        return None, ""
    parts = match.group(1).split(".")
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, parts[0]


def _unavailable(detail: str) -> Provenance:
    return Provenance(source=Source.UNAVAILABLE, detail=detail, observed_at=_now())


def _from(source: Source, detail: str = "") -> Provenance:
    return Provenance(source=source, detail=detail, observed_at=_now())


class ContextService:
    def __init__(self, client: DataHubClient) -> None:
        self.client = client
        self.extractor = HistoryWindowExtractor(dialect="postgres")

    def _src(self, source: Source, detail: str = "") -> Provenance:
        """Attribute a signal to where it *actually* came from on this request.

        In replay mode the bytes came off disk, not off a GMS, so the signal is tagged
        `cassette:recorded` and the detail names the aspect it was originally recorded
        from. Reporting `datahub:lineage` while serving a cassette would be precisely the
        kind of unearned claim this project refuses to make everywhere else.
        """
        if self.client.mode == "replay":
            origin = f"replayed from a recorded {source.value} response"
            return _from(Source.CASSETTE, f"{origin}{'; ' + detail if detail else ''}")
        return _from(source, detail)

    # -- DataHub side ------------------------------------------------------

    async def _gather(self, urn: str) -> dict:
        """Fire every DataHub read concurrently; each one fails on its own."""
        tasks = {
            "entity": self.client.get_dataset(urn),
            "properties": self.client.get_structured_properties(urn),
            "usage": self.client.get_usage(urn),
            "downstream": self.client.get_downstream(urn),
            "queries": self.client.get_queries(urn),
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        out: dict = {}
        for key, value in zip(tasks.keys(), results, strict=True):
            if isinstance(value, BaseException):
                logger.warning("datahub read %s failed for %s: %s", key, urn, value)
                out[key] = None
                out[f"{key}_error"] = str(value)[:300]
            else:
                out[key] = value
        return out

    def _resolve_date_column(self, entity: dict | None) -> tuple[str | None, Provenance]:
        """Which column carries the history we would be slicing.

        The estate deliberately uses a different name per table (event_date,
        service_date, collected_date, posted_date) so that nothing can quietly hardcode
        one. It is read from the DataHub schema, not guessed.
        """
        if not entity:
            return None, _unavailable("dataset entity not readable from DataHub")

        props = (entity.get("properties") or {}).get("customProperties") or []
        declared = {p["key"]: p["value"] for p in props if p.get("key")}
        for key in ("coldlineage.dateColumn", "coldlineage.date_column"):
            if declared.get(key):
                return declared[key], self._src(Source.DATAHUB_PROPERTIES, f"declared as {key}")

        schema = entity.get("schemaMetadata") or {}
        fields = schema.get("fields") or []
        date_fields = [f for f in fields if str(f.get("nativeDataType", "")).lower().startswith("date")]
        if date_fields:
            name = date_fields[0]["fieldPath"]
            return name, self._src(Source.DATAHUB_SCHEMA, f"first DATE column in schemaMetadata ({name})")
        ts_fields = [
            f for f in fields if "timestamp" in str(f.get("nativeDataType", "")).lower()
        ]
        if ts_fields:
            name = ts_fields[0]["fieldPath"]
            return name, self._src(Source.DATAHUB_SCHEMA, f"first TIMESTAMP column in schemaMetadata ({name})")
        return None, _unavailable("no DATE or TIMESTAMP column found in DataHub schemaMetadata")

    def _windows(
        self,
        downstream: list[dict] | None,
        queries: list[dict] | None,
        date_column: str | None,
        table_name: str,
        as_of: date,
    ) -> list[ConsumerWindow]:
        """One window per downstream consumer.

        A consumer with no parseable, date-bounded SQL gets a window of None, which the
        simulator treats as blocking. Consumers are never silently dropped -- a dropped
        consumer is an approved archive that eats someone's data.
        """
        if downstream is None:
            return []

        by_consumer: dict[str, list[dict]] = {}
        for query in queries or []:
            # A statement is attributable to the consumer that issues it and, where the
            # lineage edge is carried by another entity (an ML model reached through its
            # training job), to that carrier as well. Without this the job scores as a
            # consumer with no observable window and blocks every cutoff.
            for key in ("consumer_urn", "carrier_urn"):
                urn = query.get(key)
                if urn:
                    by_consumer.setdefault(urn, []).append(query)

        windows: list[ConsumerWindow] = []
        for node in downstream:
            urn = node.get("urn") or ""
            ctype = str(node.get("type") or "UNKNOWN")
            matched = by_consumer.get(urn, [])

            if not matched:
                derivation = (
                    WindowDerivation.NOT_A_QUERY_CONSUMER
                    if ctype in _NON_QUERY_TYPES
                    else WindowDerivation.NO_QUERIES_OBSERVED
                )
                windows.append(
                    ConsumerWindow(
                        consumer_urn=urn,
                        consumer_name=node.get("name") or urn,
                        consumer_type=ctype,
                        platform=node.get("platform"),
                        degree=int(node.get("degree") or 1),
                        earliest_date_read=None,
                        derivation=derivation,
                        provenance=self._src(
                            Source.DATAHUB_LINEAGE,
                            "lineage edge present, no query text recorded in DataHub",
                        ),
                    )
                )
                continue

            # A consumer may run several queries. The one reaching furthest back governs.
            best: date | None = None
            best_bound = None
            unbounded_reason: str | None = None
            chosen = matched[0]

            for query in matched:
                bound = self.extractor.extract(
                    query.get("sql") or "", table_name, date_column or "", as_of=as_of
                )
                if bound.earliest is None:
                    unbounded_reason = bound.note
                    chosen = query
                    best = None
                    best_bound = bound
                    break
                if best is None or bound.earliest < best:
                    best = bound.earliest
                    best_bound = bound
                    chosen = query

            if best is None:
                derivation = (
                    WindowDerivation.NO_DATE_FILTER
                    if (best_bound and not best_bound.matched)
                    else WindowDerivation.NO_DATE_FILTER
                )
                windows.append(
                    ConsumerWindow(
                        consumer_urn=urn,
                        consumer_name=node.get("name") or urn,
                        consumer_type=ctype,
                        platform=node.get("platform"),
                        degree=int(node.get("degree") or 1),
                        earliest_date_read=None,
                        derivation=derivation,
                        predicate=best_bound.predicate_text if best_bound else None,
                        evidence_sql=chosen.get("sql"),
                        query_run_count=chosen.get("run_count"),
                        provenance=self._src(
                            Source.DATAHUB_QUERIES,
                            unbounded_reason or "no resolvable lower bound on the date column",
                        ),
                    )
                )
                continue

            windows.append(
                ConsumerWindow(
                    consumer_urn=urn,
                    consumer_name=node.get("name") or urn,
                    consumer_type=ctype,
                    platform=node.get("platform"),
                    degree=int(node.get("degree") or 1),
                    earliest_date_read=best,
                    derivation=WindowDerivation.SQL_PREDICATE,
                    predicate=best_bound.predicate_text if best_bound else None,
                    evidence_sql=chosen.get("sql"),
                    query_run_count=chosen.get("run_count"),
                    provenance=self._src(
                        Source.DATAHUB_QUERIES,
                        f"parsed from SQL recorded in DataHub ({chosen.get('urn','')})",
                    ),
                )
            )

        return self._resolve_mediated(windows)

    def _resolve_mediated(self, windows: list[ConsumerWindow]) -> list[ConsumerWindow]:
        """Give multi-hop consumers the bound of whatever they read through.

        A dashboard two hops from the subject does not read the subject directly -- it
        reads an aggregate that does. It therefore cannot see rows that aggregate does
        not see, so the intermediate's bound is a valid (and conservative) bound for it.

        We do not have the exact mediating edge from a flat lineage list, so we inherit
        the EARLIEST bound across all bounded consumers closer to the subject. That is
        the most restrictive choice available and can only over-protect: it can block a
        cutoff that was in fact safe, never allow one that was not.

        Consumers at degree 1 are untouched. They read the subject directly, so a missing
        query really is a missing window, and they keep blocking.
        """
        bounded_by_degree: dict[int, list] = {}
        for w in windows:
            if w.earliest_date_read is not None:
                bounded_by_degree.setdefault(w.degree, []).append(w.earliest_date_read)

        resolved: list[ConsumerWindow] = []
        for w in windows:
            needs_bound = w.earliest_date_read is None and w.degree > 1
            upstream = [d for deg, dates in bounded_by_degree.items() if deg < w.degree for d in dates]
            if needs_bound and upstream:
                inherited = min(upstream)
                resolved.append(
                    w.model_copy(
                        update={
                            "earliest_date_read": inherited,
                            "derivation": WindowDerivation.NOT_A_QUERY_CONSUMER,
                            "provenance": self._src(
                                Source.DATAHUB_LINEAGE,
                                f"reads the subject at {w.degree} hops via an intermediate; "
                                f"inherits the earliest bound of its upstreams "
                                f"({inherited.isoformat()})",
                            ),
                        }
                    )
                )
            else:
                resolved.append(w)
        return resolved

    # -- warehouse side ----------------------------------------------------

    @staticmethod
    def _physical(db: Session, schema: str | None, table: str, date_column: str | None) -> dict:
        """Measured facts. Never estimated -- the previous version of this project
        carried a hardcoded size_gb and it produced a $0.55/month savings headline."""
        qualified = f'"{schema}"."{table}"' if schema else f'"{table}"'
        out: dict = {"row_count": None, "size_bytes": None, "min_date": None, "max_date": None}
        try:
            out["row_count"] = db.execute(text(f"SELECT count(*) FROM {qualified}")).scalar()
            out["size_bytes"] = db.execute(
                text("SELECT pg_total_relation_size(:t)"), {"t": f"{schema or 'public'}.{table}"}
            ).scalar()
        except Exception as exc:  # noqa: BLE001
            # A dataset in the catalog that is not a table in *this* warehouse is normal --
            # downstream dbt models and Snowflake tables both appear in lineage. Postgres
            # aborts the whole transaction on a failed statement, so roll back or every
            # subsequent query in this request dies with InFailedSqlTransaction.
            db.rollback()
            logger.info("no local table for %s (%s); physical facts unavailable", qualified, type(exc).__name__)
            return out

        if date_column:
            try:
                row = db.execute(
                    text(f'SELECT min("{date_column}"), max("{date_column}") FROM {qualified}')
                ).one()
                out["min_date"], out["max_date"] = row[0], row[1]
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.info("date span unavailable for %s: %s", qualified, type(exc).__name__)
        return out

    # -- assembly ----------------------------------------------------------

    async def build(self, db: Session, urn: str, as_of: date | None = None) -> DatasetContext:
        as_of = as_of or date.today()
        raw = await self._gather(urn)
        entity = raw.get("entity")
        schema_name, table_name = table_from_urn(urn)

        date_column, date_prov = self._resolve_date_column(entity)
        physical = self._physical(db, schema_name, table_name, date_column)

        # -- identity / classification
        name = table_name or urn
        platform = "postgres"
        owners: list[str] = []
        domain = None
        tags: list[str] = []
        terms: list[str] = []
        deprecated = False

        if entity:
            name = (entity.get("properties") or {}).get("name") or entity.get("name") or name
            platform = ((entity.get("platform") or {}).get("name")) or platform
            for owner in (entity.get("ownership") or {}).get("owners") or []:
                info = owner.get("owner") or {}
                label = (
                    (info.get("properties") or {}).get("displayName")
                    or info.get("username")
                    or info.get("name")
                )
                if label:
                    owners.append(label)
            domain_node = ((entity.get("domain") or {}).get("domain") or {})
            domain = (domain_node.get("properties") or {}).get("name")
            for tag in (entity.get("tags") or {}).get("tags") or []:
                tag_node = tag.get("tag") or {}
                tags.append((tag_node.get("properties") or {}).get("name") or tag_node.get("name") or "")
            for term in (entity.get("glossaryTerms") or {}).get("terms") or []:
                term_node = term.get("term") or {}
                terms.append((term_node.get("properties") or {}).get("name") or term_node.get("name") or "")
            deprecated = bool((entity.get("deprecation") or {}).get("deprecated"))

        tags = [t for t in tags if t]
        terms = [t for t in terms if t]
        entity_prov = (
            self._src(Source.DATAHUB_SCHEMA, "dataset entity read from GMS")
            if entity
            else _unavailable(raw.get("entity_error") or "dataset not found in DataHub")
        )

        # -- policy from structured properties
        props = raw.get("properties")
        if props is None:
            policy_prov = _unavailable(
                raw.get("properties_error") or "structured properties not readable"
            )
            retention = None
            legal_hold = False
            legal_matter = None
            criticality = None
        else:
            # Count only the policy namespace. `props` also carries the archive.* values
            # this service writes, so len(props) would report "9 policy values" on an
            # already-archived dataset when only 3 of them are policy.
            policy_count = sum(1 for k in props if k.startswith(f"{POLICY_NS}."))
            policy_prov = self._src(
                Source.DATAHUB_PROPERTIES,
                f"{policy_count} {POLICY_NS}.* value(s) read from the catalog",
            )
            retention = _as_float(props.get(f"{POLICY_NS}.retentionYears"))
            hold = str(props.get(f"{POLICY_NS}.legalHold") or "NONE").upper()
            legal_hold = hold == "ACTIVE"
            legal_matter = props.get(f"{POLICY_NS}.legalHoldMatter")
            criticality = _as_float(props.get(f"{POLICY_NS}.businessCriticality"))

        # -- usage
        usage = raw.get("usage")
        usage_observed = False
        if usage is None:
            usage_prov = _unavailable(raw.get("usage_error") or "usage aspect not readable")
            last_query_at = None
            query_count = None
            users = None
        else:
            usage_observed = bool(usage.get("observed"))
            usage_prov = self._src(
                Source.DATAHUB_USAGE,
                "datasetUsageStatistics, 30-day window"
                if usage_observed
                else "no datasetUsageStatistics aspect present for this dataset",
            )
            query_count = usage.get("total_queries")
            users = usage.get("unique_users")
            last_query_at = _epoch_ms(usage.get("last_active_bucket"))

        # -- lineage + windows
        downstream = raw.get("downstream")
        windows = self._windows(downstream, raw.get("queries"), date_column, table_name, as_of)

        sensitive = any(
            token in " ".join(tags + terms).lower() for token in ("pii", "phi", "sensitive", "confidential")
        )

        return DatasetContext(
            urn=urn,
            name=name,
            platform=platform,
            qualified_table=f'"{schema_name}"."{table_name}"' if schema_name else f'"{table_name}"',
            date_column=date_column,
            date_column_provenance=date_prov,
            owners=owners,
            domain=domain,
            tags=tags,
            glossary_terms=terms,
            deprecated=deprecated,
            retention_years=retention,
            legal_hold=legal_hold,
            legal_hold_matter=legal_matter,
            business_criticality=criticality,
            policy_provenance=policy_prov,
            last_query_at=last_query_at,
            query_count_30d=query_count,
            distinct_users_30d=users,
            usage_observed=usage_observed,
            usage_provenance=usage_prov,
            downstream=windows,
            row_count=physical["row_count"],
            size_bytes=physical["size_bytes"],
            min_date=physical["min_date"],
            max_date=physical["max_date"],
            physical_provenance=(
                _from(Source.WAREHOUSE, f"count(*) and pg_total_relation_size on {table_name}")
                if physical["row_count"] is not None
                else _unavailable(f"table {table_name} not readable in the warehouse")
            ),
            sensitive=sensitive,
        )

    async def lineage_complete(self, urn: str) -> bool:
        try:
            await self.client.get_downstream(urn, count=1)
            return True
        except DataHubError:
            return False


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_ms(value) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None
