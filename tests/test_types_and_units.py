"""Type round-tripping, capture limits, and the pure-Python helpers."""

import pytest

from ctrlz import preflight
from ctrlz.model import CLEAN, DELETE, INSERT, UPDATE, Change, RowVerdict
from ctrlz.ordering import order_verdicts, topological_rank

from .conftest import raw_execute, rows


# -- values survive the round trip -----------------------------------------


def test_sqlite_restores_blobs_and_nulls(sqlite_db):
    db = sqlite_db
    db.run(
        "INSERT INTO users (name, salary, avatar, tags) "
        "VALUES ('ada', 75000.5, x'deadbeef', NULL)",
        label="seed",
    )
    before = rows(db, "users")
    assert before[0]["avatar"] == b"\xde\xad\xbe\xef"

    db.run("DELETE FROM users WHERE name = 'ada'", label="delete")
    db.undo("last")

    after = rows(db, "users")
    assert after == before
    assert after[0]["avatar"] == b"\xde\xad\xbe\xef"
    assert after[0]["tags"] is None


def test_postgres_restores_arrays_json_and_generated_columns(pg_db):
    db = pg_db
    users = f"{db.schema}.users"
    db.run(
        f"INSERT INTO {users} (name, salary, tags, meta) "
        f"VALUES ('ada', 75000.25, '{{x,y}}', '{{\"k\": [1, 2]}}')",
        label="seed",
    )
    before = rows(db, users)
    assert before[0]["tags"] == ["x", "y"]
    assert before[0]["meta"] == {"k": [1, 2]}
    assert before[0]["shout"] == "ADA"

    db.run(f"DELETE FROM {users} WHERE name = 'ada'", label="delete")
    db.undo("last")

    after = rows(db, users)
    assert after == before


def test_update_touching_only_some_columns_reverts_only_those(pg_db):
    db = pg_db
    users = f"{db.schema}.users"
    db.run(f"INSERT INTO {users} (name, salary, tags) VALUES ('ada', 1, '{{a}}')")
    db.run(f"UPDATE {users} SET salary = 2 WHERE name = 'ada'", label="bump")

    assessment = db.preview("last")
    change = assessment.verdicts[0].change
    assert change.before["salary"] != change.after["salary"]
    assert change.before["tags"] == change.after["tags"]

    db.undo("last")
    assert rows(db, users)[0]["tags"] == ["a"]
    assert float(rows(db, users)[0]["salary"]) == 1


# -- capture limits are reported, never silently applied -------------------


@pytest.mark.parametrize("fixture", ["sqlite_db", "pg_db"])
def test_operation_over_the_capture_limit_is_refused(request, fixture):
    db = request.getfixturevalue(fixture)
    schema = getattr(db, "schema", None)
    users = f"{schema}.users" if schema else "users"
    settings = "ctrlz.settings" if schema else "ctrlz_settings"

    # Squeeze the limit down rather than inserting a hundred thousand rows.
    if db.engine.name == "sqlite":
        db.engine.conn.execute(
            f"UPDATE {settings} SET value = '2' WHERE key = 'max_rows_per_operation'"
        )
    else:
        with db.engine.conn.cursor() as cur:
            cur.execute(
                f"UPDATE {settings} SET value = '2' WHERE key = 'max_rows_per_operation'"
            )

    result = db.run(
        f"INSERT INTO {users} (name, salary) VALUES ('a', 1), ('b', 2), ('c', 3)",
        label="too big",
    )
    assert any("NOT undoable" in w for w in result.warnings)

    op = db.log(limit=1)[0]
    assert op.capped is True

    assessment = db.preview(op.op_id)
    assert assessment.status == "blocked"
    assert any("capture limit" in b for b in assessment.blockers)


# -- an operation that replaces a row over the same key --------------------


@pytest.mark.parametrize("fixture", ["sqlite_db", "pg_db"])
def test_delete_and_reinsert_same_key_in_one_operation(request, fixture):
    """A delete + insert over the same identity is a replace, not a conflict."""
    db = request.getfixturevalue(fixture)
    schema = getattr(db, "schema", None)
    users = f"{schema}.users" if schema else "users"
    if db.engine.name == "sqlite":
        insert = "INSERT INTO {t} (id, name, salary) VALUES (1, 'new', 2)"
    else:
        insert = (
            "INSERT INTO {t} (id, name, salary) OVERRIDING SYSTEM VALUE "
            "VALUES (1, 'new', 2)"
        )

    db.run(
        (
            "INSERT INTO {t} (id, name, salary) VALUES (1, 'old', 1)"
            if db.engine.name == "sqlite"
            else "INSERT INTO {t} (id, name, salary) OVERRIDING SYSTEM VALUE "
                 "VALUES (1, 'old', 1)"
        ).format(t=users),
        label="seed",
    )
    before = rows(db, users)

    db.run(
        f"DELETE FROM {users} WHERE id = 1; " + insert.format(t=users),
        label="replace",
    )
    assert rows(db, users)[0]["name"] == "new"

    assessment = db.preview("last")
    assert assessment.status == "undoable", assessment.counts

    db.undo("last")
    assert rows(db, users) == before


