"""Where the rulebook comes from, and whether you agreed to it.

`find_policy_file` walks up the directory tree, which is convenient and is also
how a rulebook you never chose ends up governing your session. A policy can
*weaken* protection — an empty `rules:` list disables every guardrail — so this
is not a configuration nicety, it is the difference between the headline
protection being on and being off.

Measured before the ownership check existed: a `ctrlz.policy.yaml` two
directories up took `DELETE FROM t` with no WHERE from blocked to committed,
and nothing in the output mentioned that a policy file had been read at all.

Same shape as git's CVE-2022-24765, and the same answer: a file found by
searching upwards is trusted only if it belongs to you.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from ctrlz.errors import ConfigError
from ctrlz.policy import find_policy_file, load_policy

WIDE_OPEN = """
version: 1
defaults: {risk_threshold: 100, block_on_risk: false}
rules: []
"""


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A working directory two levels below where a policy file will sit."""
    work = tmp_path / "someones" / "work"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.delenv("CTRLZ_POLICY", raising=False)
    return tmp_path, work


# -- discovery still works for your own files -------------------------------


def test_a_policy_you_own_is_found_by_walking_up(tree):
    root, _work = tree
    (root / "ctrlz.policy.yaml").write_text(WIDE_OPEN)

    found = find_policy_file()
    assert found == root / "ctrlz.policy.yaml"
    assert load_policy().rules == []


def test_no_policy_anywhere_means_the_built_in_defaults(tree):
    assert find_policy_file() is None
    assert load_policy().source == "<built-in defaults>"


# -- a file somebody else owns is refused -----------------------------------


@pytest.mark.skipif(os.name != "posix", reason="ownership is a POSIX concept")
@pytest.mark.skipif(os.getuid() != 0, reason="need root to create a foreign-owned file")
def test_a_policy_owned_by_someone_else_is_refused(tree):
    """The case the check exists for.

    Running as root is the only way to *make* a file owned by another user, so
    this skips for an unprivileged runner rather than pretending to test it.
    `test_the_ownership_check_is_reachable` covers the logic either way.
    """
    root, _work = tree
    policy = root / "ctrlz.policy.yaml"
    policy.write_text(WIDE_OPEN)
    os.chown(policy, 65534, 65534)          # nobody

    with pytest.raises(ConfigError) as caught:
        find_policy_file()

    message = str(caught.value)
    assert "refusing to use the policy" in message
    assert str(policy) in message
    assert "CTRLZ_POLICY" in message        # names the way to consent


@pytest.mark.skipif(os.name != "posix", reason="ownership is a POSIX concept")
@pytest.mark.skipif(os.getuid() != 0, reason="need root to create a foreign-owned file")
def test_naming_the_file_explicitly_is_consent(tree, monkeypatch):
    """`$CTRLZ_POLICY` bypasses the check, because asking for a specific file
    by name is a decision rather than an accident."""
    root, _work = tree
    policy = root / "ctrlz.policy.yaml"
    policy.write_text(WIDE_OPEN)
    os.chown(policy, 65534, 65534)

    monkeypatch.setenv("CTRLZ_POLICY", str(policy))
    assert find_policy_file() == policy
    assert load_policy().rules == []


def test_the_ownership_check_is_reachable(tmp_path, monkeypatch):
    """Exercises the refusal without needing to own another user.

    A test that can only run as root is a test that does not run, so the
    ownership lookup is faked here and the *decision* is what is checked.
    """
    from ctrlz.policy import loader

    policy = tmp_path / "ctrlz.policy.yaml"
    policy.write_text(WIDE_OPEN)

    real_stat = pathlib.Path.stat

    def foreign(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == policy:
            class Faked:
                st_uid = os.getuid() + 1000
            return Faked()
        return result

    monkeypatch.setattr(pathlib.Path, "stat", foreign)
    with pytest.raises(ConfigError) as caught:
        loader._check_ownership(policy)
    assert "owned by" in str(caught.value)


def test_a_root_owned_policy_is_accepted(tmp_path, monkeypatch):
    """A site-wide rulebook placed by an administrator is deliberate, and root
    can do anything else on the machine regardless."""
    from ctrlz.policy import loader

    policy = tmp_path / "ctrlz.policy.yaml"
    policy.write_text(WIDE_OPEN)

    class RootOwned:
        st_uid = 0

    monkeypatch.setattr(pathlib.Path, "stat", lambda self, *a, **k: RootOwned())
    loader._check_ownership(policy)         # must not raise


# -- the guardrail really was being disabled --------------------------------


def test_an_inherited_policy_really_can_disable_the_guardrails(tree, capsys):
    """Proof the threat is real rather than theoretical.

    This is the measurement that motivated the check: with a permissive policy
    above the working directory, the flagship blocked statement commits.
    """
    import sqlite3

    from ctrlz.cli import EXIT_BLOCK, main

    root, work = tree
    conn = sqlite3.connect(work / "app.db")
    conn.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);"
        "INSERT INTO t (v) VALUES ('a'), ('b');"
    )
    conn.commit()
    conn.close()

    dsn = f"sqlite:///{work / 'app.db'}"
    main(["--dsn", dsn, "init"])
    main(["--dsn", dsn, "track", "t"])
    capsys.readouterr()

    # With the shipped defaults, this is the headline refusal.
    assert main(["--dsn", dsn, "check", "DELETE FROM t"]) == EXIT_BLOCK

    # A permissive rulebook above the working directory removes it entirely.
    (root / "ctrlz.policy.yaml").write_text(WIDE_OPEN)
    assert main(["--dsn", dsn, "check", "DELETE FROM t"]) == 0


def test_run_says_which_policy_file_is_in_force(tree, capsys):
    """The commoner, non-malicious case: a rulebook you did not know about.

    Ownership checking cannot help when the file is legitimately yours -- a
    repository policy you have never read is still a surprise -- so `run` names
    the source whenever it is not the built-in default.
    """
    import sqlite3

    from ctrlz.cli import main

    root, work = tree
    conn = sqlite3.connect(work / "app.db")
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);")
    conn.commit()
    conn.close()

    dsn = f"sqlite:///{work / 'app.db'}"
    main(["--dsn", dsn, "init"])
    main(["--dsn", dsn, "track", "t"])

    (root / "ctrlz.policy.yaml").write_text(WIDE_OPEN)
    capsys.readouterr()
    main(["--dsn", dsn, "run", "INSERT INTO t (v) VALUES ('x')"])

    reported = capsys.readouterr().err
    assert "policy:" in reported
    assert str(root / "ctrlz.policy.yaml") in reported


def test_run_stays_quiet_about_the_built_in_defaults(tmp_path, capsys, monkeypatch):
    """Announcing the default on every run would be noise, and noise is what
    stops people reading the line that matters."""
    import sqlite3

    from ctrlz.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CTRLZ_POLICY", raising=False)
    conn = sqlite3.connect(tmp_path / "app.db")
    conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT);")
    conn.commit()
    conn.close()

    dsn = f"sqlite:///{tmp_path / 'app.db'}"
    main(["--dsn", dsn, "init"])
    main(["--dsn", dsn, "track", "t"])
    capsys.readouterr()
    main(["--dsn", dsn, "run", "INSERT INTO t (v) VALUES ('x')"])

    assert "policy:" not in capsys.readouterr().err
