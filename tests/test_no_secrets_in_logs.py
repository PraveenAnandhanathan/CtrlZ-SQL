"""NFR-5: no credentials, row values, or connection strings in logs.

The specification claimed this and listed "test asserts redaction" as how it was
measured. There was no such test. This is it.

The property matters more here than in most tools. ctrlz sits in the path of
every statement, and a statement is not metadata: ``INSERT INTO patients (ssn)
VALUES ('123-45-6789')`` *is* the row value. Anything that logs SQL verbatim
logs the data it was protecting, into a file with different permissions, a
different retention policy, and quite possibly a different country.

The approach is deliberately empirical. Reading the log calls and reasoning
about them is how you miss the one that formats an exception whose message
happens to carry the statement. So instead: run the real paths with marked
secrets in them, capture everything the logger emits at DEBUG, and assert the
markers are absent.
"""

from __future__ import annotations

import logging

import pytest

import ctrlz

#: Distinctive strings planted in the places a leak would come from. Each is
#: unlikely enough that finding one in log output is proof, not coincidence.
PASSWORD = "hunter2SECRETpw"
SSN = "123-45-6789-MARKER"
SALARY = "987654321"
TABLE = "patients_marker"

SECRETS = (PASSWORD, SSN, SALARY)


class Captured(logging.Handler):
    """Everything ctrlz logs, fully formatted, including tracebacks."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.text.append(self.format(record))
        except Exception:                      # noqa: BLE001
            self.text.append(f"<unformattable: {record.msg!r}>")

    @property
    def everything(self) -> str:
        return "\n".join(self.text)


@pytest.fixture
def logs():
    """Capture the ctrlz logger tree at DEBUG, then put it back."""
    handler = Captured()
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    logger = logging.getLogger("ctrlz")
    previous_level, previous_propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def assert_no_secrets(logs: Captured) -> None:
    for secret in SECRETS:
        assert secret not in logs.everything, (
            f"{secret!r} reached the logs:\n{logs.everything[:2000]}"
        )


# -- row values --------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    import sqlite3

    path = tmp_path / "secrets.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        f"CREATE TABLE {TABLE} (id INTEGER PRIMARY KEY, ssn TEXT, salary REAL);"
    )
    conn.commit()
    conn.close()

    tk = ctrlz.connect(f"sqlite:///{path}")
    tk.init()
    tk.track(TABLE)
    yield tk
    tk.close()


def test_the_ordinary_loop_logs_no_row_values(db, logs):
    """Insert, update, undo. None of the values may appear."""
    db.run(f"INSERT INTO {TABLE} (id, ssn, salary) VALUES (1, '{SSN}', {SALARY})")
    db.run(f"UPDATE {TABLE} SET salary = 1 WHERE ssn = '{SSN}'")
    db.preview("last")
    db.undo("last")
    assert_no_secrets(logs)


def test_a_blocked_statement_does_not_log_the_values_it_carried(db, logs):
    """The refusal path is the one most tempted to quote the statement."""
    from ctrlz.errors import PreflightBlocked

    with pytest.raises(PreflightBlocked):
        db.run(f"UPDATE {TABLE} SET ssn = '{SSN}'")      # no WHERE: blocked
    assert_no_secrets(logs)


def test_a_forced_statement_does_not_log_the_values_it_carried(db, logs):
    db.run(f"INSERT INTO {TABLE} (id, ssn, salary) VALUES (2, '{SSN}', {SALARY})")
    db.run(f"UPDATE {TABLE} SET salary = {SALARY}", force=True)
    assert_no_secrets(logs)


def test_an_analysis_failure_does_not_log_the_statement(db, logs, monkeypatch):
    """Analysis degrades to a fallback and logs that it did.

    The statement is exactly what a debug line is tempted to include, and it is
    the whole row value for an INSERT.
    """
    from ctrlz.analysis import analyze

    analyze(f"INSERT INTO {TABLE} (ssn) VALUES ('{SSN}') ((((", prefer="sqlglot")
    analyze(f"NOT SQL AT ALL '{SSN}'")
    assert_no_secrets(logs)


# -- credentials and connection strings --------------------------------------


def test_a_failed_connection_does_not_log_the_password(logs):
    """The DSN carries the password, and connection failure is a logged path."""
    from ctrlz.errors import CtrlzError

    with pytest.raises((CtrlzError, Exception)):
        ctrlz.connect(
            f"postgresql://user:{PASSWORD}@127.0.0.1:1/nonexistent_marker"
        ).init()
    assert_no_secrets(logs)


def test_the_gateway_startup_logs_no_upstream_password(logs):
    """`ctrlz gateway` prints where it is proxying to on every start."""
    from ctrlz.gateway import Gateway, Upstream

    gateway = Gateway(Upstream.from_dsn(f"postgresql://ada:{PASSWORD}@db.internal/app"))
    import asyncio

    async def start_and_stop():
        await gateway.start("127.0.0.1", 0)
        await gateway.stop()

    asyncio.run(start_and_stop())
    assert_no_secrets(logs)


def test_the_engine_never_describes_itself_with_a_password():
    """describe_target feeds `init`, `doctor` and the hub's source list."""
    from types import SimpleNamespace

    from ctrlz.engines.base import Engine

    described = Engine.describe_target(
        SimpleNamespace(dsn=f"postgresql://ada:{PASSWORD}@db:5432/app", path="")
    )
    assert PASSWORD not in described
    assert described == "postgresql://db:5432/app"