# -- guardrail heuristics --------------------------------------------------


@pytest.mark.parametrize(
    "sql,blocked",
    [
        ("DELETE FROM users", True),
        ("DELETE FROM users WHERE id = 1", False),
        ("UPDATE users SET x = 1", True),
        ("UPDATE users SET x = 1 WHERE id = 2", False),
        ("TRUNCATE users", True),
        ("SELECT * FROM users", False),
        # A WHERE hiding inside a string literal must not count as a filter.
        ("DELETE FROM logs -- WHERE id = 1", True),
        ("UPDATE t SET msg = 'no WHERE here'", True),
        ("UPDATE t SET msg = 'x' WHERE id = 1", False),
    ],
)
def test_preflight_blocks_unfiltered_writes(sql, blocked):
    assert preflight.inspect(sql).blocked is blocked


def test_preflight_warns_about_untracked_tables():
    checks = preflight.inspect("UPDATE payments SET x = 1 WHERE id = 2", tracked={"public.users"})
    assert any("payments" in w for w in checks.warnings)

    checks = preflight.inspect("UPDATE users SET x = 1 WHERE id = 2", tracked={"public.users"})
    assert not checks.warnings


def test_preflight_warns_that_ddl_is_not_captured():
    checks = preflight.inspect("ALTER TABLE users DROP COLUMN salary")
    assert any("cannot undo" in w or "not captured" in w for w in checks.warnings)


# -- ordering --------------------------------------------------------------


def test_topological_rank_puts_parents_first():
    rank = topological_rank(
        ["users", "orders", "items"],
        [("users", "orders"), ("orders", "items")],
    )
    assert rank["users"] < rank["orders"] < rank["items"]


def test_topological_rank_tolerates_cycles():
    rank = topological_rank(["a", "b"], [("a", "b"), ("b", "a")])
    assert set(rank) == {"a", "b"}


def _verdict(seq, table, action):
    change = Change(
        seq=seq,
        op_id="x",
        table_schema="",
        table_name=table,
        action=action,
        identity={"id": seq},
        before=None,
        after=None,
    )
    return RowVerdict(change=change, status=CLEAN)


def test_inverse_order_deletes_children_first_then_restores_parents_first():
    rank = {"users": 0, "orders": 1}
    verdicts = [
        _verdict(1, "users", DELETE),   # restore parent
        _verdict(2, "orders", DELETE),  # restore child
        _verdict(3, "users", INSERT),   # remove parent
        _verdict(4, "orders", INSERT),  # remove child
        _verdict(5, "users", UPDATE),
    ]
    ordered = order_verdicts(verdicts, rank, key=lambda v: v.change.table_name)
    sequence = [(v.change.table_name, v.change.action) for v in ordered]

    assert sequence == [
        ("orders", INSERT),  # remove the child row first
        ("users", INSERT),   # then the parent it pointed at
        ("users", DELETE),   # restore the parent before
        ("orders", DELETE),  # the child that references it
        ("users", UPDATE),   # updates last
    ]


# -- the gap between "looks clean" and "apply" -----------------------------


@pytest.mark.parametrize("fixture", ["sqlite_db", "pg_db"])
def test_row_changing_after_assessment_is_not_clobbered(request, fixture, monkeypatch):
    """The drift check is enforced in the UPDATE itself, not just beforehand.

    Assessment and apply cannot be one atomic step, so every inverse carries a
    guard asserting the row still holds the values we wrote. Here the row is
    changed in between, and the guard has to catch it.
    """
    db = request.getfixturevalue(fixture)
    schema = getattr(db, "schema", None)
    users = f"{schema}.users" if schema else "users"

    db.run(f"INSERT INTO {users} (name, salary) VALUES ('ada', 1)", label="seed")
    db.run(f"UPDATE {users} SET salary = 2 WHERE name = 'ada'", label="bump")
    op_id = db.log(limit=1)[0].op_id

    real_assess = db.engine.assess

    def assess_then_meddle(target_op):
        assessment = real_assess(target_op)
        # Somebody commits a change after we decided the row was clean.
        raw_execute(db, f"UPDATE {users} SET salary = 99 WHERE name = 'ada'")
        return assessment

    monkeypatch.setattr(db.engine, "assess", assess_then_meddle)

    result = db.engine.undo(op_id)
    assert result.applied == 0
    assert float(rows(db, users)[0]["salary"]) == 99
