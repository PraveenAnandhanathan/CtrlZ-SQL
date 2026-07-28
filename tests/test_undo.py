"""End-to-end behaviour, run against every engine that is available.

Each test is parameterised over the fixtures, so SQLite runs everywhere and
Postgres joins in when CTRLZ_TEST_PG_DSN is set.
"""

import pytest

from ctrlz.errors import NotUndoable, PreflightBlocked, UndoConflict
from ctrlz.model import CLEAN, DRIFTED, MISSING, OCCUPIED

from .conftest import bare_names, raw_execute, raw_insert_with_id, rows

ENGINES = ["sqlite_db", "pg_db"]


@pytest.fixture(params=ENGINES)
def db(request):
    return request.getfixturevalue(request.param)


def qualify(db, table):
    schema = getattr(db, "schema", None)
    return f"{schema}.{table}" if schema else table


def seed(db):
    users = qualify(db, "users")
    orders = qualify(db, "orders")
    db.run(
        f"INSERT INTO {users} (name, salary) VALUES ('ada', 75000), ('bob', 50000), "
        f"('cy', 60000)",
        label="seed users",
    )
    ids = [r["id"] for r in rows(db, users)]
    db.run(
        f"INSERT INTO {orders} (user_id, total) VALUES ({ids[0]}, 10), ({ids[1]}, 20)",
        label="seed orders",
    )
    return ids


# -- the basic loop --------------------------------------------------------


def test_undo_update_restores_old_values(db):
    users = qualify(db, "users")
    seed(db)
    before = rows(db, users)

    result = db.run(f"UPDATE {users} SET salary = 90000 WHERE name = 'ada'", label="raise")
    assert result.rowcount == 1
    assert float(rows(db, users)[0]["salary"]) == 90000

    assessment = db.preview("last")
    assert assessment.status == "undoable"
    assert assessment.counts[CLEAN] == 1

    db.undo("last")
    assert rows(db, users) == before


def test_undo_insert_removes_the_rows(db):
    users = qualify(db, "users")
    seed(db)
    before = rows(db, users)

    db.run(f"INSERT INTO {users} (name, salary) VALUES ('dee', 1), ('eve', 2)", label="add")
    assert len(rows(db, users)) == len(before) + 2

    db.undo("last")
    assert rows(db, users) == before


def test_undo_delete_restores_the_rows(db):
    users = qualify(db, "users")
    seed(db)
    before = rows(db, users)

    db.run(f"DELETE FROM {users} WHERE salary < 70000", label="oops")
    assert len(rows(db, users)) == 1

    db.undo("last")
    assert rows(db, users) == before


def test_undo_is_itself_undoable_as_redo(db):
    users = qualify(db, "users")
    seed(db)
    original = rows(db, users)

    db.run(f"UPDATE {users} SET salary = 1 WHERE name = 'ada'", label="mistake")
    changed = rows(db, users)

    db.undo("last")
    assert rows(db, users) == original

    db.redo()
    assert rows(db, users) == changed


# -- the hard parts --------------------------------------------------------


def test_cascade_delete_is_captured_and_restored(db):
    """The child rows are removed by the database, not by the statement.

    Trigger-level capture sees them anyway, which is the whole argument for
    capturing below the SQL layer.
    """
    users = qualify(db, "users")
    orders = qualify(db, "orders")
    seed(db)
    users_before = rows(db, users)
    orders_before = rows(db, orders)

    db.run(f"DELETE FROM {users} WHERE name IN ('ada', 'bob')", label="cascade")
    assert rows(db, orders) == []

    assessment = db.preview("last")
    tables = bare_names(v.change.qualified_name for v in assessment.verdicts)
    assert tables == {"users", "orders"}

    db.undo("last")
    assert rows(db, users) == users_before
    assert rows(db, orders) == orders_before


def test_restored_rows_do_not_collide_with_new_inserts(db):
    """Re-inserting a row with an explicit key must not strand the sequence."""
    users = qualify(db, "users")
    seed(db)

    db.run(f"DELETE FROM {users}", label="wipe", force=True)
    db.undo("last")

    db.run(f"INSERT INTO {users} (name, salary) VALUES ('zoe', 5)", label="after restore")
    names = [r["name"] for r in rows(db, users)]
    assert names.count("zoe") == 1
    assert len(names) == 4


def test_drift_is_detected_and_blocks_by_default(db):
    users = qualify(db, "users")
    seed(db)

    db.run(f"UPDATE {users} SET salary = 90000 WHERE name = 'ada'", label="raise")
    op_id = db.log(limit=1)[0].op_id

    # Somebody else edits the same row afterwards.
    raw_execute(db, f"UPDATE {users} SET salary = 12345 WHERE name = 'ada'")

    assessment = db.preview(op_id)
    assert assessment.status == "conflicts"
    assert assessment.counts[DRIFTED] == 1

    with pytest.raises(UndoConflict):
        db.undo(op_id)

    # Their value survived the refusal.
    assert float(rows(db, users)[0]["salary"]) == 12345