def test_doctor_output_carries_no_password(tmp_path, capsys):
    """`doctor` output is what people paste into bug reports."""
    from ctrlz.cli import main

    path = tmp_path / "d.db"
    main(["--dsn", f"sqlite:///{path}", "init"])
    capsys.readouterr()
    main(["--dsn", f"sqlite:///{path}", "doctor"])
    assert PASSWORD not in capsys.readouterr().out


# -- the gateway's hot path --------------------------------------------------


def test_the_gateway_refusal_log_names_the_rule_not_the_statement(logs):
    """A refusal is logged, and it must identify *what* was refused without
    quoting the statement -- which for an INSERT is the row itself."""
    from ctrlz.gateway import Interceptor, protocol

    interceptor = Interceptor(tracked=(TABLE,))
    verdict = interceptor.inspect(
        protocol.Message(
            protocol.QUERY,
            f"UPDATE {TABLE} SET ssn = '{SSN}'\x00".encode(),
        )
    )
    # Whether it refuses depends on the shipped policy; either way, nothing it
    # logged may carry the value.
    assert verdict is not None
    assert_no_secrets(logs)


def test_a_failure_inside_the_interceptor_does_not_log_the_statement(logs, monkeypatch):
    """The fail-open path must not render an exception that quotes the SQL.

    The first version of this test raised an error whose message did not
    contain the statement, so it would have passed against the leaking code it
    was written to catch. The exception here embeds the statement deliberately,
    which is what a real parser error does -- sqlglot's ParseError renders the
    offending line with a caret under it.
    """
    from ctrlz.gateway import Interceptor, protocol

    interceptor = Interceptor(tracked=(TABLE,))
    statement = f"UPDATE {TABLE} SET ssn = '{SSN}'"

    def explode(sql, *args, **kwargs):
        raise RuntimeError(f"could not parse: {sql}")      # quotes the statement

    monkeypatch.setattr(interceptor.engine, "evaluate_sql", explode)
    verdict = interceptor.inspect(
        protocol.Message(protocol.QUERY, statement.encode() + b"\x00")
    )
    assert not verdict.refused, "fail open"
    assert interceptor.failed_open == 1
    assert_no_secrets(logs)


def test_an_unreadable_message_does_not_log_its_bytes(logs, monkeypatch):
    """The other fail-open path. A decoding error quotes the bytes it choked
    on, and those bytes are the statement."""
    from ctrlz.gateway import Interceptor, protocol

    interceptor = Interceptor(tracked=(TABLE,))
    statement = f"UPDATE {TABLE} SET ssn = '{SSN}'"

    def explode(message):
        raise ValueError(f"bad bytes in {statement}")

    monkeypatch.setattr(protocol, "statement_of", explode)
    verdict = interceptor.inspect(
        protocol.Message(protocol.QUERY, statement.encode() + b"\x00")
    )
    assert not verdict.refused
    assert interceptor.failed_open == 1
    assert_no_secrets(logs)


def test_the_analysis_fallback_note_names_the_type_not_the_message(logs):
    """What the user is told when a backend fails, checked for shape.

    Losing the parser's own words costs a little diagnostic detail. It buys the
    guarantee that turning on debug logging cannot dump the data ctrlz exists
    to protect, which is the better trade for a tool in this position.
    """
    from ctrlz.analysis import analyze

    result = analyze(f"INSERT INTO {TABLE} (ssn) VALUES ('{SSN}') ((((")
    notes = " ".join(result.notes)
    assert SSN not in notes
    assert "could not parse" in notes or result.backend != "none"
    assert_no_secrets(logs)


# -- the guard on the guard --------------------------------------------------


def test_the_capture_would_catch_a_leak():
    """If the handler silently captured nothing, every test above would pass.

    Deliberately logging a secret must be detected, or this file is decoration.
    """
    handler = Captured()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("ctrlz.leaktest")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.debug("a statement containing %s", SSN)
        assert SSN in handler.everything
        with pytest.raises(AssertionError):
            assert_no_secrets(handler)
    finally:
        logger.removeHandler(handler)


def test_the_capture_includes_tracebacks():
    """log.exception is used in the fail-open paths; if the handler dropped
    traceback text, those tests would prove nothing."""
    handler = Captured()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("ctrlz.leaktest")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        try:
            raise ValueError(f"boom {SSN}")
        except ValueError:
            logger.exception("something failed")
        assert SSN in handler.everything, "tracebacks are not being captured"
    finally:
        logger.removeHandler(handler)
