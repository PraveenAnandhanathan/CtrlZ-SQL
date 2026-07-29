"""The command line surface, driven the way a user drives it."""

from __future__ import annotations

import json

import pytest

from ctrlz.cli import EXIT_ALLOW, EXIT_BLOCK, EXIT_WARN, main


@pytest.fixture
def db_url(tmp_path, capsys):
    """A SQLite database with ctrlz installed and a table tracked."""
    import sqlite3

    path = tmp_path / "cli.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, salary REAL);"
        "INSERT INTO users (name, salary) VALUES ('ada', 75000), ('bob', 50000);"
    )
    conn.commit()
    conn.close()

    url = f"sqlite:///{path}"
    assert main(["--dsn", url, "init"]) == 0
    assert main(["--dsn", url, "track", "users"]) == 0
    # Drain the setup chatter, so a test reading stdout sees only its own.
    capsys.readouterr()
    return url


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys, *argv):
    code, out, err = run(capsys, *argv)
    return code, json.loads(out), err


# -- check -----------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,expected",
    [
        ("DELETE FROM users", EXIT_BLOCK),
        ("TRUNCATE users", EXIT_BLOCK),
        ("DROP TABLE users", EXIT_WARN),
        ("UPDATE users SET salary = 1 WHERE id = 1", EXIT_ALLOW),
        ("SELECT * FROM users", EXIT_ALLOW),
    ],
)
def test_check_exit_codes_let_scripts_act_without_parsing_text(
    capsys, db_url, sql, expected
):
    code, _out, _err = run(capsys, "--dsn", db_url, "check", sql)
    assert code == expected


def test_check_reports_the_rule_and_the_risk(capsys, db_url):
    code, out, _ = run(capsys, "--dsn", db_url, "check", "DELETE FROM users")
    assert code == EXIT_BLOCK
    assert "unfiltered-write" in out
    assert "BLOCKED" in out
    assert "90" in out


def test_check_json_carries_the_analysis(capsys, db_url):
    code, payload, _ = run_json(
        capsys, "--dsn", db_url, "--json", "check",
        "DELETE FROM users USING (SELECT id FROM other WHERE ok) q",
    )
    assert code == EXIT_BLOCK
    assert payload["outcome"] == "block"
    assert payload["decided_by"] == "unfiltered-write"
    # The nested WHERE belongs to the subquery, not the delete.
    assert payload["analysis"]["has_filter"] is False
    assert payload["analysis"]["written_tables"] == ["users"]


def test_check_explain_names_how_the_verdict_was_reached(capsys, db_url):
    _code, out, _ = run(capsys, "--dsn", db_url, "check", "DELETE FROM users", "--explain")
    assert "decided by rule 'unfiltered-write'" in out


# -- policy ----------------------------------------------------------------


def test_policy_show_lists_the_effective_rules(capsys, db_url):
    code, out, _ = run(capsys, "--dsn", db_url, "policy", "show")
    assert code == 0
    assert "unfiltered-write" in out
    assert "block on risk: no" in out


def test_policy_test_evaluates_one_statement(capsys, db_url):
    code, out, _ = run(capsys, "--dsn", db_url, "policy", "test", "DELETE FROM users")
    assert code == EXIT_BLOCK
    assert "unfiltered-write" in out


def test_policy_path_reports_the_built_in_defaults(capsys, db_url, monkeypatch, tmp_path):
    monkeypatch.delenv("CTRLZ_POLICY", raising=False)
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, "--dsn", db_url, "policy", "path")
    assert code == 0
    assert "built-in defaults" in out


