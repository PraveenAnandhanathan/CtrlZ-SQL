"""The gateway, driven by real clients.

A wire-protocol proxy either works against actual clients or it does not work.
These tests drive it with `psql` as a subprocess (simple protocol) and psycopg2
(extended protocol) rather than with a mock, because the failure modes that
matter -- a desynchronised session, a swallowed authentication challenge, a
message split across reads -- are invisible to anything that speaks a
simplified dialect of the protocol.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import uuid

import pytest

from ctrlz.gateway import Gateway, Upstream
from ctrlz.policy import parse as parse_policy

PG_DSN = os.environ.get("CTRLZ_TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not PG_DSN, reason="set CTRLZ_TEST_PG_DSN to run the gateway tests"
)


class RunningGateway:
    """A Gateway on its own event loop in a background thread.

    The gateway is asyncio; the tests are synchronous and drive blocking
    clients. Running it in a thread keeps both honest instead of forcing the
    tests into an async shape they do not need.
    """

    def __init__(self, upstream_dsn: str, **kwargs):
        self.gateway = Gateway(Upstream.from_dsn(upstream_dsn), **kwargs)
        self.port: int = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self.port = self._loop.run_until_complete(
            self.gateway.start("127.0.0.1", 0)
        )
        self._ready.set()
        self._loop.run_forever()

    def __enter__(self) -> "RunningGateway":
        self._thread.start()
        assert self._ready.wait(timeout=10), "gateway did not start"
        return self

    def __exit__(self, *exc) -> None:
        asyncio.run_coroutine_threadsafe(self.gateway.stop(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)


@pytest.fixture
def sandbox():
    """A schema with a tracked table, and ctrlz installed."""
    import ctrlz
    import psycopg2

    schema = f"gw_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(PG_DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(
            f"CREATE TABLE {schema}.users ("
            f"  id serial PRIMARY KEY, name text, salary numeric)"
        )
        cur.execute(
            f"INSERT INTO {schema}.users (name, salary) "
            f"VALUES ('ada', 75000), ('bob', 50000)"
        )

    tk = ctrlz.connect(PG_DSN)
    tk.engine.default_schema = schema
    tk.init()
    tk.track(f"{schema}.users")

    yield SimpleNamespace(dsn=PG_DSN, schema=schema, toolkit=tk, admin=admin)

    tk.close()
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ctrlz CASCADE")
    admin.close()


class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def psql(port: int, *statements: str, database: str = "") -> subprocess.CompletedProcess:
    """Run an unmodified `psql` against the gateway.

    Each statement becomes its own `-c`, which psql sends as a separate simple
    Query message -- the shape needed to check that a session survives a
    refusal rather than that a batch is refused as a unit.
    """
    import urllib.parse

    parsed = urllib.parse.urlparse(PG_DSN)
    env = dict(os.environ, PGPASSWORD=parsed.password or "")
    argv = [
        "psql", "-h", "127.0.0.1", "-p", str(port),
        "-U", parsed.username or "postgres",
        "-d", database or parsed.path.lstrip("/"),
        "-v", "ON_ERROR_STOP=0",
    ]
    for statement in statements:
        argv += ["-c", statement]
    return subprocess.run(
        argv, capture_output=True, text=True, env=env, timeout=30
    )


def connect_through(port: int, **kwargs):
    """psycopg2 through the gateway -- this exercises the extended protocol."""
    import urllib.parse

    import psycopg2

    parsed = urllib.parse.urlparse(PG_DSN)
    return psycopg2.connect(
        host="127.0.0.1", port=port,
        user=parsed.username, password=parsed.password,
        dbname=parsed.path.lstrip("/"), **kwargs,
    )


def _dsn_for(port: int) -> str:
    """The test DSN, re-pointed at the gateway."""
    import urllib.parse

    parsed = urllib.parse.urlparse(PG_DSN)
    return parsed._replace(netloc=f"{parsed.username}:{parsed.password}@127.0.0.1:{port}").geturl()


def _has_psycopg3() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


requires_psql = pytest.mark.skipif(
    shutil.which("psql") is None, reason="psql is not installed"
)

requires_psycopg3 = pytest.mark.skipif(
    not _has_psycopg3(),
    reason="psycopg3 is not installed; it is the client that uses the "
    "extended protocol",
)


# -- an unmodified client works --------------------------------------------


@requires_psql
def test_psql_connects_and_queries_through_the_gateway(sandbox):
    with RunningGateway(PG_DSN, tracked=(f"{sandbox.schema}.users",)) as gw:
        result = psql(gw.port, f"SELECT name FROM {sandbox.schema}.users ORDER BY id")
    assert result.returncode == 0, result.stderr
    assert "ada" in result.stdout and "bob" in result.stdout


def test_psycopg2_works_through_the_gateway(sandbox):
    """psycopg2 interpolates parameters client-side and uses the simple
    protocol, so this covers that path, not the extended one."""
    with RunningGateway(PG_DSN, tracked=(f"{sandbox.schema}.users",)) as gw:
        conn = connect_through(gw.port)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT name FROM {sandbox.schema}.users WHERE salary > %s",
                    (60000,),
                )
                assert [r[0] for r in cur.fetchall()] == ["ada"]
        finally:
            conn.close()


@requires_psycopg3
def test_psycopg3_extended_protocol_works_through_the_gateway(sandbox):
    """psycopg3 sends Parse/Bind/Execute, so this is the real extended path."""
    import psycopg

    with RunningGateway(PG_DSN, tracked=(f"{sandbox.schema}.users",)) as gw:
        with psycopg.connect(_dsn_for(gw.port)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT name FROM {sandbox.schema}.users WHERE salary > %s",
                    (60000,),
                )
                assert [r[0] for r in cur.fetchall()] == ["ada"]


# -- refusal ---------------------------------------------------------------


@requires_psql
def test_a_blocked_statement_comes_back_as_a_native_error(sandbox):
    with RunningGateway(PG_DSN, tracked=(f"{sandbox.schema}.users",)) as gw:
        result = psql(gw.port, f"DELETE FROM {sandbox.schema}.users")
        assert "ctrlz" in result.stderr
        assert "no WHERE clause" in result.stderr

        # The session survived the refusal and the rows are untouched.
        after = psql(gw.port, f"SELECT count(*) FROM {sandbox.schema}.users")
    assert after.returncode == 0
    assert "2" in after.stdout


@requires_psql
def test_the_session_keeps_working_after_a_refusal(sandbox):
    """A refusal must not desynchronise the connection."""
    with RunningGateway(PG_DSN, tracked=(f"{sandbox.schema}.users",)) as gw:
        result = psql(
            gw.port,
            f"DELETE FROM {sandbox.schema}.users",
            f"SELECT count(*) FROM {sandbox.schema}.users",
        )
    assert "ctrlz" in result.stderr
    assert "2" in result.stdout


@requires_psql
def test_a_batch_containing_a_dangerous_statement_is_refused_whole(sandbox):
    """A multi-statement simple query arrives as one message, so it is one
    decision.

    psql sends `a; b;` as a single Query, and the protocol gives us no way to
    let half of it through. Refusing the batch is the only honest option, and
    it is the safe one: the analysis merges a script conservatively, so one
    unfiltered statement makes the whole batch unfiltered.
    """
    with RunningGateway(PG_DSN, tracked=(f"{sandbox.schema}.users",)) as gw:
        result = psql(
            gw.port,
            f"SELECT 1; DELETE FROM {sandbox.schema}.users;",
        )
        assert "ctrlz" in result.stderr

        survived = psql(gw.port, f"SELECT count(*) FROM {sandbox.schema}.users")
    assert "2" in survived.stdout


@requires_psycopg3
def test_blocking_in_the_extended_protocol_leaves_the_session_usable(sandbox):
    """Refusing a Parse must leave the connection in the state a real backend
    would: error until Sync, then ready again."""
    import psycopg

    with RunningGateway(PG_DSN, tracked=(f"{sandbox.schema}.users",)) as gw:
        with psycopg.connect(_dsn_for(gw.port), autocommit=True) as conn:
            with conn.cursor() as cur:
                with pytest.raises(Exception) as exc:
                    cur.execute(f"DELETE FROM {sandbox.schema}.users")
                assert "ctrlz" in str(exc.value)

            # Same connection, still usable.
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {sandbox.schema}.users")
                assert cur.fetchone()[0] == 2


def test_a_permitted_statement_is_not_disturbed(sandbox):
    with RunningGateway(PG_DSN, tracked=(f"{sandbox.schema}.users",)) as gw:
        conn = connect_through(gw.port)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {sandbox.schema}.users SET salary = 1 WHERE name = 'ada'"
                )
                assert cur.rowcount == 1
        finally:
            conn.close()

    with sandbox.admin.cursor() as cur:
        cur.execute(f"SELECT salary FROM {sandbox.schema}.users WHERE name = 'ada'")
        assert float(cur.fetchone()[0]) == 1


# -- fail open (NFR-3) -----------------------------------------------------


def test_an_exception_in_the_interceptor_does_not_block_the_database(sandbox):
    """A bug in the checkpoint must never take the database offline.

    The recorder inside the database is running either way, so a statement we
    failed to judge is still a statement we can undo. Letting it through is
    strictly better than refusing service.
    """
    with RunningGateway(PG_DSN, tracked=(f"{sandbox.schema}.users",)) as gw:
        def explode(*args, **kwargs):
            raise RuntimeError("injected fault")

        gw.gateway.interceptor.engine.evaluate_sql = explode

        conn = connect_through(gw.port)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {sandbox.schema}.users")
                assert cur.fetchone()[0] == 2
                # Even a statement policy would have refused gets through,
                # because we could not evaluate it.
                cur.execute(f"UPDATE {sandbox.schema}.users SET salary = 7 WHERE id > 0")
        finally:
            conn.close()

        assert gw.gateway.interceptor.failed_open > 0


# -- capture is independent of the gateway ---------------------------------


def test_changes_made_through_the_gateway_are_still_captured(sandbox):
    with RunningGateway(PG_DSN, tracked=(f"{sandbox.schema}.users",)) as gw:
        conn = connect_through(gw.port, application_name="reporting-tool")
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {sandbox.schema}.users SET salary = 3 WHERE name = 'bob'"
                )
        finally:
            conn.close()

    op = sandbox.toolkit.log(limit=1)[0]
    assert op.row_count == 1
    assert op.channel == "gateway"
    assert op.actor_app == "reporting-tool"

    sandbox.toolkit.undo(op.op_id)
    with sandbox.admin.cursor() as cur:
        cur.execute(f"SELECT salary FROM {sandbox.schema}.users WHERE name = 'bob'")
        assert float(cur.fetchone()[0]) == 50000


def test_the_gateway_is_not_required_for_capture(sandbox):
    """Stop the gateway; capture carries on. Rule 3, demonstrated."""
    with sandbox.admin.cursor() as cur:
        cur.execute(f"UPDATE {sandbox.schema}.users SET salary = 11 WHERE name = 'ada'")

    op = sandbox.toolkit.log(limit=1)[0]
    assert op.row_count == 1
    assert sandbox.toolkit.preview(op.op_id).status == "undoable"


# -- policy comes from the same evaluator (D2.6) ---------------------------


def test_the_gateway_honours_a_custom_policy(sandbox):
    policy = parse_policy({
        "version": 1,
        "rules": [{
            "name": "no-select-star",
            "when": {"tables": ["users"], "kind": ["read"]},
            "action": "block",
            "risk": 10,
            "message": "reads of users go through the reporting replica",
        }],
    })
    with RunningGateway(
        PG_DSN, policy=policy, tracked=(f"{sandbox.schema}.users",)
    ) as gw:
        conn = connect_through(gw.port)
        try:
            with conn.cursor() as cur:
                with pytest.raises(Exception) as exc:
                    cur.execute(f"SELECT * FROM {sandbox.schema}.users")
                assert "reporting replica" in str(exc.value)
        finally:
            conn.close()


def test_gateway_and_toolkit_reach_the_same_verdict(sandbox):
    """One evaluator, two doors (D2.6).

    If these ever disagree, a rule holds in one place and not the other, which
    is worse than having no rule.
    """
    from ctrlz.policy import BLOCK

    statements = [
        f"DELETE FROM {sandbox.schema}.users",
        f"UPDATE {sandbox.schema}.users SET salary = 1 WHERE id = 1",
        f"TRUNCATE {sandbox.schema}.users",
        "SELECT 1",
    ]
    gateway = Gateway(Upstream.from_dsn(PG_DSN), tracked=(f"{sandbox.schema}.users",))

    for sql in statements:
        from ctrlz.gateway import protocol

        message = protocol.Message(protocol.QUERY, sql.encode() + b"\x00")
        verdict = gateway.interceptor.inspect(message)
        via_toolkit = sandbox.toolkit.check(sql)

        assert verdict.refused is (via_toolkit.outcome == BLOCK), sql
