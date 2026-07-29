"""Attribution: who made each change, and what the guardrails thought."""

from __future__ import annotations

import pytest

import ctrlz
from ctrlz.actor import UNKNOWN, Actor

from .conftest import rows

ENGINES = ["sqlite_db", "pg_db"]


@pytest.fixture(params=ENGINES)
def db(request):
    return request.getfixturevalue(request.param)


def qualify(db, table):
    schema = getattr(db, "schema", None)
    return f"{schema}.{table}" if schema else table


# -- resolving an actor ----------------------------------------------------


def test_actor_resolves_without_any_environment(monkeypatch):
    for name in ("CTRLZ_ACTOR", "CTRLZ_TICKET", "CTRLZ_HOST", "CTRLZ_APPLICATION"):
        monkeypatch.delenv(name, raising=False)
    actor = Actor.resolve()
    assert actor.user and actor.user != ""
    assert actor.host and actor.host != ""
    assert actor.ticket is None


def test_environment_overrides_are_used(monkeypatch):
    monkeypatch.setenv("CTRLZ_ACTOR", "praveen")
    monkeypatch.setenv("CTRLZ_TICKET", "OPS-1234")
    monkeypatch.setenv("CTRLZ_HOST", "laptop")
    actor = Actor.resolve(channel="cli")
    assert (actor.user, actor.ticket, actor.host) == ("praveen", "OPS-1234", "laptop")
    assert actor.describe() == "praveen@laptop (OPS-1234)"


def test_resolution_survives_a_host_with_no_identity(monkeypatch):
    """A stripped container has no passwd entry and no hostname.

    That is not a reason to refuse to record a change.
    """
    monkeypatch.delenv("CTRLZ_ACTOR", raising=False)
    monkeypatch.delenv("CTRLZ_HOST", raising=False)
    monkeypatch.setattr("ctrlz.actor.getpass.getuser", _boom)
    monkeypatch.setattr("ctrlz.actor.socket.gethostname", _boom)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)

    actor = Actor.resolve()
    assert actor.user == UNKNOWN
    assert actor.host == UNKNOWN


def _boom(*args, **kwargs):
    raise OSError("no identity on this host")


def test_values_are_bounded_and_single_line(monkeypatch):
    monkeypatch.setenv("CTRLZ_ACTOR", "a" * 5000)
    monkeypatch.setenv("CTRLZ_TICKET", "OPS-1\nDROP TABLE users")
    actor = Actor.resolve()
    assert len(actor.user) <= 200
    assert "\n" not in actor.ticket


# -- attribution reaches the database --------------------------------------


def test_actor_is_recorded_on_the_operation(db, monkeypatch):
    users = qualify(db, "users")
    monkeypatch.setenv("CTRLZ_ACTOR", "praveen")
    monkeypatch.setenv("CTRLZ_TICKET", "OPS-77")
    db.actor = Actor.resolve(channel="cli")

    db.run(f"INSERT INTO {users} (name, salary) VALUES ('ada', 1)", label="seed")

    op = db.log(limit=1)[0]
    assert op.actor_user == "praveen"
    assert op.ticket == "OPS-77"
    assert op.channel == "cli"
    assert op.who == "praveen"
    assert op.actor_host


def test_policy_verdict_is_recorded_with_the_operation(db):
    users = qualify(db, "users")
    db.run(f"INSERT INTO {users} (name, salary) VALUES ('ada', 1)")

    op = db.log(limit=1)[0]
    assert op.policy_outcome == "allow"
    assert op.risk == 0


def test_a_forced_override_is_recorded_as_forced(db):
    """The history must show what happened, not what we would have preferred."""
    users = qualify(db, "users")
    db.run(f"INSERT INTO {users} (name, salary) VALUES ('ada', 1)")
    db.run(f"DELETE FROM {users}", force=True, label="wipe")

    op = db.log(limit=1)[0]
    assert op.policy_outcome == "forced"
    assert op.risk >= 90


def test_a_warned_operation_records_the_warning_and_its_risk(db):
    users = qualify(db, "users")
    db.run(f"INSERT INTO {users} (name, salary) VALUES ('ada', 1)")
    result = db.run(f"UPDATE {users} SET salary = 2 WHERE 1 = 1", label="tautology")

    assert result.committed is True
    op = db.log(limit=1)[0]
    assert op.policy_outcome == "warn"
    assert op.risk == 60


def test_changes_from_outside_ctrlz_have_no_actor(db):
    """A NULL actor is the honest answer, not a guess.

    ctrlz still captures the change -- capture does not depend on the tool
    being in the connection path -- but it does not know who made it.
    """
    from .conftest import raw_execute

    users = qualify(db, "users")
    db.run(f"INSERT INTO {users} (name, salary) VALUES ('ada', 1)")
    raw_execute(db, f"UPDATE {users} SET salary = 99 WHERE name = 'ada'")

    op = db.log(limit=1)[0]
    assert op.source == "external"
    assert op.actor_user is None
    assert op.who != ""     # falls back to the database role


def test_attribution_does_not_disturb_undo(db, monkeypatch):
    monkeypatch.setenv("CTRLZ_ACTOR", "praveen")
    db.actor = Actor.resolve(channel="cli")

    users = qualify(db, "users")
    db.run(f"INSERT INTO {users} (name, salary) VALUES ('ada', 75000)", label="seed")
    before = rows(db, users)

    db.run(f"UPDATE {users} SET salary = 1 WHERE name = 'ada'", label="oops")
    db.undo("last")

    assert rows(db, users) == before
    # The undo is itself attributed.
    undo_op = db.log(limit=1)[0]
    assert undo_op.is_undo


# -- the doctor reports what it knows ---------------------------------------


def test_doctor_reports_policy_and_actor(db):
    info = db.doctor()
    assert info["policy_rules"] > 0
    assert info["block_on_risk"] is False
    assert info["schema_version"] == 2
    assert "@" in info["actor"]
