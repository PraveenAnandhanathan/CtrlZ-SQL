"""MySQL specifics: the gap, its exact boundary, and the types.

The shared behavioural suite already runs against MySQL; five of its tests are
recorded as a known gap in ``conftest.MYSQL_CASCADE_GAP``. What is proved here
is the *shape* of that gap -- that it is confined to tables with cascading
children, and that everything outside it works exactly as it does elsewhere.

A limitation nobody has measured the edges of is indistinguishable from a
limitation that is everywhere.
"""

from __future__ import annotations

import decimal
import os
import uuid

import pytest

import ctrlz
from ctrlz.errors import NotUndoable

MYSQL_DSN = os.environ.get("CTRLZ_TEST_MYSQL_DSN")

pytestmark = pytest.mark.skipif(
    not MYSQL_DSN, reason="set CTRLZ_TEST_MYSQL_DSN to run the MySQL tests"
)


@pytest.fixture
def db(request):
    """A MySQL database whose schema each test defines for itself."""
    import pymysql
    from urllib.parse import urlparse

    parsed = urlparse(MYSQL_DSN)
    name = f"ctrlz_m_{uuid.uuid4().hex[:8]}"
    admin = pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=parsed.password or "",
        autocommit=True,
    )
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")

    dsn = MYSQL_DSN.rstrip("/").rsplit("/", 1)[0] + f"/{name}"
    toolkit = ctrlz.connect(dsn)
    toolkit.admin = admin
    toolkit.db_name = name

    def ddl(*statements: str) -> None:
        with admin.cursor() as cur:
            cur.execute(f"USE {name}")
            for statement in statements:
                cur.execute(statement)

    toolkit.ddl = ddl
    yield toolkit

    toolkit.close()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE {name}")
    admin.close()


def rows(db, table, order="id"):
    import pymysql.cursors

    with db.engine.conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(f"SELECT * FROM {table} ORDER BY {order}")
        return [dict(r) for r in cur.fetchall()]


# -- the boundary of the cascade gap ---------------------------------------


def test_a_delete_on_a_table_with_cascading_children_is_refused(db):
    """The headline finding, asserted rather than described.

    Restoring the parent and silently losing the children would be worse than
    offering no undo at all, so the operation is refused outright.
    """
    db.ddl(
        "CREATE TABLE parent (id int PRIMARY KEY, name varchar(30)) ENGINE=InnoDB",
        "CREATE TABLE child (id int PRIMARY KEY, p int, "
        " FOREIGN KEY (p) REFERENCES parent(id) ON DELETE CASCADE) ENGINE=InnoDB",
        "INSERT INTO parent VALUES (1, 'a')",
        "INSERT INTO child VALUES (10, 1)",
    )
    db.init()
    db.track("parent")
    db.track("child")

    db.run("DELETE FROM parent WHERE id = 1", label="cascade")

    # The child is gone and was never captured.
    assert rows(db, "child") == []
    operation = db.log(limit=1)[0]
    assert operation.tables == ["parent"]

    assessment = db.preview(operation.op_id)
    assert assessment.status == "blocked"
    assert any("cascade" in b.lower() for b in assessment.blockers)

    with pytest.raises(NotUndoable, match="cascade"):
        db.undo(operation.op_id)


def test_the_same_delete_without_a_cascade_is_fully_undoable(db):
    """The gap is the cascade, not the DELETE.

    Identical statement, identical shape, a RESTRICT foreign key instead of a
    cascading one -- and the undo works. That is what bounds the finding.
    """
    db.ddl(
        "CREATE TABLE parent (id int PRIMARY KEY, name varchar(30)) ENGINE=InnoDB",
        "CREATE TABLE child (id int PRIMARY KEY, p int, "
        " FOREIGN KEY (p) REFERENCES parent(id) ON DELETE RESTRICT) ENGINE=InnoDB",
        "INSERT INTO parent VALUES (1, 'a'), (2, 'b')",
        "INSERT INTO child VALUES (10, 1)",
    )
    db.init()
    db.track("parent")
    db.track("child")
    before = rows(db, "parent")

    db.run("DELETE FROM parent WHERE id = 2", label="no cascade here")
    assert len(rows(db, "parent")) == 1

    assert db.preview("last").status == "undoable"
    db.undo("last")
    assert rows(db, "parent") == before


def test_updates_on_a_cascading_parent_are_still_undoable(db):
    """Only the referenced key cascades. Touching other columns is safe."""
    db.ddl(
        "CREATE TABLE parent (id int PRIMARY KEY, name varchar(30)) ENGINE=InnoDB",
        "CREATE TABLE child (id int PRIMARY KEY, p int, "
        " FOREIGN KEY (p) REFERENCES parent(id) ON DELETE CASCADE) ENGINE=InnoDB",
        "INSERT INTO parent VALUES (1, 'a')",
        "INSERT INTO child VALUES (10, 1)",
    )
    db.init()
    db.track("parent")
    db.track("child")
    before = rows(db, "parent")

    db.run("UPDATE parent SET name = 'renamed' WHERE id = 1", label="rename")
    assert db.preview("last").status == "undoable"

    db.undo("last")
    assert rows(db, "parent") == before