def test_drift_can_be_overridden_explicitly(db):
    users = qualify(db, "users")
    seed(db)

    db.run(f"UPDATE {users} SET salary = 90000 WHERE name = 'ada'", label="raise")
    op_id = db.log(limit=1)[0].op_id
    raw_execute(db, f"UPDATE {users} SET salary = 12345 WHERE name = 'ada'")

    result = db.undo(op_id, allow_conflicts=True)
    assert result.conflicts_overridden == 1
    assert float(rows(db, users)[0]["salary"]) == 75000


def test_already_deleted_rows_are_skipped_not_failed(db):
    users = qualify(db, "users")
    seed(db)

    db.run(f"UPDATE {users} SET salary = 90000 WHERE salary < 70000", label="raise")
    op_id = db.log(limit=1)[0].op_id
    raw_execute(db, f"DELETE FROM {users} WHERE name = 'bob'")

    assessment = db.preview(op_id)
    assert assessment.counts[MISSING] == 1
    assert assessment.status == "undoable"  # a vanished row is not a conflict

    result = db.undo(op_id)
    assert result.skipped == 1
    assert float(rows(db, users)[-1]["salary"]) == 60000


def test_occupied_identity_is_never_overwritten(db):
    users = qualify(db, "users")
    seed(db)
    victim = rows(db, users)[0]["id"]

    db.run(f"DELETE FROM {users} WHERE id = {victim}", label="delete ada")
    op_id = db.log(limit=1)[0].op_id

    # Something else takes that primary key back.
    raw_insert_with_id(db, users, victim, "squatter", 1)

    assessment = db.preview(op_id)
    assert assessment.counts[OCCUPIED] == 1

    result = db.undo(op_id, allow_conflicts=True)
    assert result.skipped == 1
    assert rows(db, users)[0]["name"] == "squatter"


def test_operation_undone_twice_is_refused(db):
    users = qualify(db, "users")
    seed(db)
    db.run(f"UPDATE {users} SET salary = 1 WHERE name = 'ada'", label="x")
    op_id = db.log(limit=1)[0].op_id

    db.undo(op_id)
    with pytest.raises(NotUndoable):
        db.undo(op_id)


def test_multi_statement_transaction_is_one_operation(db):
    users = qualify(db, "users")
    orders = qualify(db, "orders")
    seed(db)
    users_before = rows(db, users)
    orders_before = rows(db, orders)

    db.run(
        f"UPDATE {users} SET salary = 1 WHERE name = 'ada'; "
        f"DELETE FROM {users} WHERE name = 'bob'",
        label="two statements",
    )
    assert len(rows(db, users)) == 2

    op = db.log(limit=1)[0]
    # One update, one deleted user, and the order that cascaded with them.
    assert op.row_count == 3
    assert bare_names(op.tables) == {"users", "orders"}

    db.undo(op.op_id)
    assert rows(db, users) == users_before
    assert rows(db, orders) == orders_before


# -- guardrails ------------------------------------------------------------


def test_missing_where_is_blocked(db):
    users = qualify(db, "users")
    seed(db)
    with pytest.raises(PreflightBlocked):
        db.run(f"DELETE FROM {users}", label="no where")
    assert len(rows(db, users)) == 3


def test_force_overrides_the_guardrail(db):
    users = qualify(db, "users")
    seed(db)
    db.run(f"DELETE FROM {users}", label="no where", force=True)
    assert rows(db, users) == []


def test_dry_run_reports_real_counts_and_changes_nothing(db):
    users = qualify(db, "users")
    seed(db)
    before = rows(db, users)

    result = db.run(f"UPDATE {users} SET salary = 0 WHERE salary > 1", dry_run=True)
    assert result.rowcount == 3
    assert result.committed is False
    assert rows(db, users) == before


def test_row_threshold_prompt_can_reject_the_commit(db):
    users = qualify(db, "users")
    seed(db)
    before = rows(db, users)
    asked = {}

    def refuse(rowcount, sql):
        asked["rowcount"] = rowcount
        return False

    result = db.run(
        f"UPDATE {users} SET salary = 0 WHERE salary > 1",
        confirm_over=2,
        confirm=refuse,
    )
    assert asked["rowcount"] == 3
    assert result.committed is False
    assert rows(db, users) == before


def test_untracked_table_is_reported_as_not_undoable(db):
    users = qualify(db, "users")
    seed(db)
    db.untrack(users)

    result = db.run(f"UPDATE {users} SET salary = 3 WHERE name = 'ada'")
    assert result.op_id is None
    assert any("NOT undoable" in w for w in result.warnings)


# -- history ---------------------------------------------------------------


def test_log_records_labels_and_tables(db):
    users = qualify(db, "users")
    seed(db)
    db.run(f"UPDATE {users} SET salary = 2 WHERE name = 'ada'", label="tweak")

    entries = db.log(limit=5)
    assert entries[0].label == "tweak"
    assert bare_names(entries[0].tables) == {"users"}
    assert entries[0].source == "ctrlz"


def test_purge_drops_history(db):
    users = qualify(db, "users")
    seed(db)
    db.run(f"UPDATE {users} SET salary = 2 WHERE name = 'ada'", label="tweak")
    assert db.log(limit=50)

    db.purge()
    assert db.log(limit=50) == []


def test_doctor_lists_unprotected_tables(db):
    users = qualify(db, "users")
    db.untrack(users)
    info = db.doctor()
    assert any(t.endswith("users") for t in info["untracked"])
