"""Upgrading a v0.1 database must never cost you your undo history.

These tests build a genuine v1 store from the frozen v0.1 DDL, capture real
history through v1 triggers, then upgrade and check that everything recorded
before the upgrade is still there and still undoable.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

import ctrlz
from ctrlz.migrations import CURRENT_VERSION, attribution_columns, pending

from .conftest import require
from .v1_schema import (
    V1_POSTGRES_SCHEMA,
    V1_SQLITE_SCHEMA,
    v1_sqlite_trigger,
)

PG_DSN_ENV = "CTRLZ_TEST_PG_DSN"


# -- the migration list itself ---------------------------------------------


def test_pending_returns_only_newer_migrations():
    """Asserted against the migration list rather than a hard-coded version,
    so adding a migration does not require editing this test."""
    from ctrlz.migrations import MIGRATIONS

    every_version = [m.version for m in MIGRATIONS]
    assert every_version == sorted(every_version), "migrations must be ordered"
    assert len(set(every_version)) == len(every_version), "versions must be unique"
    assert max(every_version) == CURRENT_VERSION

    assert [m.version for m in pending(0)] == every_version
    assert [m.version for m in pending(1)] == [v for v in every_version if v > 1]
    assert pending(CURRENT_VERSION) == ()


def test_every_migration_is_additive_only():
    """Additive migrations cannot lose data and cannot rewrite a table.

    A DROP or a rewrite in a history table is how an upgrade turns into an
    incident, so the shape of the migrations is asserted rather than trusted.
    """
    for migration in pending(0):
        for statement in migration.postgres:
            upper = statement.upper()
            assert "DROP COLUMN" not in upper
            assert "DROP TABLE" not in upper
            assert "ALTER COLUMN" not in upper
            assert "UPDATE " not in upper       # no backfill
        for _table, _column, column_type in migration.sqlite_columns:
            assert "NOT NULL" not in column_type.upper()


# -- SQLite ----------------------------------------------------------------


def build_v1_sqlite(path) -> None:
    """A real v0.1 database: v1 tables, v1 triggers, real captured history."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, salary REAL);
        """
    )
    conn.executescript(V1_SQLITE_SCHEMA)
    conn.execute(
        "INSERT INTO ctrlz_tracked (table_name, identity) VALUES ('users', ?)",
        (json.dumps(["id"]),),
    )
    image = (
        "json_object('id', {a}.id, 'name', {a}.name, 'salary', {a}.salary)"
    )
    for action, new, old in (("INSERT", "NEW", None), ("UPDATE", "NEW", "OLD"),
                             ("DELETE", None, "OLD")):
        source = new or old
        conn.executescript(
            v1_sqlite_trigger(
                "users",
                action,
                f"json_object('id', {source}.id)",
                image.format(a=old) if old else "NULL",
                image.format(a=new) if new else "NULL",
            )
        )

    # History captured by v1, before the upgrade exists.
    op = uuid.uuid4().hex
    conn.execute(
        "UPDATE ctrlz_current_op SET op_id = ?, label = 'v1 seed', source = 'ctrlz' "
        "WHERE id = 1",
        (op,),
    )
    conn.execute("INSERT INTO users (name, salary) VALUES ('ada', 75000)")
    conn.execute("UPDATE ctrlz_current_op SET op_id = NULL, source = NULL WHERE id = 1")

    op = uuid.uuid4().hex
    conn.execute(
        "UPDATE ctrlz_current_op SET op_id = ?, label = 'v1 mistake', source = 'ctrlz' "
        "WHERE id = 1",
        (op,),
    )
    conn.execute("UPDATE users SET salary = 1 WHERE name = 'ada'")
    conn.execute("UPDATE ctrlz_current_op SET op_id = NULL, source = NULL WHERE id = 1")
    conn.close()


@pytest.fixture
def v1_sqlite(tmp_path):
    path = tmp_path / "v1.db"
    build_v1_sqlite(path)
    return path


def test_v1_sqlite_starts_at_version_one(v1_sqlite):
    tk = ctrlz.connect(f"sqlite:///{v1_sqlite}")
    assert tk.engine.schema_version() == 1
    assert len(tk.log(limit=10)) == 2
    tk.close()


def test_upgrading_sqlite_keeps_the_history(v1_sqlite):
    tk = ctrlz.connect(f"sqlite:///{v1_sqlite}")
    before = [(op.op_id, op.label, op.row_count) for op in tk.log(limit=10)]

    tk.init()

    assert tk.engine.schema_version() == CURRENT_VERSION
    after = [(op.op_id, op.label, op.row_count) for op in tk.log(limit=10)]
    assert after == before
    tk.close()


def test_history_captured_before_the_upgrade_is_still_undoable(v1_sqlite):
    """The point of the whole exercise."""
    tk = ctrlz.connect(f"sqlite:///{v1_sqlite}")
    mistake = [op for op in tk.log(limit=10) if op.label == "v1 mistake"][0]

    tk.init()

    assessment = tk.preview(mistake.op_id)
    assert assessment.status == "undoable"

    tk.undo(mistake.op_id)
    salary = tk.engine.conn.execute("SELECT salary FROM users").fetchone()[0]
    assert salary == 75000
    tk.close()


def test_pre_upgrade_operations_have_no_actor_and_new_ones_do(v1_sqlite, monkeypatch):
    monkeypatch.setenv("CTRLZ_ACTOR", "praveen")
    tk = ctrlz.connect(f"sqlite:///{v1_sqlite}")
    tk.init()
    tk.actor = tk.actor.__class__.resolve(channel="cli")

    old = [op for op in tk.log(limit=10) if op.label == "v1 seed"][0]
    assert old.actor_user is None       # honest: v1 did not record it

    tk.run("INSERT INTO users (name, salary) VALUES ('bob', 5)", label="post upgrade")
    new = tk.log(limit=1)[0]
    assert new.actor_user == "praveen"
    tk.close()


def test_upgrading_twice_is_a_no_op(v1_sqlite):
    tk = ctrlz.connect(f"sqlite:///{v1_sqlite}")
    tk.init()
    first = [(op.op_id, op.label) for op in tk.log(limit=10)]
    tk.init()
    tk.init()
    assert [(op.op_id, op.label) for op in tk.log(limit=10)] == first
    assert tk.engine.schema_version() == CURRENT_VERSION
    tk.close()


def test_capture_still_works_after_the_upgrade(v1_sqlite):
    """The v1 triggers must be rebuilt, or new changes go unrecorded."""
    tk = ctrlz.connect(f"sqlite:///{v1_sqlite}")
    tk.init()

    tk.run("UPDATE users SET salary = 3 WHERE name = 'ada'", label="after upgrade")
    op = tk.log(limit=1)[0]
    assert op.row_count == 1
    assert op.label == "after upgrade"

    tk.undo(op.op_id)
    salary = tk.engine.conn.execute("SELECT salary FROM users").fetchone()[0]
    assert salary == 1      # back to what the v1 mistake left
    tk.close()


# -- PostgreSQL ------------------------------------------------------------


@pytest.fixture
def v1_postgres(request):
    import os

    dsn = os.environ.get(PG_DSN_ENV)
    require("postgres", dsn, PG_DSN_ENV)

    import psycopg2

    schema = f"ctrlz_m_{uuid.uuid4().hex[:8]}"
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS ctrlz CASCADE")
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(
            f"CREATE TABLE {schema}.users ("
            f"  id serial PRIMARY KEY, name text NOT NULL, salary numeric)"
        )
        # A genuine v0.1 store, including the v1 capture function.
        cur.execute(V1_POSTGRES_SCHEMA)
        cur.execute(
            "INSERT INTO ctrlz.tracked (table_schema, table_name, identity) "
            "VALUES (%s, 'users', %s)",
            (schema, ["id"]),
        )
        cur.execute(
            f"CREATE TRIGGER ctrlz_capture AFTER INSERT OR UPDATE OR DELETE "
            f"ON {schema}.users FOR EACH ROW EXECUTE FUNCTION ctrlz.capture('id')"
        )
        cur.execute(
            f"INSERT INTO {schema}.users (name, salary) VALUES ('ada', 75000)"
        )
        cur.execute(f"UPDATE {schema}.users SET salary = 1 WHERE name = 'ada'")

    yield dsn, schema, conn

    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ctrlz CASCADE")
    conn.close()


def test_v1_postgres_upgrades_and_keeps_undoable_history(v1_postgres):
    dsn, schema, _conn = v1_postgres
    tk = ctrlz.connect(dsn)
    tk.engine.default_schema = schema

    assert tk.engine.schema_version() == 1
    before = [(op.op_id, op.row_count) for op in tk.log(limit=10)]
    assert len(before) == 2

    tk.init()

    assert tk.engine.schema_version() == CURRENT_VERSION
    assert [(op.op_id, op.row_count) for op in tk.log(limit=10)] == before

    # The UPDATE captured under v1 is still reversible under v2.
    mistake = tk.log(limit=10)[0]
    assert tk.preview(mistake.op_id).status == "undoable"
    tk.undo(mistake.op_id)

    with tk.engine.conn.cursor() as cur:
        cur.execute(f"SELECT salary FROM {schema}.users")
        assert float(cur.fetchone()[0]) == 75000
    tk.close()


def test_postgres_upgrade_adds_every_attribution_column(v1_postgres):
    dsn, schema, _conn = v1_postgres
    tk = ctrlz.connect(dsn)
    tk.engine.default_schema = schema
    tk.init()

    with tk.engine.conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'ctrlz' AND table_name = 'operations'"
        )
        columns = {r[0] for r in cur.fetchall()}
    assert set(attribution_columns()) <= columns
    tk.close()


def test_postgres_capture_records_attribution_after_upgrade(v1_postgres, monkeypatch):
    dsn, schema, _conn = v1_postgres
    monkeypatch.setenv("CTRLZ_ACTOR", "praveen")

    tk = ctrlz.connect(dsn)
    tk.engine.default_schema = schema
    tk.init()
    tk.actor = tk.actor.__class__.resolve(channel="cli")

    tk.run(f"UPDATE {schema}.users SET salary = 9 WHERE name = 'ada'", label="new")
    op = tk.log(limit=1)[0]
    assert op.actor_user == "praveen"
    assert op.policy_outcome == "allow"
    tk.close()
