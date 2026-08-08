#!/usr/bin/env python3
"""Regression tests for the restore transaction.

The bug these exist for: `_resync_identity` assumed the primary key was called
`id` and swallowed the error when it wasn't. But a failed statement aborts the
whole Postgres transaction, so the caller's `db.commit()` silently degraded into
a rollback -- and `POST /api/restore` returned `{"verified": true, "rows": 666839}`
having restored nothing at all.

A restore that lies about success is the worst failure this system could have, so
the invariant is pinned here: **nothing in the identity resync may ever discard
rows that were already appended.**

    docker cp backend/tests/test_restore_integrity.py <api>:/app/ && \
    docker exec <api> python test_restore_integrity.py
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from app.core.db import SessionLocal
from app.services.archive import ArchiveService


class _Ctx:
    """Minimal stand-in for DatasetContext -- only these two fields are read."""

    def __init__(self, name: str, qualified: str):
        self.name = name
        self.qualified_table = qualified


def _drop(db, table):
    db.execute(text(f"DROP TABLE IF EXISTS {table}"))
    db.commit()


def test_resync_on_a_table_with_no_sequence_leaves_the_transaction_usable():
    """The exact shape of the original bug: no sequence-backed column, and the
    caller's later commit must still persist."""
    with SessionLocal() as db:
        _drop(db, "public.t_no_seq")
        db.execute(text("CREATE TABLE public.t_no_seq (k text, v int)"))
        db.commit()
        try:
            db.execute(text("INSERT INTO public.t_no_seq VALUES ('a', 1)"))
            ArchiveService._resync_identity(db, _Ctx("t_no_seq", '"public"."t_no_seq"'))
            db.commit()  # must NOT be degraded into a rollback
            rows = db.execute(text("SELECT count(*) FROM public.t_no_seq")).scalar()
            assert rows == 1, f"the appended row was discarded by the resync (rows={rows})"
        finally:
            _drop(db, "public.t_no_seq")


def test_resync_finds_a_non_id_primary_key():
    """`encounter_id`, not `id` -- the case that originally raised UndefinedColumn."""
    with SessionLocal() as db:
        _drop(db, "public.t_enc")
        db.execute(text("CREATE TABLE public.t_enc (encounter_id serial PRIMARY KEY, v int)"))
        db.execute(text("INSERT INTO public.t_enc (encounter_id, v) VALUES (500, 1)"))
        db.commit()
        try:
            ArchiveService._resync_identity(db, _Ctx("t_enc", '"public"."t_enc"'))
            db.commit()
            nxt = db.execute(text("SELECT nextval(pg_get_serial_sequence('public.t_enc','encounter_id'))")).scalar()
            assert nxt == 501, f"sequence was not fast-forwarded past the restored key (got {nxt})"
        finally:
            _drop(db, "public.t_enc")


def test_resync_survives_a_missing_table_without_poisoning_the_transaction():
    with SessionLocal() as db:
        _drop(db, "public.t_keep")
        db.execute(text("CREATE TABLE public.t_keep (k text)"))
        db.commit()
        try:
            db.execute(text("INSERT INTO public.t_keep VALUES ('x')"))
            ArchiveService._resync_identity(db, _Ctx("t_ghost", '"public"."t_ghost"'))
            db.commit()
            assert db.execute(text("SELECT count(*) FROM public.t_keep")).scalar() == 1
        finally:
            _drop(db, "public.t_keep")


def test_identity_column_is_discovered_not_assumed():
    """Guards the specific regression: no hardcoded 'id' anywhere in the resync."""
    import inspect

    src = inspect.getsource(ArchiveService._resync_identity)
    assert "'id'" not in src and '"id"' not in src, "the resync must not hardcode a column name"
    assert "begin_nested" in src, "the resync must run inside a savepoint"


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
