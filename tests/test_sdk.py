"""The SDK wrapper: the same rulebook, applied from inside the application."""

from __future__ import annotations

import os
import sqlite3

import pytest

from ctrlz.errors import PreflightBlocked
from ctrlz.policy import BLOCK, parse as parse_policy
from ctrlz.sdk import GuardedConnection, guard, install_sqlalchemy_guard

PG_DSN = os.environ.get("CTRLZ_TEST_PG_DSN")


@pytest.fixture
def connection(tmp_path):
    conn = sqlite3.connect(tmp_path / "app.db")
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, salary REAL);"
        "INSERT INTO users (name, salary) VALUES ('ada', 75000), ('bob', 50000);"
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def guarded(connection):
    return guard(
        connection, attribute=False, tracked=["users"], dialect="sqlite"
    )


# -- the guard applies -----------------------------------------------------


def test_a_dangerous_statement_is_refused(guarded, connection):
    with pytest.raises(PreflightBlocked) as exc:
        guarded.cursor().execute("DELETE FROM users")
    assert "WHERE" in str(exc.value)

    # Nothing happened.
    assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 2


def test_a_permitted_statement_runs_untouched(guarded, connection):
    cursor = guarded.cursor()
    cursor.execute("UPDATE users SET salary = 1 WHERE name = 'ada'")
    guarded.commit()
    assert connection.execute(
        "SELECT salary FROM users WHERE name = 'ada'"
    ).fetchone()[0] == 1


