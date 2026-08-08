#!/usr/bin/env python3
"""Tests for the frozen-copy catalog record.

Run inside the API container (it needs `app` importable and, for the last few,
a reachable warehouse):

    docker exec coldlineage-datahub-hackathon-10thaug-backend-1 \
        python tests/test_frozen_copy.py

The claim under test: after a verified archive, the frozen copy becomes a
first-class DataHub entity carrying the source's schema, COPY lineage back to
the source, and whatever relationships the *database* declared -- never
relationships inferred from column names.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, date, datetime

from app.datahub.frozen import (
    FROZEN_PLATFORM,
    FROZEN_SUBTYPE,
    FrozenCopyRegistrar,
    _split,
    build_schema_aspect,
    field_type_union,
    frozen_dataset_name,
    frozen_dataset_urn,
    read_foreign_keys,
    read_source_columns,
    resolve_relationship_targets,
)
from app.domain.models import ArchiveManifest

MANIFEST = ArchiveManifest(
    dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
    table="patient_encounters",
    cutoff_date=date(2024, 1, 1),
    rows=516_088,
    bytes=81_000_000,
    parts=[{"key": "p0", "rows": 1, "bytes": 1, "sha256": "a"}],
    sha256="deadbeef" * 8,
    columns=["encounter_id", "event_date"],
    object_uri="s3://coldlineage-archive/patient_encounters/2024-01-01/abc/",
    manifest_uri="s3://coldlineage-archive/patient_encounters/2024-01-01/abc/manifest.json",
    verified_readback=True,
    created_at=datetime.now(UTC),
)


# --- naming ----------------------------------------------------------------


def test_frozen_name_mirrors_the_object_storage_prefix():
    """The URN has to be a usable address, not a label."""
    assert frozen_dataset_name("coldlineage-archive", "patient_encounters", "2024-01-01") == (
        "coldlineage-archive/patient_encounters/2024-01-01"
    )


def test_frozen_urn_is_on_the_object_store_platform_not_postgres():
    """An archive that claims to be a postgres table is exactly the confusion
    this entity exists to remove."""
    urn = frozen_dataset_urn("bkt", "t", "2024-01-01")
    assert f"urn:li:dataPlatform:{FROZEN_PLATFORM}" in urn
    assert "postgres" not in urn


def test_qualified_table_is_split_and_unquoted():
    assert _split('"public"."patient_encounters"') == ("public", "patient_encounters")
    assert _split("patient_encounters") == ("public", "patient_encounters")
    assert _split("analytics.events") == ("analytics", "events")


# --- schema carry-over -----------------------------------------------------


def test_schema_aspect_carries_every_column_with_its_native_type():
    cols = [("encounter_id", "integer", False), ("event_date", "date", True),
            ("payer", "character varying", True)]
    aspect = build_schema_aspect(table="patient_encounters", columns=cols)
    fields = aspect["fields"]
    assert len(fields) == 3
    assert [f["fieldPath"] for f in fields] == ["encounter_id", "event_date", "payer"]
    assert [f["nativeDataType"] for f in fields] == ["integer", "date", "character varying"]
    assert fields[0]["nullable"] is False and fields[1]["nullable"] is True
    assert aspect["platform"] == "urn:li:dataPlatform:s3"


def test_known_postgres_types_map_to_datahub_types():
    assert field_type_union("integer") == {"type": {"com.linkedin.schema.NumberType": {}}}
    assert field_type_union("date") == {"type": {"com.linkedin.schema.DateType": {}}}
    assert field_type_union("boolean") == {"type": {"com.linkedin.schema.BooleanType": {}}}
    assert field_type_union("timestamp without time zone") == {
        "type": {"com.linkedin.schema.TimeType": {}}
    }


def test_unknown_type_becomes_null_not_a_guess():
    """A wrong type in the catalog is worse than an honestly unknown one,
    because a consumer will plan against it."""
    aspect = build_schema_aspect(table="t", columns=[("weird", "tsvector", True)])
    assert aspect["fields"][0]["type"] == {"type": {"com.linkedin.schema.NullType": {}}}
    assert aspect["fields"][0]["nativeDataType"] == "tsvector", "the real type is still recorded"


# --- relationships ---------------------------------------------------------


def test_foreign_keys_become_resolvable_dataset_urns():
    fks = [{"name": "fk_enc", "ref_schema": "public", "ref_table": "patient_encounters",
            "field_mappings": [{"source_field": "encounter_id", "destination_field": "encounter_id"}]}]
    out = resolve_relationship_targets(fks, platform="postgres", database="coldlineage")
    assert out[0]["destination"] == (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)"
    )
    assert out[0]["field_mappings"][0]["source_field"] == "encounter_id"


def test_composite_foreign_key_stays_one_relationship_with_two_mappings():
    fks = [{"name": "fk_two", "ref_schema": "public", "ref_table": "other",
            "field_mappings": [{"source_field": "a", "destination_field": "x"},
                               {"source_field": "b", "destination_field": "y"}]}]
    out = resolve_relationship_targets(fks, platform="postgres", database="db")
    assert len(out) == 1 and len(out[0]["field_mappings"]) == 2


def test_no_foreign_keys_yields_no_relationships():
    assert resolve_relationship_targets([], platform="postgres", database="db") == []


# --- the registrar's aspect set (no network) --------------------------------


def _capture(reg):
    """Intercept _emit so the aspect set can be asserted without a GMS."""
    seen: list[dict] = []

    async def _fake(http, *, entity_type, urn, aspect, value):
        seen.append({"entity_type": entity_type, "urn": urn, "aspect": aspect, "value": value})

    reg._emit = _fake  # type: ignore[method-assign]
    return seen


def test_registrar_emits_the_expected_aspect_set():
    reg = FrozenCopyRegistrar(gms_url="http://unused")
    seen = _capture(reg)
    ops = asyncio.run(
        reg.register(
            manifest=MANIFEST,
            context=_FakeContext(),
            columns=[("encounter_id", "integer", False)],
            er_relationships=[],
        )
    )
    aspects = [s["aspect"] for s in seen]
    assert aspects == [
        "datasetProperties", "schemaMetadata", "subTypes", "upstreamLineage", "globalTags",
    ], aspects
    assert all(o.status == "ok" for o in ops), [o.as_dict() for o in ops]
    assert all(s["entity_type"] == "dataset" for s in seen)


def test_schema_aspect_is_skipped_when_columns_are_unavailable():
    """Better to register the archive with no schema than with an invented one."""
    reg = FrozenCopyRegistrar(gms_url="http://unused")
    seen = _capture(reg)
    asyncio.run(reg.register(manifest=MANIFEST, context=_FakeContext(), columns=[]))
    assert "schemaMetadata" not in [s["aspect"] for s in seen]


def test_lineage_is_copy_not_transformed():
    """The archive is the same rows, moved. Nothing was derived or reshaped, and
    labelling it TRANSFORMED would misrepresent the data to anyone reading the graph."""
    reg = FrozenCopyRegistrar(gms_url="http://unused")
    seen = _capture(reg)
    asyncio.run(reg.register(manifest=MANIFEST, context=_FakeContext(), columns=[]))
    lineage = [s for s in seen if s["aspect"] == "upstreamLineage"][0]["value"]
    assert lineage["upstreams"][0]["type"] == "COPY"
    assert lineage["upstreams"][0]["dataset"] == MANIFEST.dataset_urn


def test_frozen_properties_record_the_verified_checksum_and_source():
    reg = FrozenCopyRegistrar(gms_url="http://unused")
    seen = _capture(reg)
    asyncio.run(reg.register(manifest=MANIFEST, context=_FakeContext(), columns=[]))
    props = [s for s in seen if s["aspect"] == "datasetProperties"][0]["value"]["customProperties"]
    assert props["coldlineage.sha256"] == MANIFEST.sha256
    assert props["coldlineage.source_urn"] == MANIFEST.dataset_urn
    assert props["coldlineage.rows"] == "516088"
    assert props["coldlineage.verified_readback"] == "true"


def test_relationships_are_emitted_as_er_model_relationship_entities():
    reg = FrozenCopyRegistrar(gms_url="http://unused")
    seen = _capture(reg)
    asyncio.run(
        reg.register(
            manifest=MANIFEST,
            context=_FakeContext(),
            columns=[],
            er_relationships=resolve_relationship_targets(
                [{"name": "fk_enc", "ref_schema": "public", "ref_table": "encounters",
                  "field_mappings": [{"source_field": "encounter_id",
                                      "destination_field": "encounter_id"}]}],
                platform="postgres", database="coldlineage",
            ),
        )
    )
    rels = [s for s in seen if s["aspect"] == "erModelRelationshipProperties"]
    assert len(rels) == 1
    assert rels[0]["entity_type"] == "erModelRelationship"
    assert rels[0]["value"]["source"].startswith("urn:li:dataset:(urn:li:dataPlatform:s3,")
    assert rels[0]["value"]["relationshipFieldMappings"] == [
        {"sourceField": "encounter_id", "destinationField": "encounter_id"}
    ]


def test_registration_failure_is_reported_not_raised():
    """The bytes are already safe when this runs. A catalog problem must never
    unwind a completed, verified move."""
    reg = FrozenCopyRegistrar(gms_url="http://unused")

    async def _boom(http, **kwargs):
        raise RuntimeError("gms exploded")

    reg._emit = _boom  # type: ignore[method-assign]
    ops = asyncio.run(reg.register(manifest=MANIFEST, context=_FakeContext(), columns=[]))
    assert ops and all(o.status == "failed" for o in ops)
    assert "gms exploded" in ops[0].detail


def test_subtype_marks_it_as_an_archive():
    assert FROZEN_SUBTYPE == "Cold Tier Archive"


class _FakeContext:
    date_column = "event_date"
    platform = "postgres"
    qualified_table = '"public"."patient_encounters"'
    name = "patient_encounters"


# --- the archive must not become a consumer of its own source --------------
#
# Registering the frozen copy draws COPY lineage back to the source, which makes
# the archive appear DOWNSTREAM of the table it came from. It has no recorded SQL,
# so fail-closed reads it as unbounded -- and cataloguing one archive would block
# every future cutoff on that table. Caught by the smoke test the first time; pinned
# here so it stays caught.


def test_our_own_archive_urns_are_recognised():
    from app.core.config import settings
    from app.datahub.frozen import is_own_archive

    urn = frozen_dataset_urn(settings.minio_bucket, "patient_encounters", "2024-01-01")
    assert is_own_archive(urn)


def test_real_consumers_are_never_mistaken_for_an_archive():
    """A false positive here silently drops a real reader from the safety
    analysis, which is the one mistake this system must never make."""
    from app.datahub.frozen import is_own_archive

    for urn in (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
        "urn:li:dashboard:(superset,quarterly_compliance_dashboard)",
        "urn:li:dataJob:(urn:li:dataFlow:(airflow,ml,PROD),patient_ltv_training)",
        "urn:li:dataset:(urn:li:dataPlatform:s3,someone-elses-bucket/data,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:dbt,coldlineage.analytics.agg,PROD)",
        "",
    ):
        assert not is_own_archive(urn), urn


def test_consumer_analysis_skips_own_archives():
    from app.core.config import settings
    from app.services.context import ContextService

    archive = frozen_dataset_urn(settings.minio_bucket, "patient_encounters", "2024-01-01")
    downstream = [
        {"urn": archive, "type": "DATASET", "name": "archive"},
        {"urn": "urn:li:dashboard:(superset,d)", "type": "DASHBOARD", "name": "a dashboard"},
    ]
    service = ContextService.__new__(ContextService)  # skip __init__; only _src needs a client
    service.client = type("_C", (), {"mode": "live", "recorded_at": None})()
    windows = ContextService._windows(
        service, downstream, [], "event_date", "patient_encounters", date(2026, 8, 8)
    )
    urns = [w.consumer_urn for w in windows]
    assert archive not in urns, "the archive must not appear as a consumer of its own source"
    assert "urn:li:dashboard:(superset,d)" in urns, "real consumers must survive the filter"


# --- against the live warehouse -------------------------------------------


def test_reads_real_columns_from_the_warehouse():
    from app.core.db import SessionLocal

    with SessionLocal() as db:
        cols = read_source_columns(db, '"public"."patient_encounters"')
    names = [c[0] for c in cols]
    assert "encounter_id" in names and "event_date" in names
    assert len(names) >= 10, names
    assert all(isinstance(c[2], bool) for c in cols)


def test_demo_estate_has_no_foreign_keys_so_carry_forward_is_a_no_op():
    """Documented, not hidden: the synthetic estate declares no constraints, so
    there is nothing to carry. The mechanism is exercised by the unit tests above
    and populates on a real warehouse that does declare them."""
    from app.core.db import SessionLocal

    with SessionLocal() as db:
        fks = read_foreign_keys(db, '"public"."patient_encounters"')
    assert fks == []


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
