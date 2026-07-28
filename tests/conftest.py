import os
import uuid

import pytest

import ctrlz

PG_DSN = os.environ.get("CTRLZ_TEST_PG_DSN")


def pytest_report_header(config):
    return f"ctrlz: postgres tests {'enabled' if PG_DSN else 'skipped (set CTRLZ_TEST_PG_DSN)'}"


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
    import psycopg2.extras

    with engine.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT * FROM {table} ORDER BY {order}")
        return [dict(r) for r in cur.fetchall()]


def bare_names(names):
    """Strip schema qualification so assertions work on either engine."""
    return {n.rpartition(".")[2] for n in names}


def raw_insert_with_id(tk, table, row_id, name, salary):
    """Insert a row with an explicit primary key, the way a rival writer would.

    Postgres needs OVERRIDING SYSTEM VALUE for a GENERATED ALWAYS identity
    column; SQLite does not.
    """
    if tk.engine.name == "sqlite":
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
