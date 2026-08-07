#!/usr/bin/env python
"""Push the ColdLineage demo estate into DataHub so the catalog context is REAL.

    python scripts/ingest_datahub.py
    python scripts/ingest_datahub.py --gms http://localhost:8080 --token "$DATAHUB_TOKEN"
    python scripts/ingest_datahub.py --out-file /tmp/coldlineage-metadata.json  # no GMS needed
    python scripts/ingest_datahub.py --dry-run                                   # build + validate only

WHAT THIS SCRIPT IS FOR
-----------------------
ColdLineage's claim is that its archive decisions are grounded in catalog context
rather than in numbers it made up. That claim is only worth anything if the context
is actually in DataHub. This script puts it there:

  datasets            five Postgres tables, with schemaMetadata generated from the
                      LIVE information_schema, not from a hardcoded column list, and
                      datasetProperties carrying row counts and byte sizes measured
                      with pg_total_relation_size at ingestion time.
  ownership           corpuser entities plus business/technical owners.
  domain              five domains, one per functional area.
  tags                PHI / PII / HIPAA / SOX / LegalHold / tiering.
  glossary terms      four governance terms, attached at dataset and column level.
  lineage             18 downstream consumers -- datasets, dashboards, charts, ML
                      models and data jobs -- each wired to its subject dataset with
                      the aspect DataHub actually traverses for that entity type, so
                      searchAcrossLineage returns them.
  queries             the REAL SQL each consumer runs, as `query` entities with
                      queryProperties.statement and querySubjects pointing at the
                      subject dataset. This is what the backend parses with sqlglot
                      to derive per-consumer history windows.
  usage               datasetUsageStatistics buckets and operation aspects giving
                      each table a last-query time, 30-day query count and distinct
                      user count consistent with its intended temperature.
  policy properties   io.coldlineage.policy.* structured property VALUES
                      (retentionYears, legalHold, legalHoldMatter, businessCriticality).
                      The DEFINITIONS are upserted separately by
                      scripts/bootstrap_datahub.sh from backend/app/datahub/properties.yaml.

HONESTY
-------
The estate is synthetic and says so: every entity carries
`coldlineage.synthetic = "true"` in customProperties, and datasets additionally
carry `coldlineage.demo_role` explaining why that table exists in the demo. What is
NOT synthetic is the measurement -- row counts, byte sizes and min/max dates are read
out of Postgres during this run and stamped with `coldlineage.measured_at`. If
Postgres is unreachable this script refuses to run rather than emit a plausible
number, because a schema or a size that nobody measured is a lie with a timestamp
on it.

Idempotent. Every aspect is an UPSERT keyed by urn, and the timeseries aspects carry
deterministic messageIds so re-running overwrites its own buckets instead of
stacking duplicates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import consumers as C  # noqa: E402
import estate as E  # noqa: E402
from estate import TableSpec  # noqa: E402

try:
    import psycopg
except ImportError:  # pragma: no cover
    print("psycopg is not installed. pip install -r scripts/requirements-seed.txt",
          file=sys.stderr)
    raise SystemExit(2)

from datahub.emitter.mcp import MetadataChangeProposalWrapper as MCP  # noqa: E402
from datahub.ingestion.sink.file import write_metadata_file  # noqa: E402
from datahub.metadata import schema_classes as M  # noqa: E402

DEFAULT_DSN = os.environ.get(
    "COLDLINEAGE_PG_DSN",
    "postgresql://coldlineage:coldlineage@localhost:5433/coldlineage",
)
DEFAULT_GMS = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")

INGESTION_SOURCE = "coldlineage-demo-estate"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def now_ms() -> int:
    return int(time.time() * 1000)


def dt_ms(d: datetime) -> int:
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def date_ms(d: date) -> int:
    return dt_ms(datetime.combine(d, dtime(12, 0), tzinfo=timezone.utc))


def audit(actor: str = "urn:li:corpuser:coldlineage", when_ms: int | None = None):
    return M.AuditStampClass(time=when_ms or now_ms(), actor=actor)


def change_audit(actor: str = "urn:li:corpuser:coldlineage"):
    return M.ChangeAuditStampsClass(created=audit(actor), lastModified=audit(actor))


def stable_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


BASE_PROPS = {
    "coldlineage.synthetic": "true",
    "coldlineage.source": INGESTION_SOURCE,
    "coldlineage.anchor_date": E.ANCHOR.isoformat(),
}


# --------------------------------------------------------------------------
# Postgres introspection -- the schema we emit is the schema that exists
# --------------------------------------------------------------------------


@dataclass
class LiveColumn:
    name: str
    data_type: str          # information_schema.columns.data_type
    native_type: str        # format_type(), e.g. "numeric(12,2)"
    nullable: bool
    ordinal: int
    default: str | None


@dataclass
class LiveTable:
    key: str
    columns: list[LiveColumn]
    row_count: int
    total_bytes: int
    heap_bytes: int
    index_bytes: int
    min_date: date | None
    max_date: date | None
    measured_at: datetime
    primary_keys: list[str] = field(default_factory=list)


INTROSPECT_COLUMNS = """
SELECT c.column_name,
       c.data_type,
       format_type(a.atttypid, a.atttypmod) AS native_type,
       c.is_nullable = 'YES'                AS nullable,
       c.ordinal_position,
       c.column_default