def test_reads_are_never_in_the_way(guarded):
    rows = guarded.cursor().execute("SELECT name FROM users ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["ada", "bob"]


def test_parameters_are_passed_through_unchanged(guarded):
    cursor = guarded.cursor()
    cursor.execute("SELECT name FROM users WHERE salary > ?", (60000,))
    assert [r[0] for r in cursor.fetchall()] == ["ada"]


def test_executemany_is_guarded_too(guarded):
    with pytest.raises(PreflightBlocked):
        guarded.cursor().executemany("DELETE FROM users", [()])


def test_a_subquery_where_does_not_excuse_an_unfiltered_delete(connection):
    """The statement this whole phase exists for, refused at the other door."""
    guarded = guard(connection, attribute=False, tracked=["users"])
    with pytest.raises(PreflightBlocked, match="WHERE"):
        guarded.cursor().execute(
            "DELETE FROM users USING (SELECT id FROM staging WHERE ok) q"
        )


# -- it is a proxy, not a reimplementation ---------------------------------


def test_unknown_attributes_reach_the_real_connection(guarded, connection):
    assert guarded.total_changes == connection.total_changes
    guarded.execute  # sqlite3's shortcut method, not something we defined
    assert callable(guarded.execute)


def test_cursor_attributes_and_iteration_are_delegated(guarded):
    cursor = guarded.cursor()
    cursor.execute("SELECT name FROM users ORDER BY id")
    assert [row[0] for row in cursor] == ["ada", "bob"]
    assert cursor.description is not None


def test_the_connection_context_manager_still_works(connection):
    guarded = guard(connection, attribute=False, tracked=["users"], dialect="sqlite")
    with guarded as conn:
        conn.cursor().execute("UPDATE users SET salary = 2 WHERE id = 1")
    assert connection.execute("SELECT salary FROM users WHERE id = 1").fetchone()[0] == 2


# -- configuration ---------------------------------------------------------


def test_a_custom_policy_applies(connection):
    policy = parse_policy({
        "version": 1,
        "rules": [{
            "name": "read-only-app",
            "when": {"kind": ["write"]},
            "action": "block",
            "risk": 99,
            "message": "this service does not write",
        }],
    })
    guarded = guard(
        connection, attribute=False, policy=policy, tracked=["users"], dialect="sqlite"
    )
    with pytest.raises(PreflightBlocked, match="does not write"):
        guarded.cursor().execute("UPDATE users SET salary = 1 WHERE id = 1")

    guarded.cursor().execute("SELECT 1")  # reads are still fine


def test_warn_only_mode_is_opt_in_and_off_by_default(guarded, connection):
    """A guardrail that only writes to a log is not a guardrail."""
    assert guarded.warn_only is False

    guarded.warn_only = True
    guarded.cursor().execute("DELETE FROM users")
    guarded.commit()
    assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_the_on_block_hook_sees_the_decision(connection):
    seen = []
    guarded = GuardedConnection(
        connection, tracked=["users"], dialect="sqlite", on_block=seen.append
    )
    with pytest.raises(PreflightBlocked):
        guarded.cursor().execute("DELETE FROM users")

    assert seen and seen[0].decided_by.name == "unfiltered-write"
    assert seen[0].risk == 90


def test_attribution_failure_does_not_break_the_connection(connection):
    """SQLite has no set_config; the wrapper must shrug, not fail."""
    guarded = guard(connection, attribute=True, tracked=["users"], dialect="sqlite")
    assert guarded.cursor().execute("SELECT 1").fetchone()[0] == 1


# -- one evaluator, two doors (D2.6) ---------------------------------------


STATEMENTS = [
    "DELETE FROM users",
    "UPDATE users SET salary = 1",
    "UPDATE users SET salary = 1 WHERE id = 1",
    "DELETE FROM users USING (SELECT id FROM staging WHERE ok) q",
    "TRUNCATE users",
    "SELECT * FROM users",
    "INSERT INTO users (name) VALUES ('x')",
    "DROP TABLE users",
    "UPDATE users SET x = 1 WHERE 1 = 1",
    "not sql at all",
]


@pytest.mark.parametrize("sql", STATEMENTS)
def test_the_sdk_and_the_gateway_agree(sql, connection):
    """If these ever disagree, a rule holds at one door and not the other.

    That is worse than having no rule, because it is invisible: the same
    statement is refused or permitted depending on how the application
    happened to connect.
    """
    from ctrlz.gateway import Interceptor, protocol

    tracked = ("users",)
    sdk = GuardedConnection(connection, tracked=tracked, dialect="postgres")
    gateway = Interceptor(tracked=tracked, dialect="postgres")

    message = protocol.Message(protocol.QUERY, sql.encode() + b"\x00")
    gateway_refused = gateway.inspect(message).refused

    try:
        sdk.check(sql)
        sdk_refused = False
    except PreflightBlocked:
        sdk_refused = True

    assert gateway_refused is sdk_refused, sql


@pytest.mark.parametrize("sql", STATEMENTS)
def test_the_sdk_and_the_cli_agree(sql, tmp_path, connection):
    """Three doors now: CLI, SDK, gateway. All one evaluator."""
    import ctrlz

    toolkit = ctrlz.connect(f"sqlite:///{tmp_path / 'x.db'}")
    try:
        toolkit.init()
        cli_refused = toolkit.check(sql).outcome == BLOCK
    finally:
        toolkit.close()

    guarded = GuardedConnection(connection, tracked=(), dialect="postgres")
    try:
        guarded.check(sql)
        sdk_refused = False
    except PreflightBlocked:
        sdk_refused = True

    assert cli_refused is sdk_refused, sql


# -- SQLAlchemy ------------------------------------------------------------


def test_sqlalchemy_hook_blocks_and_can_be_removed(tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")

    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'sa.db'}")
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE TABLE users (id INTEGER, name TEXT)"))
        conn.execute(sqlalchemy.text("INSERT INTO users VALUES (1, 'ada')"))

    remove = install_sqlalchemy_guard(engine, tracked=["users"], dialect="sqlite")
    try:
        with pytest.raises(Exception) as exc:
            with engine.begin() as conn:
                conn.execute(sqlalchemy.text("DELETE FROM users"))
        assert "WHERE" in str(exc.value)
    finally:
        remove()

    # With the listener gone, behaviour is exactly as before.
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("DELETE FROM users"))
