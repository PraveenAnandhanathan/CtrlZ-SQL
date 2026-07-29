"""The control plane: a replica that must never be mistaken for the source.

Most of what is asserted here is what the hub *does not* do. A shipped copy of
an undo history is a convenience; the risk is that somebody starts treating it
as the record, or that row values end up somewhere nobody agreed to put them.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

import ctrlz
from ctrlz.hub import Hub, ship

PG_DSN = os.environ.get("CTRLZ_TEST_PG_DSN")


@pytest.fixture
def source(tmp_path):
    """A database with real captured history."""
    path = tmp_path / "source.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, salary REAL);"
        "INSERT INTO users (name, salary) VALUES ('ada', 75000), ('bob', 50000);"
    )
    conn.commit()
    conn.close()

    toolkit = ctrlz.connect(f"sqlite:///{path}")
    toolkit.init()
    toolkit.track("users")
    yield toolkit
    toolkit.close()


@pytest.fixture
def hub(tmp_path):
    store = Hub(f"sqlite:///{tmp_path / 'hub.db'}")
    yield store
    store.close()


def make_history(source):
    source.run("UPDATE users SET salary = 90000 WHERE name = 'ada'", label="raise")
    source.run("DELETE FROM users WHERE name = 'bob'", label="remove bob")


# -- shipping --------------------------------------------------------------


def test_history_reaches_the_hub(source, hub):
    make_history(source)
    result = ship(source, hub, name="billing")

    assert result.operations == 2
    assert result.changes == 2

    shipped = hub.operations()
    assert {op.label for op in shipped} == {"raise", "remove bob"}
    assert all(op.source_name == "billing" for op in shipped)


def test_shipping_twice_changes_nothing(source, hub):
    """Idempotent, because a shipper that duplicates on retry cannot be retried."""
    make_history(source)
    ship(source, hub, name="billing")
    before = [(op.op_id, op.row_count) for op in hub.operations()]

    second = ship(source, hub, name="billing")
    assert second.operations == 0
    assert second.changes == 0
    assert [(op.op_id, op.row_count) for op in hub.operations()] == before


def test_shipping_resumes_from_the_watermark(source, hub):
    make_history(source)
    ship(source, hub, name="billing")

    source.run("UPDATE users SET salary = 1 WHERE name = 'ada'", label="later")
    result = ship(source, hub, name="billing")

    assert result.operations == 1
    assert result.changes == 1
    assert len(hub.operations()) == 3


def test_an_interrupted_run_loses_nothing(source, hub):
    """Progress is a watermark, advanced only after a batch is stored.

    Shipping one change at a time and re-running is the same as shipping the
    lot: an interruption costs a repeat, never a gap.
    """
    for index in range(4):
        source.run(
            f"UPDATE users SET salary = {index + 1} WHERE name = 'ada'",
            label=f"step {index}",
        )

    while True:
        result = ship(source, hub, name="billing", batch=1)
        if not result.moved_anything:
            break

    labels = {op.label for op in hub.operations()}
    assert labels == {f"step {i}" for i in range(4)}


def test_an_undo_after_shipping_is_picked_up_on_the_next_run(source, hub):
    """The watermark cannot see this: undoing sets a column on a shipped row."""
    make_history(source)
    ship(source, hub, name="billing")
    assert all(not op.already_undone for op in hub.operations())

    operation = [op for op in source.log(limit=5) if op.label == "raise"][0]
    source.undo(operation.op_id)

    result = ship(source, hub, name="billing")
    assert result.refreshed >= 1

    shipped = {op.op_id: op for op in hub.operations()}
    assert shipped[operation.op_id].already_undone


# -- values stay put unless asked ------------------------------------------


def test_row_values_do_not_leave_the_database_by_default(source, hub):
    """Shipping data off the database it came from is a decision, not a default."""
    make_history(source)
    ship(source, hub, name="billing")

    rows = hub._all("SELECT identity, before_image, after_image FROM ctrlz_hub_changes")
    assert rows
    assert all(
        r["identity"] is None and r["before_image"] is None and r["after_image"] is None
        for r in rows
    )

    # The salary is nowhere in the hub, in any column.
    dump = json.dumps(hub._all("SELECT * FROM ctrlz_hub_changes"), default=str)
    assert "75000" not in dump


def test_values_are_shipped_when_explicitly_requested(source, hub):
    make_history(source)
    ship(source, hub, name="billing", include_values=True)

    dump = json.dumps(hub._all("SELECT * FROM ctrlz_hub_changes"), default=str)
    assert "75000" in dump


def test_what_changed_is_recorded_even_without_values(source, hub):
    """Metadata still answers the audit question: which tables, how much."""
    make_history(source)
    ship(source, hub, name="billing")

    counts = hub._all(
        "SELECT table_name, inserts, updates, deletes FROM ctrlz_hub_operation_tables"
    )
    assert {c["table_name"] for c in counts} == {"users"}
    assert sum(c["updates"] for c in counts) == 1
    assert sum(c["deletes"] for c in counts) == 1


def test_the_dsn_hint_never_carries_a_password(tmp_path, hub):
    """The hub says where an operation happened. It is not a credential store."""
    from ctrlz.hub import _dsn_hint

    class FakeEngine:
        dsn = "postgresql://someone:hunter2@db.internal:5432/app"

    hint = _dsn_hint(FakeEngine())
    assert "hunter2" not in hint
    assert "someone" not in hint
    assert "db.internal" in hint


# -- the hub is a replica, not the record ----------------------------------


def test_the_hub_cannot_undo_anything(hub):
    """Undo is never orchestrated from here (D4.3).

    A hub that could undo would be a hub that could undo *stale* data, with no
    way to know it was stale. The absence of the method is the design.
    """
    for forbidden in ("undo", "redo", "apply", "assess", "execute"):
        assert not hasattr(hub, forbidden), f"Hub must not expose {forbidden}()"


def test_purging_the_hub_leaves_the_source_untouched(source, hub):
    make_history(source)
    ship(source, hub, name="billing")
    assert len(hub.operations()) == 2

    hub.purge()
    assert hub.operations() == []

    # The authoritative history is still exactly where it was.
    assert len(source.log(limit=10)) == 2
    assert source.preview("last").status == "undoable"


def test_an_operation_purged_at_the_source_stays_in_the_hub(source, hub):
    """A replica outliving the original is the point of keeping one."""
    make_history(source)
    ship(source, hub, name="billing")

    source.purge()
    assert source.log(limit=10) == []
    assert len(hub.operations()) == 2


# -- identity and querying -------------------------------------------------


def test_a_source_keeps_one_identity_across_connections(tmp_path):
    """Two databases must be distinguishable, and a DSN will not do it."""
    path = tmp_path / "s.db"
    sqlite3.connect(path).close()

    first = ctrlz.connect(f"sqlite:///{path}")
    first.init()
    identity = first.engine.source_id()
    first.close()

    second = ctrlz.connect(f"sqlite:///{path}")
    assert second.engine.source_id() == identity
    second.close()


def test_two_databases_ship_side_by_side(tmp_path, hub):
    toolkits = []
    for name in ("alpha", "beta"):
        path = tmp_path / f"{name}.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER);"
            "INSERT INTO t VALUES (1, 1);"
        )
        conn.commit()
        conn.close()
        toolkit = ctrlz.connect(f"sqlite:///{path}")
        toolkit.init()
        toolkit.track("t")
        toolkit.run(f"UPDATE t SET v = 2 WHERE id = 1", label=f"{name} change")
        ship(toolkit, hub, name=name)
        toolkits.append(toolkit)

    assert len(hub.sources()) == 2
    assert len(hub.operations()) == 2
    assert {op.source_name for op in hub.operations()} == {"alpha", "beta"}
    assert [op.label for op in hub.operations(source="alpha")] == ["alpha change"]

    for toolkit in toolkits:
        toolkit.close()


def test_operations_can_be_filtered(source, hub, monkeypatch):
    monkeypatch.setenv("CTRLZ_ACTOR", "praveen")
    source.actor = source.actor.__class__.resolve(channel="cli")
    make_history(source)
    source.run("DELETE FROM users", label="wipe", force=True)
    ship(source, hub, name="billing")

    assert len(hub.operations(actor="praveen")) == 3
    assert hub.operations(actor="nobody") == []
    assert len(hub.operations(table="users")) == 3
    assert hub.operations(table="nothing") == []

    risky = hub.operations(min_risk=90)
    assert [op.label for op in risky] == ["wipe"]
    assert risky[0].policy_outcome == "forced"


def test_attribution_survives_the_trip(source, hub, monkeypatch):
    monkeypatch.setenv("CTRLZ_ACTOR", "praveen")
    monkeypatch.setenv("CTRLZ_TICKET", "OPS-42")
    source.actor = source.actor.__class__.resolve(channel="cli")

    source.run("UPDATE users SET salary = 5 WHERE name = 'ada'", label="ticketed")
    ship(source, hub, name="billing")

    operation = hub.operations()[0]
    assert operation.actor == "praveen"
    assert operation.ticket == "OPS-42"
    assert operation.channel == "cli"


def test_retention_trims_the_hub_by_age(source, hub):
    make_history(source)
    ship(source, hub, name="billing")

    assert hub.purge(older_than_seconds=3600) == 0   # nothing is that old yet
    assert len(hub.operations()) == 2

    assert hub.purge(older_than_seconds=0) == 2
    assert hub.operations() == []


# -- a PostgreSQL-backed hub -----------------------------------------------


@pytest.mark.skipif(not PG_DSN, reason="set CTRLZ_TEST_PG_DSN")
def test_the_hub_works_on_postgres_too(source):
    """The same store, on the database a team would actually centralise on."""
    import psycopg2

    admin = psycopg2.connect(PG_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        for table in (
            "ctrlz_hub_changes", "ctrlz_hub_operation_tables",
            "ctrlz_hub_operations", "ctrlz_hub_sources",
        ):
            cur.execute(f"DROP TABLE IF EXISTS {table}")

    store = Hub(PG_DSN)
    try:
        make_history(source)
        result = ship(source, store, name="billing")
        assert result.operations == 2

        shipped = store.operations()
        assert {op.label for op in shipped} == {"raise", "remove bob"}

        # Idempotent here too.
        assert ship(source, store, name="billing").operations == 0
        assert len(store.operations()) == 2
    finally:
        store.close()
        admin.close()