FROM information_schema.columns c
JOIN pg_attribute a
  ON a.attrelid = to_regclass(quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))
 AND a.attname  = c.column_name
WHERE c.table_schema = %s AND c.table_name = %s
ORDER BY c.ordinal_position
"""

INTROSPECT_PK = """
SELECT a.attname
FROM pg_index i
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
WHERE i.indrelid = to_regclass(%s) AND i.indisprimary
"""


def introspect(conn, spec: TableSpec) -> LiveTable:
    with conn.cursor() as cur:
        cur.execute(INTROSPECT_COLUMNS, (E.PG_SCHEMA, spec.key))
        cols = [
            LiveColumn(name=r[0], data_type=r[1], native_type=r[2], nullable=r[3],
                       ordinal=r[4], default=r[5])
            for r in cur.fetchall()
        ]
        if not cols:
            raise RuntimeError(
                f'Table "{E.PG_SCHEMA}"."{spec.key}" does not exist. '
                f"Run scripts/seed_warehouse.py first."
            )
        cur.execute(INTROSPECT_PK, (f"{E.PG_SCHEMA}.{spec.key}",))
        pks = [r[0] for r in cur.fetchall()]

        dc = spec.date_column
        cur.execute(
            f'SELECT count(*), min("{dc}"), max("{dc}") FROM "{E.PG_SCHEMA}"."{spec.key}"'
        )
        row_count, min_d, max_d = cur.fetchone()
        cur.execute(
            "SELECT pg_total_relation_size(%s), pg_relation_size(%s), pg_indexes_size(%s)",
            (f"{E.PG_SCHEMA}.{spec.key}",) * 3,
        )
        total_b, heap_b, idx_b = cur.fetchone()

    return LiveTable(
        key=spec.key, columns=cols, row_count=int(row_count),
        total_bytes=int(total_b), heap_bytes=int(heap_b), index_bytes=int(idx_b),
        min_date=min_d, max_date=max_d,
        measured_at=datetime.now(timezone.utc), primary_keys=pks,
    )


# information_schema.data_type -> DataHub schema field type
_TYPE_MAP: dict[str, type] = {
    "bigint": M.NumberTypeClass,
    "integer": M.NumberTypeClass,
    "smallint": M.NumberTypeClass,
    "numeric": M.NumberTypeClass,
    "double precision": M.NumberTypeClass,
    "real": M.NumberTypeClass,
    "text": M.StringTypeClass,
    "character varying": M.StringTypeClass,
    "character": M.StringTypeClass,
    "boolean": M.BooleanTypeClass,
    "date": M.DateTypeClass,
    "timestamp with time zone": M.TimeTypeClass,
    "timestamp without time zone": M.TimeTypeClass,
    "time with time zone": M.TimeTypeClass,
    "time without time zone": M.TimeTypeClass,
    "jsonb": M.RecordTypeClass,
    "json": M.RecordTypeClass,
    "uuid": M.StringTypeClass,
    "bytea": M.BytesTypeClass,
}


def field_type(pg_type: str) -> M.SchemaFieldDataTypeClass:
    cls = _TYPE_MAP.get(pg_type, M.NullTypeClass)
    return M.SchemaFieldDataTypeClass(type=cls())


# --------------------------------------------------------------------------
# Aspect builders
# --------------------------------------------------------------------------


class Builder:
    """Accumulates MetadataChangeProposals and remembers what was built, so the
    end-of-run summary reports what was actually emitted rather than a guess."""

    def __init__(self, gms: str | None = None, token: str | None = None) -> None:
        self.mcps: list[MCP] = []
        self.tally: dict[str, int] = {}
        self.entities: set[str] = set()
        # Set when a live GMS is reachable, so whole-aspect writes can read the current
        # value first and preserve what other writers put there.
        self.gms = gms
        self.token = token

    def add(self, urn: str, aspect, label: str) -> None:
        self.mcps.append(MCP(entityUrn=urn, aspect=aspect))
        self.tally[label] = self.tally.get(label, 0) + 1
        self.entities.add(urn)

    # ---- supporting entities ------------------------------------------

    def platform_entities(self) -> None:
        for d in E.DOMAINS:
            self.add(d.urn, M.DomainPropertiesClass(
                name=d.name, description=d.description,
                customProperties=dict(BASE_PROPS)), "domain")
        for t in E.TAGS:
            self.add(E.tag_urn(t.name), M.TagPropertiesClass(
                name=t.name, description=t.description, colorHex=t.color_hex), "tag")
        for term in E.GLOSSARY_TERMS:
            self.add(E.term_urn(term.name), M.GlossaryTermInfoClass(
                definition=term.definition,
                termSource="INTERNAL",
                name=term.name,
                customProperties=dict(BASE_PROPS)), "glossaryTerm")
        for p in E.PEOPLE:
            self.add(E.corpuser_urn(p.username), M.CorpUserInfoClass(
                active=True, displayName=p.display_name, email=p.email,
                title=p.title, fullName=p.display_name,
                system=p.username.endswith("-svc") or p.username == "data-platform",
                customProperties=dict(BASE_PROPS)), "corpUser")
        for flow_id, desc in C.AIRFLOW_FLOWS.items():
            self.add(C.dataflow_urn(flow_id), M.DataFlowInfoClass(
                name=flow_id, description=desc, project="coldlineage-demo",
                env=E.DATAHUB_ENV, customProperties=dict(BASE_PROPS)), "dataFlow")

    # ---- subject datasets ---------------------------------------------

    def dataset(self, spec: TableSpec, live: LiveTable) -> None:
        urn = spec.urn
        col_desc = {c.name: c.description for c in spec.columns}
        col_tags = {c.name: c.tags for c in spec.columns}
        col_terms = {c.name: c.terms for c in spec.columns}

        fields = []
        for lc in live.columns:
            tags = col_tags.get(lc.name, ())
            terms = col_terms.get(lc.name, ())
            fields.append(M.SchemaFieldClass(
                fieldPath=lc.name,
                type=field_type(lc.data_type),
                nativeDataType=lc.native_type,
                nullable=lc.nullable,
                description=col_desc.get(
                    lc.name,
                    "Surrogate primary key." if lc.name in live.primary_keys else ""),
                isPartOfKey=lc.name in live.primary_keys,
                globalTags=M.GlobalTagsClass(
                    tags=[M.TagAssociationClass(tag=E.tag_urn(t)) for t in tags]
                ) if tags else None,
                glossaryTerms=M.GlossaryTermsClass(
                    terms=[M.GlossaryTermAssociationClass(urn=E.term_urn(t)) for t in terms],
                    auditStamp=audit(),
                ) if terms else None,
            ))

        raw_schema = spec.create_sql + ";\n" + ";\n".join(spec.index_sql) + ";"
        self.add(urn, M.SchemaMetadataClass(
            schemaName=spec.datahub_name,
            platform=f"urn:li:dataPlatform:{E.PLATFORM_POSTGRES}",
            version=0,
            hash="",
            platformSchema=M.OtherSchemaClass(rawSchema=raw_schema),
            fields=fields,
            primaryKeys=live.primary_keys or None,
            created=audit(),
            lastModified=audit(),
        ), "schemaMetadata")

        props = dict(BASE_PROPS)
        props.update({
            "coldlineage.demo_role": spec.demo_role,
            "coldlineage.date_column": spec.date_column,
            "coldlineage.qualified_table": spec.dotted,
            # Measured, not declared. Stamped so a stale number is visibly stale.
            "coldlineage.measured_at": live.measured_at.isoformat(),
            "coldlineage.measured_row_count": str(live.row_count),
            "coldlineage.measured_total_bytes": str(live.total_bytes),
            "coldlineage.measured_heap_bytes": str(live.heap_bytes),
            "coldlineage.measured_index_bytes": str(live.index_bytes),
            "coldlineage.measurement_method":
                "pg_total_relation_size / pg_relation_size / pg_indexes_size",
            "coldlineage.min_date": live.min_date.isoformat() if live.min_date else "",
            "coldlineage.max_date": live.max_date.isoformat() if live.max_date else "",
        })
        self.add(urn, M.DatasetPropertiesClass(
            name=spec.key,
            qualifiedName=spec.datahub_name,
            description=spec.description + "\n\nDemo role: " + spec.demo_role,
            customProperties=props,
        ), "datasetProperties")

        self.add(urn, M.SubTypesClass(typeNames=["Table"]), "subTypes")

        self.add(urn, M.OwnershipClass(owners=[
            M.OwnerClass(owner=E.corpuser_urn(u), type=getattr(M.OwnershipTypeClass, t))
            for u, t in spec.owners
        ], lastModified=audit()), "ownership")

        # globalTags is a whole-aspect write, so emitting only the estate's own tags would
        # silently drop any tag another writer added -- including `cold-tier-archived`,
        # which ColdLineage itself applies after an archive. Re-ingesting would quietly
        # erase the very marker that tells downstream readers history is missing. Read the
        # current tags first and union. This is the same hazard the backend's writeback
        # docstring warns about for datasetProperties.
        preserved: list[str] = []
        existing = _read_existing_tags(self.gms, self.token, urn) if self.gms else []
        estate_tags = {E.tag_urn(t) for t in spec.tags}
        for tag_urn in existing:
            if tag_urn not in estate_tags:
                preserved.append(tag_urn)
        if preserved:
            print(f"    preserving {len(preserved)} externally-applied tag(s): "
                  f"{', '.join(t.split(':')[-1] for t in preserved)}")

        self.add(urn, M.GlobalTagsClass(tags=[
            M.TagAssociationClass(tag=E.tag_urn(t)) for t in spec.tags
        ] + [
            M.TagAssociationClass(tag=t) for t in preserved
        ]), "globalTags")

        self.add(urn, M.GlossaryTermsClass(
            terms=[M.GlossaryTermAssociationClass(urn=E.term_urn(t)) for t in spec.terms],
            auditStamp=audit(),
        ), "glossaryTerms")

        self.add(urn, M.DomainsClass(
            domains=[E.DOMAIN_BY_NAME[spec.domain].urn]), "domains")

    # ---- policy structured property VALUES ----------------------------

    def policy_properties(self, spec: TableSpec) -> MCP:
        """Values only. Definitions come from backend/app/datahub/properties.yaml.

        Returned rather than added, so the caller can emit these in an isolated
        phase and give a precise error if the definitions were never upserted.
        """
        p = spec.policy
        assignments = [
            M.StructuredPropertyValueAssignmentClass(
                propertyUrn=E.property_urn(E.POLICY_RETENTION_YEARS),
                values=[float(p.retention_years)], lastModified=audit()),
            M.StructuredPropertyValueAssignmentClass(
                propertyUrn=E.property_urn(E.POLICY_LEGAL_HOLD),
                values=[p.legal_hold], lastModified=audit()),
            M.StructuredPropertyValueAssignmentClass(
                propertyUrn=E.property_urn(E.POLICY_BUSINESS_CRITICALITY),
                values=[float(p.business_criticality)], lastModified=audit()),
        ]
        if p.legal_hold_matter:
            assignments.append(M.StructuredPropertyValueAssignmentClass(
                propertyUrn=E.property_urn(E.POLICY_LEGAL_HOLD_MATTER),
                values=[p.legal_hold_matter], lastModified=audit()))
        return MCP(entityUrn=spec.urn,
                   aspect=M.StructuredPropertiesClass(properties=assignments))

    # ---- usage telemetry ----------------------------------------------

    def usage(self, spec: TableSpec, days: int = 30) -> None:
        """Daily datasetUsageStatistics buckets plus a final operation aspect.

        The buckets are shaped by the table's UsageProfile so the aggregate a
        consumer of DataHub sees matches the temperature the table is meant to
        exhibit. lab_results emits thirty consecutive ZERO buckets on purpose --
        that is precisely the signal that makes every dataset-level tiering tool
        recommend archiving it, and precisely the signal ColdLineage is built to
        distrust.
        """
        u = spec.usage
        urn = spec.urn
        total = u.query_count_30d
        # Weight recent days slightly heavier, then correct the rounding drift so
        # the emitted buckets sum to exactly query_count_30d.
        weights = [1.0 + 0.5 * (i / max(days - 1, 1)) for i in range(days)]
        wsum = sum(weights)
        counts = [int(round(total * w / wsum)) for w in weights]
        drift = total - sum(counts)
        if counts:
            counts[-1] += drift

        users = [E.PEOPLE_BY_KEY[k] for k in u.top_user_keys if k in E.PEOPLE_BY_KEY]
        top_sql = [
            c.sql for c in C.for_table(spec.key) if c.sql
        ][:5]

        for i in range(days):
            bucket_day = E.ANCHOR - timedelta(days=days - 1 - i)
            ts = dt_ms(datetime.combine(bucket_day, dtime(0, 0), tzinfo=timezone.utc))
            n = counts[i]
            if users and n:
                # Zipf-ish split across the known users, remainder to the first.
                shares = [max(1, int(n * w)) for w in (0.5, 0.25, 0.15, 0.10)[:len(users)]]
                shares[0] += n - sum(shares)
                user_counts = [
                    M.DatasetUserUsageCountsClass(
                        user=E.corpuser_urn(p.username), count=max(0, s), userEmail=p.email)
                    for p, s in zip(users, shares)
                ]
            else:
                user_counts = []
            self.add(urn, M.DatasetUsageStatisticsClass(
                timestampMillis=ts,
                eventGranularity=M.TimeWindowSizeClass(unit=M.CalendarIntervalClass.DAY,
                                                       multiple=1),
                uniqueUserCount=len(user_counts) if n else 0,
                totalSqlQueries=n,
                topSqlQueries=top_sql if n else [],
                userCounts=user_counts,
                # Deterministic: a re-run overwrites this bucket rather than adding one.
                messageId=f"{INGESTION_SOURCE}:{spec.key}:usage:{bucket_day.isoformat()}",
            ), "datasetUsageStatistics")

        last_q = u.last_query_date()
        last_actor = (E.corpuser_urn(u.top_user_keys[0]) if u.top_user_keys
                      else E.corpuser_urn("compliance-svc"))
        self.add(urn, M.OperationClass(
            timestampMillis=date_ms(last_q),
            lastUpdatedTimestamp=date_ms(last_q),
            operationType=M.OperationTypeClass.CUSTOM,
            customOperationType="QUERY",
            actor=last_actor,
            sourceType=M.OperationSourceTypeClass.DATA_PLATFORM,
            numAffectedRows=0,
            customProperties={
                **BASE_PROPS,
                "coldlineage.last_query_at": last_q.isoformat(),
                "coldlineage.query_count_30d": str(u.query_count_30d),
                "coldlineage.distinct_users_30d": str(u.distinct_users_30d),
            },
            messageId=f"{INGESTION_SOURCE}:{spec.key}:operation:{last_q.isoformat()}",
        ), "operation")

    # ---- consumers and lineage ----------------------------------------

    def consumer(self, c: C.Consumer) -> None:
        subject = c.subject.urn
        props = dict(BASE_PROPS)
        props.update({
            "coldlineage.subject_urn": subject,
            "coldlineage.subject_table": c.subject.dotted,
            "coldlineage.subject_date_column": c.subject.date_column,
            "coldlineage.consumer_key": c.key,
            "coldlineage.lineage_degree": str(c.degree),
        })
        if c.last_run_at:
            props["coldlineage.last_run_at"] = c.last_run_at.isoformat()
        if c.run_count is not None:
            props["coldlineage.run_count"] = str(c.run_count)

        upstream_urn = c.reads_via or subject

        if c.consumer_type == C.DATASET:
            name = c.urn.split(",")[1]
            self.add(c.urn, M.DatasetPropertiesClass(
                name=c.name, qualifiedName=name, description=c.description,
                customProperties=props, externalUrl=c.external_url), "consumer.dataset")
            self.add(c.urn, M.SubTypesClass(
                typeNames=["View" if c.platform == "dbt" else "Table"]), "subTypes")
            self.add(c.urn, M.UpstreamLineageClass(upstreams=[
                M.UpstreamClass(dataset=upstream_urn,
                                type=M.DatasetLineageTypeClass.TRANSFORMED,
                                auditStamp=audit())
            ]), "upstreamLineage")

        elif c.consumer_type == C.DASHBOARD:
            self.add(c.urn, M.DashboardInfoClass(
                title=c.name, description=c.description,
                lastModified=change_audit(),
                dashboardUrl=c.external_url, externalUrl=c.external_url,
                customProperties=props,
                datasetEdges=[M.EdgeClass(destinationUrn=upstream_urn,
                                          sourceUrn=c.urn, lastModified=audit())],
                lastRefreshed=dt_ms(c.last_run_at) if c.last_run_at else None,
            ), "consumer.dashboard")

        elif c.consumer_type == C.CHART:
            self.add(c.urn, M.ChartInfoClass(
                title=c.name, description=c.description,
                lastModified=change_audit(),
                chartUrl=c.external_url, externalUrl=c.external_url,
                customProperties=props,
                inputEdges=[M.EdgeClass(destinationUrn=upstream_urn,
                                        sourceUrn=c.urn, lastModified=audit())],
                type=M.ChartTypeClass.BAR,
                lastRefreshed=dt_ms(c.last_run_at) if c.last_run_at else None,
            ), "consumer.chart")

        elif c.consumer_type == C.DATA_JOB:
            flow_id = c.urn.split("dataFlow:(")[1].split(",")[1]
            job_id = c.urn.rsplit(",", 1)[1].rstrip(")")
            self.add(c.urn, M.DataJobInfoClass(
                name=c.name, type="COMMAND", description=c.description,
                flowUrn=C.dataflow_urn(flow_id), externalUrl=c.external_url,
                env=E.DATAHUB_ENV, customProperties=props,
                status=M.JobStatusClass.COMPLETED,
            ), "consumer.dataJob")
            self.add(c.urn, M.DataJobInputOutputClass(
                inputDatasets=[upstream_urn], outputDatasets=[],
                inputDatasetEdges=[M.EdgeClass(destinationUrn=upstream_urn,
                                               sourceUrn=c.urn, lastModified=audit())],
            ), "dataJobInputOutput")
            _ = job_id

        elif c.consumer_type == C.MLMODEL:
            # DataHub has no mlModel -> dataset lineage edge. The traversable
            # relationship is mlModel --TrainedBy--> dataJob --Consumes--> dataset,
            # so the model is honestly a degree-2 consumer and the training job is
            # the entity that actually reads the table.
            assert c.lineage_via_job, f"{c.key}: MLMODEL consumers need lineage_via_job"
            job = c.lineage_via_job
            flow_id = job.split("dataFlow:(")[1].split(",")[1]
            self.add(job, M.DataJobInfoClass(
                name=f"{c.key}_training", type="COMMAND",
                description=f"Training job for {c.name}. Issues the model's feature query "
                            f"against {c.subject.dotted}.",
                flowUrn=C.dataflow_urn(flow_id), env=E.DATAHUB_ENV,
                customProperties={**props, "coldlineage.role": "ml_training_job"},
                status=M.JobStatusClass.COMPLETED,
            ), "consumer.dataJob")
            self.add(job, M.DataJobInputOutputClass(
                inputDatasets=[subject], outputDatasets=[],
                inputDatasetEdges=[M.EdgeClass(destinationUrn=subject,
                                               sourceUrn=job, lastModified=audit())],
            ), "dataJobInputOutput")
            self.add(c.urn, M.MLModelPropertiesClass(
                name=c.name, description=c.description,
                externalUrl=c.external_url,
                trainingJobs=[job],
                type="gradient_boosted_trees",
                date=dt_ms(c.last_run_at) if c.last_run_at else None,
                customProperties=props,
            ), "consumer.mlModel")

        else:  # pragma: no cover
            raise ValueError(f"unknown consumer type {c.consumer_type}")

    # ---- query entities: the real SQL --------------------------------

    def query(self, c: C.Consumer) -> None:
        if c.sql is None:
            return
        created = audit(
            actor=E.corpuser_urn("coldlineage"),
            when_ms=dt_ms(c.last_run_at) if c.last_run_at else now_ms(),
        )
        props = {
            **BASE_PROPS,
            # The backend reads these off queryProperties to attribute a statement
            # to the consumer that runs it. DataHub's querySubjects only models
            # dataset/schemaField subjects, so the dashboard/chart/model/job identity
            # has to travel here.
            "coldlineage.consumer_urn": c.urn,
            # An MLMODEL reaches the subject through a training DataJob, so the lineage
            # edge belongs to the job while the SQL belongs to the model. Stamping the
            # carrier too lets the backend attribute this statement to both, instead of
            # scoring the job as a consumer with no observable query window.
            **({"coldlineage.carrier_urn": c.lineage_via_job} if c.lineage_via_job else {}),
            # A consumer that reads through an intermediate cannot see rows the
            # intermediate does not read. Recording the mediator lets the backend
            # inherit that bound rather than treating the hop as unbounded.
            **({"coldlineage.reads_via": c.reads_via} if c.reads_via else {}),
            "coldlineage.consumer_key": c.key,
            "coldlineage.consumer_name": c.name,
            "coldlineage.consumer_type": c.consumer_type,
            "coldlineage.consumer_platform": c.platform,
            "coldlineage.subject_urn": c.subject.urn,
            "coldlineage.subject_date_column": c.subject.date_column,
            "coldlineage.lineage_degree": str(c.degree),
            "coldlineage.dialect": "postgres",
        }
        if c.last_run_at:
            props["coldlineage.last_run_at"] = c.last_run_at.isoformat()
        if c.run_count is not None:
            props["coldlineage.run_count"] = str(c.run_count)

        self.add(c.query_urn, M.QueryPropertiesClass(
            statement=M.QueryStatementClass(value=c.sql,
                                            language=M.QueryLanguageClass.SQL),
            source=M.QuerySourceClass.SYSTEM,
            created=created,
            lastModified=created,
            name=f"{c.name} -- {c.subject.key}",
            description=c.description,
            customProperties=props,
        ), "queryProperties")

        subjects = [M.QuerySubjectClass(entity=c.subject.urn)]
        if c.consumer_type == C.DATASET:
            subjects.append(M.QuerySubjectClass(entity=c.urn))
        self.add(c.query_urn, M.QuerySubjectsClass(subjects=subjects), "querySubjects")


# --------------------------------------------------------------------------
# GMS reachability
# --------------------------------------------------------------------------


def _read_existing_tags(gms: str, token: str | None, urn: str, timeout: float = 8.0) -> list[str]:
    """Current globalTags on an entity, so a whole-aspect write can union rather than
    replace. Returns [] on any failure -- losing a preserved tag is bad, but failing the
    whole ingest because the catalog was briefly unreachable is worse."""
    import json as _json
    import urllib.error
    import urllib.request

    query = (
        "query($u:String!){dataset(urn:$u){tags{tags{tag{urn}}}}}"
    )
    body = _json.dumps({"query": query, "variables": {"u": urn}}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(
            gms.rstrip("/") + "/api/graphql", data=body, headers=headers
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = _json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return []
    dataset = ((payload.get("data") or {}).get("dataset")) or {}
    return [
        (t.get("tag") or {}).get("urn")
        for t in ((dataset.get("tags") or {}).get("tags") or [])
        if (t.get("tag") or {}).get("urn")
    ]


def probe_gms(gms: str, token: str | None, timeout: float = 6.0) -> tuple[bool, str]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(gms.rstrip("/") + "/config")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
            try:
                cfg = json.loads(body)
                ver = cfg.get("versions", {}).get("acryldata/datahub", {}).get("version", "?")
                return True, f"GMS reachable (version {ver})"
            except json.JSONDecodeError:
                return True, "GMS reachable"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, (f"GMS answered {exc.code} at {gms}. It is up but rejected the "
                           f"credentials. Set DATAHUB_TOKEN or pass --token.")
        return False, f"GMS answered HTTP {exc.code} at {gms}"
    except Exception as exc:  # noqa: BLE001
        return False, f"GMS not reachable at {gms}: {exc}"


def structured_properties_present(gms: str, token: str | None) -> tuple[bool, str]:
    """Check that at least one io.coldlineage.policy.* DEFINITION exists.

    Emitting a value for a property that was never defined is silently useless in
    some GMS versions and a hard error in others, and either way the demo loses its
    policy inputs. Better to say so up front.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    purn = E.property_urn(E.POLICY_LEGAL_HOLD)
    url = (gms.rstrip("/") + "/openapi/v2/entity/structuredproperty/"
           + urllib.parse.quote(purn, safe="") + "/propertyDefinition")
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                return True, f"structured property definitions present ({purn})"
            return False, f"unexpected HTTP {resp.status} checking {purn}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, (
                f"structured property {purn} is NOT defined in this GMS.\n"
                f"    Upsert the definitions first:\n"
                f"      datahub properties upsert -f backend/app/datahub/properties.yaml"
            )
        return False, f"HTTP {exc.code} checking {purn}"
    except Exception as exc:  # noqa: BLE001
        return False, f"could not check structured property definitions: {exc}"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:  # noqa: C901
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gms", default=DEFAULT_GMS, help=f"GMS URL (default {DEFAULT_GMS})")
    ap.add_argument("--token", default=os.environ.get("DATAHUB_TOKEN") or None)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--out-file", default=None, metavar="PATH",
                    help="Write the metadata to a DataHub metadata file instead of "
                         "emitting. Usable with no GMS at all.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build and validate every aspect, emit nothing.")
    ap.add_argument("--skip-policy-properties", action="store_true",
                    help="Skip io.coldlineage.policy.* VALUES (use if the definitions "
                         "have not been upserted yet).")
    ap.add_argument("--usage-days", type=int, default=30,
                    help="How many daily usage buckets to emit per dataset.")
    ap.add_argument("--ui-url", default=os.environ.get("DATAHUB_FRONTEND_URL"),
                    help="DataHub UI base URL for the deep links printed at the end. "
                         "Defaults to DATAHUB_FRONTEND_URL, else http://localhost:9002.")
    args = ap.parse_args()

    offline = bool(args.out_file) or args.dry_run

    print("ColdLineage -- ingesting the demo estate into DataHub")
    print(f"  anchor date : {E.ANCHOR.isoformat()}")
    print(f"  warehouse   : {args.dsn.split('@')[-1]}")
    print(f"  gms         : {'(offline)' if offline else args.gms}")
    print()

    # ---- 1. measure the warehouse. no measurement, no emission. --------
    print("[1/5] Introspecting and measuring Postgres")
    try:
        conn = psycopg.connect(args.dsn, autocommit=True, connect_timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: cannot reach Postgres at {args.dsn.split('@')[-1]}: {exc}",
              file=sys.stderr)
        print("  This script will not emit a schema or a size it did not measure.",
              file=sys.stderr)
        print("  Start it with:  docker compose up -d postgres", file=sys.stderr)
        print("  Then seed it :  python scripts/seed_warehouse.py", file=sys.stderr)
        return 1

    live: dict[str, LiveTable] = {}
    try:
        for spec in E.TABLES:
            lt = introspect(conn, spec)
            live[spec.key] = lt
            print(f"  {spec.key:<20} {len(lt.columns):>2} cols  {lt.row_count:>9,} rows  "
                  f"{lt.total_bytes:>12,} bytes  "
                  f"{lt.min_date} .. {lt.max_date}")
    except RuntimeError as exc:
        print(f"  FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print()

    # ---- 2. reachability ------------------------------------------------
    emitter = None
    props_ok = False
    if not offline:
        print("[2/5] Checking DataHub")
        ok, detail = probe_gms(args.gms, args.token)
        print(f"  {detail}")
        if not ok:
            print("\n  Nothing was emitted. Bring GMS up, or run with --out-file "
                  "to produce a metadata file you can ingest later:", file=sys.stderr)
            print("    python scripts/ingest_datahub.py --out-file "
                  "/tmp/coldlineage-metadata.json", file=sys.stderr)
            return 1
        props_ok, pdetail = structured_properties_present(args.gms, args.token)
        print(f"  {pdetail}")
        from datahub.emitter.rest_emitter import DataHubRestEmitter
        emitter = DataHubRestEmitter(gms_server=args.gms, token=args.token,
                                     connect_timeout_sec=10, read_timeout_sec=60)
    else:
        print("[2/5] Offline mode -- skipping GMS checks")
    print()

    # ---- 3. build --------------------------------------------------------
    print("[3/5] Building metadata change proposals")
    # Pass GMS through only when we are actually going to emit; in offline/dry-run mode
    # there is nothing to preserve and no catalog to ask.
    b = Builder(gms=args.gms if not args.out_file else None, token=args.token)
    b.platform_entities()
    for spec in E.TABLES:
        b.dataset(spec, live[spec.key])
        b.usage(spec, days=args.usage_days)
    for c in C.CONSUMERS:
        b.consumer(c)
        b.query(c)

    policy_mcps: list[MCP] = []
    if not args.skip_policy_properties:
        policy_mcps = [b.policy_properties(spec) for spec in E.TABLES]

    for label in sorted(b.tally):
        print(f"  {label:<28} {b.tally[label]:>4}")
    if policy_mcps:
        print(f"  {'structuredProperties':<28} {len(policy_mcps):>4}  (policy VALUES)")
    print(f"  {'-' * 28} ----")
    print(f"  {'total aspects':<28} {len(b.mcps) + len(policy_mcps):>4} "
          f"across {len(b.entities)} entities")
    print()

    # ---- 4. emit ---------------------------------------------------------
    print("[4/5] Emitting")
    all_mcps = b.mcps + policy_mcps
    if args.out_file:
        write_metadata_file(Path(args.out_file), all_mcps)
        print(f"  wrote {len(all_mcps)} aspects to {args.out_file}")
        print("  ingest it later with:")
        print(f"    datahub ingest -c <(printf 'source:\\n  type: file\\n  config:\\n"
              f"    path: {args.out_file}\\nsink:\\n  type: datahub-rest\\n  config:\\n"
              f"    server: {args.gms}\\n')")
    elif args.dry_run:
        # Serialising every aspect exercises the same Avro validation the emitter
        # does, so a dry run really does prove the payload is well formed.
        bad = 0
        for m in all_mcps:
            try:
                m.make_mcp().validate()
            except Exception as exc:  # noqa: BLE001
                bad += 1
                print(f"  INVALID {m.entityUrn} :: {exc}", file=sys.stderr)
        print(f"  dry run: validated {len(all_mcps)} aspects, {bad} invalid")
        if bad:
            return 1
    else:
        assert emitter is not None
        failures: list[tuple[str, str]] = []
        sent = 0
        t0 = time.perf_counter()
        for m in b.mcps:
            try:
                emitter.emit(m)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                failures.append((f"{m.entityUrn} [{m.aspectName}]", str(exc)[:200]))
        print(f"  {sent}/{len(b.mcps)} core aspects emitted in "
              f"{time.perf_counter() - t0:,.1f}s")

        if policy_mcps:
            if not props_ok:
                print("  SKIPPED io.coldlineage.policy.* values: the property "
                      "definitions are not present in this GMS.")
                print("    Run: datahub properties upsert -f "
                      "backend/app/datahub/properties.yaml")
                print("    Then re-run this script. Without them the demo has no "
                      "retention floor and no legal hold.")
                failures.append(("structuredProperties",
                                 "property definitions missing; values not written"))
            else:
                psent = 0
                for m in policy_mcps:
                    try:
                        emitter.emit(m)
                        psent += 1
                    except Exception as exc:  # noqa: BLE001
                        failures.append((f"{m.entityUrn} [structuredProperties]",
                                         str(exc)[:200]))
                print(f"  {psent}/{len(policy_mcps)} policy structured-property "
                      f"value sets emitted")

        if failures:
            print(f"\n  {len(failures)} failure(s):", file=sys.stderr)
            for target, err in failures[:20]:
                print(f"    {target}\n      {err}", file=sys.stderr)
            return 1
    print()

    # ---- 5. report exactly what went in ---------------------------------
    print("[5/5] What is now in DataHub")
    # The UI is a different service from GMS, so its URL cannot be derived by
    # string-substituting the port -- that only happens to work for the default
    # quickstart layout and prints a dead link for every other deployment.
    ui = (args.ui_url or "http://localhost:9002").rstrip("/")
    for spec in E.TABLES:
        lt = live[spec.key]
        cons = C.for_table(spec.key)
        with_sql = [c for c in cons if c.sql]
        unbounded = [c for c in cons if c.expectation.derivation == "no_date_filter"]
        print(f"\n  {spec.key}")
        print(f"    urn            {spec.urn}")
        print(f"    measured       {lt.row_count:,} rows / {lt.total_bytes:,} bytes / "
              f"{spec.date_column} {lt.min_date} .. {lt.max_date}")
        print(f"    domain / tags  {spec.domain} / {', '.join(spec.tags)}")
        print(f"    policy         retentionYears={spec.policy.retention_years} "
              f"legalHold={spec.policy.legal_hold} "
              f"businessCriticality={spec.policy.business_criticality}"
              + (f" matter={spec.policy.legal_hold_matter!r}"
                 if spec.policy.legal_hold_matter else ""))
        print(f"    usage          last_query={spec.usage.last_query_date().isoformat()} "
              f"queries_30d={spec.usage.query_count_30d} "
              f"users_30d={spec.usage.distinct_users_30d}")
        print(f"    consumers      {len(cons)} "
              f"({', '.join(sorted({c.consumer_type for c in cons}))})")
        print(f"    queries        {len(with_sql)} SQL statements ingested as "
              f"query entities")
        if unbounded:
            print(f"    ** UNBOUNDED   {', '.join(c.key for c in unbounded)} "
                  f"-- reads the whole table, no date predicate")
        if not offline:
            print(f"    open           {ui}/dataset/{spec.urn}")

    print()
    print("Rows and telemetry are synthetic and every entity is stamped "
          "coldlineage.synthetic=true.")
    print("Row counts, byte sizes, column types and date ranges were measured from "
          "Postgres during this run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