def test_a_custom_policy_changes_behaviour_with_no_code_change(
    capsys, db_url, tmp_path, monkeypatch
):
    """The point of putting rules in a file."""
    policy = tmp_path / "ctrlz.policy.yaml"
    policy.write_text(
        """
version: 1
defaults: {risk_threshold: 70, block_on_risk: false}
rules:
  - name: no-touching-users
    when: {tables: [users], kind: [write]}
    action: block
    risk: 99
    message: users is off limits in this project
"""
    )
    monkeypatch.setenv("CTRLZ_POLICY", str(policy))

    # A statement the shipped defaults allow outright.
    code, out, _ = run(
        capsys, "--dsn", db_url, "check", "UPDATE users SET salary = 1 WHERE id = 1"
    )
    assert code == EXIT_BLOCK
    assert "off limits" in out


def test_policy_lint_rejects_a_broken_file(capsys, db_url, tmp_path, monkeypatch):
    policy = tmp_path / "bad.yaml"
    policy.write_text("version: 1\nrules:\n  - name: r\n    when: {has_filtr: false}\n")
    monkeypatch.setenv("CTRLZ_POLICY", str(policy))

    code, _out, err = run(capsys, "--dsn", db_url, "policy", "lint")
    assert code == 1
    assert "has_filtr" in err


# -- run -------------------------------------------------------------------


def test_run_blocks_and_explains(capsys, db_url):
    code, _out, err = run(capsys, "--dsn", db_url, "run", "DELETE FROM users")
    assert code == EXIT_BLOCK
    assert "Blocked by a guardrail" in err
    assert "--force" in err


def test_run_force_records_the_override(capsys, db_url):
    code, out, err = run(
        capsys, "--dsn", db_url, "run", "DELETE FROM users", "--force", "--label", "wipe"
    )
    assert code == 0
    assert "overridden with --force" in err

    _code, payload, _ = run_json(capsys, "--dsn", db_url, "--json", "log", "-n", "1")
    assert payload[0]["policy_outcome"] == "forced"
    assert payload[0]["risk"] == 90


def test_run_records_the_actor(capsys, db_url, monkeypatch):
    monkeypatch.setenv("CTRLZ_ACTOR", "praveen")
    monkeypatch.setenv("CTRLZ_TICKET", "OPS-9")
    run(capsys, "--dsn", db_url, "run", "UPDATE users SET salary = 1 WHERE id = 1")

    _code, payload, _ = run_json(capsys, "--dsn", db_url, "--json", "log", "-n", "1")
    assert payload[0]["actor_user"] == "praveen"
    assert payload[0]["ticket"] == "OPS-9"
    assert payload[0]["channel"] == "cli"


def test_dry_run_changes_nothing(capsys, db_url):
    code, out, _ = run(
        capsys, "--dsn", db_url, "run",
        "UPDATE users SET salary = 0 WHERE id > 0", "--dry-run",
    )
    assert code == 0
    assert "Rolled back" in out
    assert "2 row(s)" in out


# -- the loop still works end to end ---------------------------------------


def test_run_preview_undo(capsys, db_url):
    run(capsys, "--dsn", db_url, "run",
        "UPDATE users SET salary = 1 WHERE name = 'ada'", "--label", "oops")

    code, out, _ = run(capsys, "--dsn", db_url, "preview")
    assert code == 0
    assert "UNDOABLE" in out

    code, out, _ = run(capsys, "--dsn", db_url, "undo", "--yes")
    assert code == 0
    assert "Undone." in out


def test_log_shows_actor_and_risk_columns(capsys, db_url):
    run(capsys, "--dsn", db_url, "run", "UPDATE users SET salary = 2 WHERE id = 1")
    code, out, _ = run(capsys, "--dsn", db_url, "log")
    assert code == 0
    assert "ACTOR" in out and "RISK" in out


def test_doctor_reports_policy_schema_and_gaps(capsys, db_url):
    run(capsys, "--dsn", db_url, "untrack", "users")
    code, out, _ = run(capsys, "--dsn", db_url, "doctor")
    assert code == 0
    from ctrlz.migrations import CURRENT_VERSION

    assert f"schema:      v{CURRENT_VERSION}" in out
    assert "NOT protected" in out
    assert "block on risk: no" in out
