"""Register the frozen archive as a first-class DataHub entity.

Why this exists
---------------
Before this module, a verified archive left DataHub knowing only that *part of
`patient_encounters` had gone somewhere*: four annotations on the source entity,
one of them a link to a manifest. Search DataHub for the archive itself and you
found nothing, because as far as the catalog was concerned it did not exist.

That is a real hole. The bytes are a governed asset -- they carry PHI, they have
a schema, they have a retention obligation, and somebody will eventually need to
find them without already knowing which source table to look behind.

So once an archive is verified, the frozen copy gets modelled properly:

    s3.coldlineage-archive/patient_encounters/2024-01-01     <-- its own dataset
      datasetProperties   cutoff, rows, sha256, manifest, source urn
      schemaMetadata      the source's columns and types, carried over
      subTypes            "Cold Tier Archive"
      upstreamLineage     COPY from the source dataset
      globalTags          cold-tier-frozen-copy
      erModelRelationship the source's declared FK relationships, retargeted

    postgres.coldlineage.public.patient_encounters           <-- the live table

No bytes are copied. This is metadata describing an object that already landed
and was already verified -- which is why it is cheap, and why it runs only after
`_verify_readback` passes. Cataloguing an archive we have not proven exists would
be worse than not cataloguing it at all.

Transport
---------
Aspects go to GMS's `/aspects?action=ingestProposal` endpoint over httpx, not
through the `acryl-datahub` SDK. The SDK would be the idiomatic choice, but it is
a heavy dependency that pins `pydantic` against this service's own pins -- the
same conflict that had to be untangled in the agent's virtualenv. The REST
proposal endpoint is the transport the SDK itself wraps, so nothing is lost, and
this service keeps one HTTP client for everything.

Every aspect shape below was emitted against a live GMS v1.7.0 and read back
through GraphQL before being committed. The union keys in `schemaMetadata`
(`com.linkedin.schema.*`) are the part that silently accepts a 200 and stores
nothing useful if you get them wrong, so they are verified rather than assumed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.datahub.client import WritebackOperation
from app.domain.models import ArchiveManifest

logger = logging.getLogger(__name__)

FROZEN_TAG_URN = "urn:li:tag:cold-tier-frozen-copy"
FROZEN_SUBTYPE = "Cold Tier Archive"

# The archive lands in object storage, so the frozen dataset belongs to the `s3`
# platform, not to postgres. Getting this wrong would make the archive look like a
# live warehouse table -- precisely the confusion this entity exists to remove.
FROZEN_PLATFORM = "s3"

ACTOR = "urn:li:corpuser:coldlineage"


def frozen_dataset_name(bucket: str, table: str, cutoff: str) -> str:
    """`bucket/table/cutoff` -- mirrors the object-storage prefix exactly.

    Deriving the name from the real prefix makes the URN a usable address:
    whoever finds this entity can go straight to the objects.
    """
    return f"{bucket}/{table}/{cutoff}"


def frozen_dataset_urn(bucket: str, table: str, cutoff: str, env: str = "PROD") -> str:
    return (
        f"urn:li:dataset:(urn:li:dataPlatform:{FROZEN_PLATFORM},"
        f"{frozen_dataset_name(bucket, table, cutoff)},{env})"
    )


def _stamp() -> dict[str, Any]:
    return {"time": int(datetime.now(UTC).timestamp() * 1000), "actor": ACTOR}


def is_own_archive(urn: str) -> bool:
    """True when this URN is one of the frozen-copy entities we mint ourselves.

    The consumer analysis needs this: COPY lineage from an archive back to its
    source makes the archive look downstream of the table it came from, and an
    archive has no SQL, so fail-closed would read it as unbounded and block every
    future cutoff on that table.

    Matched on the URN shape we generate -- the object-store platform plus the
    configured archive bucket. Deliberately narrow: a false positive here would
    silently drop a real consumer from the safety analysis, which is the one kind
    of mistake this system must never make.
    """
    return urn.startswith(
        f"urn:li:dataset:(urn:li:dataPlatform:{FROZEN_PLATFORM},{settings.minio_bucket}/"
    )


# ---------------------------------------------------------------------------
# Reading the source's shape out of the warehouse
# ---------------------------------------------------------------------------


def _split(qualified_table: str) -> tuple[str, str]:
    """`"public"."patient_encounters"` -> ("public", "patient_encounters")."""
    cleaned = qualified_table.replace('"', "")
    schema, _, table = cleaned.rpartition(".")
    return (schema or "public"), table


def read_source_columns(db: Any, qualified_table: str) -> list[tuple[str, str, bool]]:
    """(name, postgres type, nullable) in ordinal order.

    Safe to read after the archive: the delete removes rows, never columns.
    """
    from sqlalchemy import text

    schema, table = _split(qualified_table)
    rows = db.execute(
        text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"
        ),
        {"s": schema, "t": table},
    ).fetchall()
    return [(r[0], r[1], (r[2] or "").upper() == "YES") for r in rows]


def read_foreign_keys(db: Any, qualified_table: str) -> list[dict]:
    """The table's outbound foreign keys, as declared by the database itself.

    This is the honest source for relationships: a constraint the database
    enforces, not two columns that happen to share a name. Column-name similarity
    is a guess, and a guess written into a catalog becomes a fact somebody else
    plans against.

    Returns [] when the table declares none -- which is the case for the synthetic
    demo estate, so the carry-forward is a documented no-op there and is exercised
    by unit tests instead. On a real warehouse with real constraints, it populates.
    """
    from sqlalchemy import text

    schema, table = _split(qualified_table)
    rows = db.execute(
        text(
            """
            SELECT tc.constraint_name,
                   kcu.column_name,
                   ccu.table_schema AS ref_schema,
                   ccu.table_name   AS ref_table,
                   ccu.column_name  AS ref_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
             AND tc.table_schema = ccu.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = :s AND tc.table_name = :t
            ORDER BY tc.constraint_name, kcu.ordinal_position
            """
        ),
        {"s": schema, "t": table},
    ).fetchall()

    grouped: dict[str, dict] = {}
    for constraint, column, ref_schema, ref_table, ref_column in rows:
        entry = grouped.setdefault(
            constraint,
            {
                "name": constraint,
                "ref_schema": ref_schema,
                "ref_table": ref_table,
                "field_mappings": [],
            },
        )
        entry["field_mappings"].append({"source_field": column, "destination_field": ref_column})
    return list(grouped.values())


def resolve_relationship_targets(
    foreign_keys: list[dict], *, platform: str, database: str, env: str = "PROD"
) -> list[dict]:
    """Turn referenced tables into dataset URNs the catalog can actually resolve."""
    return [
        {
            "name": fk["name"],
            "destination": (
                f"urn:li:dataset:(urn:li:dataPlatform:{platform},"
                f"{database}.{fk['ref_schema']}.{fk['ref_table']},{env})"
            ),
            "field_mappings": fk["field_mappings"],
        }
        for fk in foreign_keys
    ]


# ---------------------------------------------------------------------------
# Schema translation
# ---------------------------------------------------------------------------
#
# Postgres types become DataHub's schema type union. Anything unrecognised maps to
# NullType rather than being guessed at: a wrong type in the catalog is worse than
# an honestly unknown one, because a consumer will plan against it. The real
# Postgres type is always preserved in `nativeDataType` either way.

_TYPE_MAP = {
    "integer": "NumberType",
    "bigint": "NumberType",
    "smallint": "NumberType",
    "numeric": "NumberType",
    "decimal": "NumberType",
    "real": "NumberType",
    "double precision": "NumberType",
    "money": "NumberType",
    "text": "StringType",
    "character varying": "StringType",
    "character": "StringType",
    "uuid": "StringType",
    "boolean": "BooleanType",
    "date": "DateType",
    "timestamp without time zone": "TimeType",
    "timestamp with time zone": "TimeType",
    "time without time zone": "TimeType",
    "time with time zone": "TimeType",
    "json": "RecordType",
    "jsonb": "RecordType",
    "bytea": "BytesType",
    "ARRAY": "ArrayType",
}


def field_type_union(pg_type: str) -> dict[str, Any]:
    name = _TYPE_MAP.get((pg_type or "").strip()) or _TYPE_MAP.get((pg_type or "").lower().strip())
    return {"type": {f"com.linkedin.schema.{name or 'NullType'}": {}}}


def build_schema_aspect(
    *, table: str, columns: list[tuple[str, str, bool]], raw_schema: str = ""
) -> dict[str, Any]:
    """Carry the source schema onto the frozen copy.

    `columns` is (name, postgres_type, nullable) in ordinal position.
    """
    stamp = _stamp()
    return {
        "schemaName": table,
        "platform": f"urn:li:dataPlatform:{FROZEN_PLATFORM}",
        "version": 0,
        "hash": "",
        "platformSchema": {"com.linkedin.schema.OtherSchema": {"rawSchema": raw_schema or ""}},
        "fields": [
            {
                "fieldPath": name,
                "type": field_type_union(pg_type),
                "nativeDataType": pg_type,
                "nullable": nullable,
            }
            for name, pg_type, nullable in columns
        ],
        "created": stamp,
        "lastModified": stamp,
    }


# ---------------------------------------------------------------------------
# The registrar
# ---------------------------------------------------------------------------


class FrozenCopyRegistrar:
    """Creates the catalog record for a verified archive.

    Every aspect is reported as its own `WritebackOperation`, so a partial failure
    is visible rather than swallowed -- the same rule the rest of the writeback
    follows.
    """

    def __init__(self, gms_url: str | None = None, token: str | None = None):
        self.gms_url = (gms_url or settings.datahub_gms_url).rstrip("/")
        self.token = token if token is not None else settings.datahub_token

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-RestLi-Protocol-Version": "2.0.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _emit(
        self, http: httpx.AsyncClient, *, entity_type: str, urn: str, aspect: str, value: dict
    ) -> None:
        """One MetadataChangeProposal. Raises on anything but a 2xx."""
        body = {
            "proposal": {
                "entityType": entity_type,
                "entityUrn": urn,
                "changeType": "UPSERT",
                "aspectName": aspect,
                "aspect": {"value": json.dumps(value), "contentType": "application/json"},
            }
        }
        response = await http.post(
            f"{self.gms_url}/aspects?action=ingestProposal", json=body, headers=self._headers()
        )
        if response.status_code >= 300:
            raise RuntimeError(f"{response.status_code}: {response.text[:200]}")

    async def register(
        self,
        *,
        manifest: ArchiveManifest,
        context: Any,
        columns: list[tuple[str, str, bool]],
        er_relationships: list[dict] | None = None,
    ) -> list[WritebackOperation]:
        cutoff = manifest.cutoff_date.isoformat()
        urn = frozen_dataset_urn(settings.minio_bucket, manifest.table, cutoff)
        date_column = getattr(context, "date_column", None) or "the date column"

        aspects: list[tuple[str, dict]] = [
            (
                "datasetProperties",
                {
                    "name": f"{manifest.table} (archived before {cutoff})",
                    "qualifiedName": frozen_dataset_name(
                        settings.minio_bucket, manifest.table, cutoff
                    ),
                    "description": (
                        f"Cold-tier archive of `{manifest.table}`: every row with "
                        f"{date_column} < {cutoff}. {manifest.rows:,} rows in "
                        f"{len(manifest.parts)} Parquet parts, read-back verified against "
                        f"SHA-256 before the source rows were deleted. Restorable via "
                        f"POST /api/restore."
                    ),
                    "customProperties": {
                        "coldlineage.archived_through": cutoff,
                        "coldlineage.rows": str(manifest.rows),
                        "coldlineage.bytes": str(manifest.bytes),
                        "coldlineage.parts": str(len(manifest.parts)),
                        "coldlineage.sha256": manifest.sha256,
                        "coldlineage.manifest_uri": manifest.manifest_uri,
                        "coldlineage.object_uri": manifest.object_uri,
                        "coldlineage.source_urn": manifest.dataset_urn,
                        "coldlineage.verified_readback": str(manifest.verified_readback).lower(),
                        "coldlineage.format": "parquet/snappy",
                    },
                },
            ),
            ("subTypes", {"typeNames": [FROZEN_SUBTYPE]}),
            (
                "upstreamLineage",
                # COPY, not TRANSFORMED: the archive is the same rows, moved.
                # Nothing was derived, aggregated or reshaped, and saying otherwise
                # would misrepresent the data to anyone reading the graph.
                {
                    "upstreams": [
                        {
                            "dataset": manifest.dataset_urn,
                            "type": "COPY",
                            "auditStamp": _stamp(),
                        }
                    ]
                },
            ),
            ("globalTags", {"tags": [{"tag": FROZEN_TAG_URN}]}),
        ]
        if columns:
            aspects.insert(
                1, ("schemaMetadata", build_schema_aspect(table=manifest.table, columns=columns))
            )

        ops: list[WritebackOperation] = []
        async with httpx.AsyncClient(timeout=settings.datahub_timeout_seconds) as http:
            for aspect_name, value in aspects:
                try:
                    await self._emit(
                        http, entity_type="dataset", urn=urn, aspect=aspect_name, value=value
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("frozen copy %s failed: %s", aspect_name, exc)
                    ops.append(
                        WritebackOperation(f"frozen:{aspect_name}", urn, "failed", str(exc)[:300])
                    )
                else:
                    ops.append(
                        WritebackOperation(
                            f"frozen:{aspect_name}",
                            urn,
                            "ok",
                            _describe(aspect_name, manifest, columns),
                        )
                    )

            ops.extend(
                await self._carry_relationships(http, urn, manifest, er_relationships or [])
            )
        return ops

    async def _carry_relationships(
        self,
        http: httpx.AsyncClient,
        frozen_urn: str,
        manifest: ArchiveManifest,
        relationships: list[dict],
    ) -> list[WritebackOperation]:
        """Retarget the source's declared relationships onto the frozen copy.

        DataHub models these as `ERModelRelationship` entities -- catalog-level
        declarations of how two datasets join. We only ever create one from a
        constraint the database actually enforces, so the claim we write is as
        strong as the claim we read.

        Without this the archive looks like an orphan column bag. With it, the
        frozen copy still says "this joins to encounters on encounter_id".
        """
        ops: list[WritebackOperation] = []
        for rel in relationships:
            name = rel.get("name") or "relationship"
            destination = rel.get("destination")
            mappings = rel.get("field_mappings") or []
            if not destination or not mappings:
                continue
            rel_urn = (
                f"urn:li:erModelRelationship:"
                f"{manifest.table}-{manifest.cutoff_date.isoformat()}-{name}"
            )
            value = {
                "name": f"{name} (archived {manifest.cutoff_date.isoformat()})",
                "source": frozen_urn,
                "destination": destination,
                "relationshipFieldMappings": [
                    {"sourceField": m["source_field"], "destinationField": m["destination_field"]}
                    for m in mappings
                ],
                "created": _stamp(),
            }
            try:
                await self._emit(
                    http,
                    entity_type="erModelRelationship",
                    urn=rel_urn,
                    aspect="erModelRelationshipProperties",
                    value=value,
                )
            except Exception as exc:  # noqa: BLE001
                ops.append(
                    WritebackOperation(
                        "frozen:erModelRelationship", rel_urn, "failed", str(exc)[:300]
                    )
                )
            else:
                joined = ", ".join(
                    f"{m['source_field']}={m['destination_field']}" for m in mappings
                )
                target = destination.split(",")[1] if "," in destination else destination
                ops.append(
                    WritebackOperation(
                        "frozen:erModelRelationship",
                        rel_urn,
                        "ok",
                        f"declared join to {target} on {joined}",
                    )
                )
        return ops


def _describe(aspect: str, manifest: ArchiveManifest, columns: list) -> str:
    if aspect == "schemaMetadata":
        return f"{len(columns)} columns carried from the source schema"
    if aspect == "upstreamLineage":
        source = (
            manifest.dataset_urn.split(",")[1] if "," in manifest.dataset_urn else "the source"
        )
        return f"COPY lineage from {source}"
    if aspect == "datasetProperties":
        return f"{manifest.rows:,} rows, cutoff {manifest.cutoff_date.isoformat()}, sha256 recorded"
    if aspect == "subTypes":
        return f"subtype {FROZEN_SUBTYPE!r}"
    if aspect == "globalTags":
        return "tagged cold-tier-frozen-copy"
    return aspect
