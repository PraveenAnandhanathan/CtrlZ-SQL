import os
import uuid

import pytest

import ctrlz

PG_DSN = os.environ.get("CTRLZ_TEST_PG_DSN")
MYSQL_DSN = os.environ.get("CTRLZ_TEST_MYSQL_DSN")


def pytest_report_header(config):
    return (
        f"ctrlz: postgres {'enabled' if PG_DSN else 'skipped'}, "
        f"mysql {'enabled' if MYSQL_DSN else 'skipped'}"
    )


#: Behavioural tests MySQL cannot pass, every one for the same reason: InnoDB
#: performs ON DELETE CASCADE below the trigger layer, so the rows it removes
#: are never captured. ctrlz refuses those operations rather than restoring the
#: parent and silently losing the children.
#:
#: These are xfail(strict=True) on purpose. If MySQL ever starts passing them --
#: because we found a way to see cascaded rows, or because MySQL changed -- the
#: build fails and somebody has to come back and delete this list. An xfail that
#: can quietly become a pass is a lie with a longer shelf life.
MYSQL_CASCADE_GAP = {
    "test_undo_delete_restores_the_rows",
    "test_cascade_delete_is_captured_and_restored",
    "test_restored_rows_do_not_collide_with_new_inserts",
    "test_occupied_identity_is_never_overwritten",
    "test_multi_statement_transaction_is_one_operation",
}

CASCADE_REASON = (
    "MySQL: InnoDB performs foreign-key cascades without firing triggers, so "
    "cascaded rows are never captured and ctrlz refuses the undo. See "
    "spec/tasks-phase3.md."
)


def pytest_collection_modifyitems(config, items):
    """Mark the MySQL cascade gap where it is visible, not in the test bodies.

    The behavioural tests are shared and must stay identical across engines;
    an engine's limitation belongs in one labelled list, not scattered through
    the assertions as conditionals.
    """
    for item in items:
        name = item.originalname or item.name
        if "mysql_db" in item.name and name in MYSQL_CASCADE_GAP:
            item.add_marker(pytest.mark.xfail(strict=True, reason=CASCADE_REASON))


@pytest.fixture
def sqlite_db(tmp_path):
    """A SQLite database with a users/orders schema and ctrlz installed."""
    path = tmp_path / "test.db"
    tk = ctrlz.connect(f"sqlite:///{path}")
    tk.engine.conn.executescript(
        """
        CREATE TABLE users (
            id      INTEGER PRIMARY KEY,
            name    TEXT NOT NULL,
            salary  REAL,
            avatar  BLOB,
            tags    TEXT
        );
        CREATE TABLE orders (
            id      INTEGER PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            total   REAL
        );
        """
    )
    tk.init()
    tk.track("users")
    tk.track("orders")
    yield tk
    tk.close()


@pytest.fixture
def pg_db():
    """A throwaway schema in a real Postgres, with ctrlz installed."""
    if not PG_DSN:
        pytest.skip("set CTRLZ_TEST_PG_DSN to run the Postgres tests")

    import psycopg2

    schema = f"ctrlz_t_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(PG_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(
            f"""
            CREATE TABLE {schema}.users (
                id      int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                name    text NOT NULL,
                salary  numeric,
                tags    text[],
                meta    jsonb,
                shout   text GENERATED ALWAYS AS (upper(name)) STORED
            );
            CREATE TABLE {schema}.orders (
                id      serial PRIMARY KEY,
                user_id int REFERENCES {schema}.users(id) ON DELETE CASCADE,
                total   numeric
            );
            """
        )

    tk = ctrlz.connect(PG_DSN)
    tk.engine.default_schema = schema
    with tk.engine.conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}, public")
    tk.init()
    tk.track(f"{schema}.users")
    tk.track(f"{schema}.orders")
    tk.schema = schema
    yield tk
    tk.close()
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ctrlz CASCADE")
    admin.close()


def rows(tk, table, order="id"):
    """Read a table back, as plain tuples-of-dicts, engine-independently."""
    engine = tk.engine
    if engine.name == "sqlite":
        cur = engine.conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        return [dict(r) for r in cur.fetchall()]
    if engine.name == "mysql":
        import pymysql.cursors

        with engine.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(f"SELECT * FROM {table} ORDER BY {order}")
            return [dict(r) for r in cur.fetchall()]
    import psycopg2.extras

    with engine.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT * FROM {table} ORDER BY {order}")
        return [dict(r) for r in cur.fetchall()]


@pytest.fixture
def mysql_db():
    """A throwaway MySQL database with ctrlz installed.

    The schema deliberately matches the PostgreSQL fixture, cascade and all.
    Giving MySQL an easier schema would hide the one thing this engine exists
    to discover.
    """
    if not MYSQL_DSN:
        pytest.skip("set CTRLZ_TEST_MYSQL_DSN to run the MySQL tests")

    import pymysql
    from urllib.parse import urlparse

    parsed = urlparse(MYSQL_DSN)
    name = f"ctrlz_t_{uuid.uuid4().hex[:8]}"
    admin = pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=parsed.password or "",
        autocommit=True,
    )
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
        cur.execute(f"USE {name}")
        cur.execute(
            "CREATE TABLE users ("
            "  id int AUTO_INCREMENT PRIMARY KEY,"
            "  name varchar(100) NOT NULL,"
            "  salary decimal(12,2),"
            "  tags json,"
            "  meta json,"
            "  shout varchar(120) GENERATED ALWAYS AS (UPPER(name)) STORED"
            ") ENGINE=InnoDB"
        )
        cur.execute(
            "CREATE TABLE orders ("
            "  id int AUTO_INCREMENT PRIMARY KEY,"
            "  user_id int,"
            "  total decimal(12,2),"
            "  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
            ") ENGINE=InnoDB"
        )

    dsn = MYSQL_DSN.rstrip("/").rsplit("/", 1)[0] + f"/{name}"
    tk = ctrlz.connect(dsn)
    tk.init()
    tk.track("users")
    tk.track("orders")
    yield tk
    tk.close()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE {name}")
    admin.close()


def bare_names(names):
    """Strip schema qualification so assertions work on either engine."""
    return {n.rpartition(".")[2] for n in names}


def raw_insert_with_id(tk, table, row_id, name, salary):
    """Insert a row with an explicit primary key, the way a rival writer would.

    Postgres needs OVERRIDING SYSTEM VALUE for a GENERATED ALWAYS identity
    column; SQLite does not.
    """
    if tk.engine.name in ("sqlite", "mysql"):
        sql = f"INSERT INTO {table} (id, name, salary) VALUES ({row_id}, '{name}', {salary})"
    else:
        sql = (
            f"INSERT INTO {table} (id, name, salary) OVERRIDING SYSTEM VALUE "
            f"VALUES ({row_id}, '{name}', {salary})"
        )
    raw_execute(tk, sql)


def raw_execute(tk, sql):
    """Run SQL outside the toolkit, the way another client would."""
    engine = tk.engine
    if engine.name == "sqlite":
        engine.conn.execute(sql)
        return
    with engine.conn.cursor() as cur:
        cur.execute(sql)