def test_a_table_with_no_children_at_all_is_unaffected(db):
    db.ddl(
        "CREATE TABLE notes (id int PRIMARY KEY, body text) ENGINE=InnoDB",
        "INSERT INTO notes VALUES (1, 'keep'), (2, 'also keep')",
    )
    db.init()
    db.track("notes")
    before = rows(db, "notes")

    db.run("DELETE FROM notes WHERE id = 2", label="tidy")
    db.undo("last")
    assert rows(db, "notes") == before


def test_doctor_reports_the_cascade_risk_before_anyone_relies_on_it(db):
    db.ddl(
        "CREATE TABLE parent (id int PRIMARY KEY) ENGINE=InnoDB",
        "CREATE TABLE child (id int PRIMARY KEY, p int, "
        " FOREIGN KEY (p) REFERENCES parent(id) ON DELETE CASCADE) ENGINE=InnoDB",
    )
    db.init()
    db.track("parent")

    risks = db.engine.cascade_risks()
    assert "parent" in risks
    assert risks["parent"][0]["child"] == "child"

    assert any("cascade" in c.lower() for c in db.engine.caveats)


# -- types round-trip ------------------------------------------------------


def test_decimal_json_blob_and_generated_columns_survive_an_undo(db):
    """MySQL's JSON cannot hold binary and its DECIMAL is not a float.

    Both are places where a careless implementation loses data quietly rather
    than loudly, so both are asserted with exact values.
    """
    db.ddl(
        "CREATE TABLE things ("
        "  id int AUTO_INCREMENT PRIMARY KEY,"
        "  name varchar(40) NOT NULL,"
        "  price decimal(18,4),"
        "  payload blob,"
        "  meta json,"
        "  shout varchar(60) GENERATED ALWAYS AS (UPPER(name)) STORED"
        ") ENGINE=InnoDB",
        "INSERT INTO things (name, price, payload, meta) VALUES "
        " ('widget', 12345.6789, x'00ff10', '{\"k\": [1, 2], \"s\": \"x\"}'),"
        " ('plain', NULL, NULL, NULL)",
    )
    db.init()
    db.track("things")
    before = rows(db, "things")
    assert before[0]["price"] == decimal.Decimal("12345.6789")
    assert before[0]["payload"] == b"\x00\xff\x10"

    db.run("DELETE FROM things WHERE name = 'widget'", label="delete")
    db.undo("last")

    after = rows(db, "things")
    assert after == before
    # Precision, not proximity: a float round-trip would lose the last digit.
    assert after[0]["price"] == decimal.Decimal("12345.6789")
    assert after[0]["payload"] == b"\x00\xff\x10"
    assert after[0]["shout"] == "WIDGET"


def test_drift_detection_works_across_json_and_decimal_columns(db):
    """The guard compares stored image to live image.

    Comparing a decoded image column-by-column would report every DECIMAL and
    every JSON column as drifted, and undo would silently match nothing.
    """
    db.ddl(
        "CREATE TABLE things (id int PRIMARY KEY, price decimal(18,4), meta json)"
        " ENGINE=InnoDB",
        "INSERT INTO things VALUES (1, 1.5000, '{\"a\": 1}')",
    )
    db.init()
    db.track("things")

    db.run("UPDATE things SET price = 2.5000 WHERE id = 1", label="bump")
    assessment = db.preview("last")
    assert assessment.status == "undoable", assessment.counts

    db.undo("last")
    assert rows(db, "things")[0]["price"] == decimal.Decimal("1.5000")


# -- MySQL housekeeping ----------------------------------------------------


def test_auto_increment_needs_no_resync_after_a_restore(db):
    """InnoDB advances the counter itself when a row lands with a higher key.

    PostgreSQL needs an explicit setval here; MySQL does not, so the engine
    reports no sequences fixed and the next insert still gets a fresh id.
    """
    db.ddl(
        "CREATE TABLE notes (id int AUTO_INCREMENT PRIMARY KEY, body varchar(20))"
        " ENGINE=InnoDB",
        "INSERT INTO notes (body) VALUES ('one'), ('two'), ('three')",
    )
    db.init()
    db.track("notes")

    db.run("DELETE FROM notes WHERE id >= 2", label="trim")
    result = db.undo("last")
    assert result.sequences_fixed == []

    db.run("INSERT INTO notes (body) VALUES ('four')", label="after restore")
    identifiers = [r["id"] for r in rows(db, "notes")]
    assert len(identifiers) == 4
    assert len(set(identifiers)) == 4


def test_a_stale_tracked_table_does_not_break_an_upgrade(db):
    """A table tracked earlier may have been dropped since."""
    db.ddl(
        "CREATE TABLE temporary_thing (id int PRIMARY KEY) ENGINE=InnoDB",
    )
    db.init()
    db.track("temporary_thing")
    db.ddl("DROP TABLE temporary_thing")

    db.init()  # must not raise
    assert "temporary_thing" not in dict(db.tracked())


def test_operations_run_through_ctrlz_are_grouped_exactly(db):
    """Per-connection grouping is the fallback, not what ctrlz itself does."""
    db.ddl(
        "CREATE TABLE notes (id int PRIMARY KEY, body varchar(20)) ENGINE=InnoDB",
    )
    db.init()
    db.track("notes")

    db.run("INSERT INTO notes VALUES (1, 'a')", label="first")
    db.run("INSERT INTO notes VALUES (2, 'b')", label="second")

    operations = db.log(limit=5)
    assert [op.label for op in operations[:2]] == ["second", "first"]
    assert all(op.row_count == 1 for op in operations[:2])
