"""The cache key, held to one invariant.

    If two statements share a fingerprint, they must get the same verdict.

Everything else here is performance. That one line is correctness, and it is why
this file exists: sharing a verdict between `WHERE 1 = 1` and `WHERE 1 = 2` would
let a statement that wipes a table inherit "allowed" from one that matches
nothing.

The invariant is checked two ways. Directly, against the same differential
corpus the analysis backends are held to -- every statement gets its literals
mutated and the verdicts must still agree. And adversarially, against the pairs
that are specifically designed to break it.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from ctrlz.analysis import analyze
from ctrlz.gateway.fingerprint import PLACEHOLDER, fingerprint
from ctrlz.policy import PolicyEngine, load_defaults

CORPUS = yaml.safe_load(
    (pathlib.Path(__file__).parent / "corpus" / "statements.yaml").read_text()
)

ENGINE = PolicyEngine(load_defaults())


def verdict(sql: str):
    """Everything the policy engine actually reads about a statement."""
    analysis = analyze(sql)
    decision = ENGINE.evaluate_sql(sql)
    return (
        analysis.statement,
        analysis.kind,
        tuple(sorted(analysis.written_tables)),
        tuple(sorted(analysis.read_tables)),
        analysis.has_filter,
        analysis.filter_is_tautology,
        analysis.unfiltered,
        decision.outcome,
        decision.risk,
        decision.decided_by.name if decision.decided_by else None,
    )


# -- the invariant, against the corpus -------------------------------------


def mutate_literals(sql: str) -> str:
    """Change every literal value, leaving the shape of the statement alone.

    Numbers are shifted and strings are renamed, so a fingerprint that ignores
    literal values must be identical for the original and the mutant.
    """
    def bump(match: re.Match) -> str:
        return str(int(match.group(0)) + 7)

    mutated = re.sub(r"(?<![\w.'])\d+(?![\w.])", bump, sql)
    return re.sub(r"'[^']*'", "'zzz'", mutated)


@pytest.mark.parametrize(
    "sql", [entry["sql"] for entry in CORPUS], ids=range(len(CORPUS))
)
def test_a_shared_fingerprint_means_a_shared_verdict(sql):
    """The whole safety argument, over every statement in the corpus."""
    mutant = mutate_literals(sql)
    if fingerprint(sql) != fingerprint(mutant):
        return          # different keys: they never share a cache entry
    assert verdict(sql) == verdict(mutant), (
        f"these share a fingerprint but not a verdict:\n"
        f"  {sql}\n  {mutant}\n"
        f"fingerprint: {fingerprint(sql)!r}"
    )


def test_the_corpus_actually_exercises_the_normalisation():
    """A guard on the guard: if nothing normalised, the test above proved
    nothing at all."""
    normalised = [
        entry["sql"] for entry in CORPUS
        if PLACEHOLDER in fingerprint(entry["sql"])
    ]
    assert len(normalised) >= 10, (
        f"only {len(normalised)} corpus statements normalise; the invariant "
        f"test is not being exercised"
    )


# -- the pairs designed to break it ----------------------------------------


@pytest.mark.parametrize(
    "one,other",
    [
        # The pair this module exists for. Same shape, opposite meaning.
        ("UPDATE t SET x = 1 WHERE 1 = 1", "UPDATE t SET x = 1 WHERE 1 = 2"),
        ("DELETE FROM t WHERE 1 = 1", "DELETE FROM t WHERE 1 = 0"),
        ("UPDATE t SET x = 1 WHERE 'a' = 'a'", "UPDATE t SET x = 1 WHERE 'a' = 'b'"),
        ("DELETE FROM t WHERE 2 > 1", "DELETE FROM t WHERE 1 > 2"),
        ("DELETE FROM t WHERE 1 <> 2", "DELETE FROM t WHERE 1 <> 1"),
    ],
)
def test_literal_against_literal_is_never_normalised(one, other):
    """A comparison between two literals is the tautology case; those keep
    their exact text so they cannot pool a verdict."""
    assert PLACEHOLDER not in fingerprint(one)
    assert fingerprint(one) == one
    assert fingerprint(one) != fingerprint(other)


def test_the_dangerous_pair_really_is_dangerous():
    """Proof that the protection is load-bearing rather than decorative.

    If these two got the same verdict anyway, the whole module would be
    solving a problem that does not exist -- so assert that they differ.
    """
    tautology = verdict("UPDATE users SET salary = 1 WHERE 1 = 1")
    ordinary = verdict("UPDATE users SET salary = 1 WHERE 1 = 2")
    assert tautology != ordinary
    assert tautology[5] is True and ordinary[5] is False    # filter_is_tautology


# -- what must normalise ---------------------------------------------------


@pytest.mark.parametrize(
    "one,other",
    [
        ("UPDATE users SET salary = 1 WHERE id = 5",
         "UPDATE users SET salary = 2 WHERE id = 6"),
        ("DELETE FROM orders WHERE created_at < '2020-01-01'",
         "DELETE FROM orders WHERE created_at < '2021-06-30'"),
        ("INSERT INTO audit (actor, action) VALUES ('ada', 'login')",
         "INSERT INTO audit (actor, action) VALUES ('bob', 'logout')"),
        ("SELECT * FROM users WHERE dept = 'eng' LIMIT 10",
         "SELECT * FROM users WHERE dept = 'ops' LIMIT 500"),
        ("UPDATE t SET x = 1.5e3 WHERE id = 9", "UPDATE t SET x = 2.5 WHERE id = 11"),
    ],
)
def test_statements_differing_only_in_values_share_a_key(one, other):
    """The point of the exercise: this is what psycopg2 traffic looks like."""
    assert fingerprint(one) == fingerprint(other)
    assert PLACEHOLDER in fingerprint(one)
    assert verdict(one) == verdict(other)


def test_a_bare_boolean_filter_needs_no_normalising():
    """`WHERE true` is already constant text, and is a tautology, so it caches
    on itself and keeps its verdict."""
    assert fingerprint("DELETE FROM t WHERE true") == "DELETE FROM t WHERE true"
    assert analyze("DELETE FROM t WHERE true").filter_is_tautology is True


# -- things that only look like literals -----------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        'UPDATE "weird name" SET x = 1 WHERE id = 2',        # quoted identifier
        "UPDATE `back ticks` SET x = 1 WHERE id = 2",        # MySQL quoting
        "UPDATE col1 SET x2 = 1 WHERE id3 = 2",              # digits in names
        "SELECT * FROM t WHERE a = $1 AND b = $2",           # bind parameters
        "UPDATE t SET x = 1 -- WHERE 1 = 1\n",               # comment
        "UPDATE t SET x = 1 /* 1 = 1 */ WHERE id = 2",       # block comment
    ],
)
def test_identifiers_and_comments_are_not_treated_as_values(sql):
    """Blanking a table name would pool verdicts across different tables,
    which is a worse bug than the one this module fixes."""
    print_key = fingerprint(sql)
    for table in ("weird name", "back ticks", "col1", "x2", "id3"):
        if table in sql:
            assert table in print_key, f"{table!r} was mangled: {print_key!r}"
    # Bind parameters are already constant; they must survive untouched.
    if "$1" in sql:
        assert "$1" in print_key and "$2" in print_key


def test_written_tables_survive_normalisation():
    """The strongest form of the previous test: the analysis of the fingerprint
    still names the same tables."""
    sql = "UPDATE public.users SET salary = 42 WHERE id = 7"
    assert analyze(fingerprint(sql).replace(PLACEHOLDER, "1")).written_tables == \
        analyze(sql).written_tables


# -- it must not raise, whatever it is handed ------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "", " ", "'", "''", '"', "`", "--", "/*", "$", "$$", "$$a$$", "$tag$x$tag$",
        "1", "1.", ".5", "1e", "1e+", "0x1f", "SELECT", "\x00", "\xff",
        "UPDATE t SET x = 'unterminated",
        "SELECT '" + "a" * 500 + "'",
        "SELECT " + "1," * 200,
    ],
)
def test_the_fingerprint_never_raises(sql):
    """It sits in the gateway's hot path, where an exception would be a
    fail-open on every statement."""
    assert isinstance(fingerprint(sql), str)


def test_a_dollar_quoted_string_is_a_value_not_a_parameter():
    one = "INSERT INTO t (body) VALUES ($tag$hello$tag$)"
    other = "INSERT INTO t (body) VALUES ($tag$goodbye$tag$)"
    assert fingerprint(one) == fingerprint(other)
    assert PLACEHOLDER in fingerprint(one)


# -- the interceptor uses it -----------------------------------------------


def test_the_interceptor_now_shares_verdicts_across_literal_values():
    """The behaviour NFR-2's restatement was written around."""
    from ctrlz.gateway import Interceptor, protocol

    interceptor = Interceptor(tracked=("users",))
    for value in range(50):
        interceptor.inspect(
            protocol.Message(
                protocol.QUERY,
                f"UPDATE users SET salary = {value} WHERE id = {value}\x00".encode(),
            )
        )
    # 50 distinct statements, one cache entry.
    assert len(interceptor._cache) == 1
    assert interceptor.evaluated == 50


def test_the_tautology_does_not_inherit_a_verdict_from_a_safe_neighbour():
    """The exact pooling this module exists to prevent, end to end.

    A statement matching nothing is judged first and allowed. A statement that
    touches every row follows, differing only in one literal. It must be judged
    on its own merits.
    """
    from ctrlz.gateway import Interceptor, protocol

    interceptor = Interceptor(tracked=("users",))

    harmless = interceptor.inspect(
        protocol.Message(protocol.QUERY, b"UPDATE users SET salary = 0 WHERE 1 = 2\x00")
    ).decision
    dangerous = interceptor.inspect(
        protocol.Message(protocol.QUERY, b"UPDATE users SET salary = 0 WHERE 1 = 1\x00")
    ).decision

    assert len(interceptor._cache) == 2, "the two statements pooled a cache entry"
    assert harmless.outcome == "allow" and harmless.risk == 0
    assert dangerous.analysis.filter_is_tautology is True
    assert dangerous.decided_by.name == "tautology-filter"
    assert dangerous.risk == 60
    # The shipped defaults warn on a tautology rather than blocking it, which is
    # D-3. What matters here is that the two reach different verdicts at all.
    assert harmless.outcome != dangerous.outcome


def test_the_tautology_is_still_refused_when_the_policy_blocks_it():
    """The same pooling, where inheriting the wrong verdict would cost data.

    Warning is the shipped default, so the previous test cannot show a refusal.
    Under a policy that blocks tautologies, the statement that touches every row
    must be refused even though a near-identical one was just allowed.
    """
    from ctrlz.gateway import Interceptor, protocol
    from ctrlz.policy import parse as parse_policy

    policy = parse_policy({
        "version": 1,
        "defaults": {"risk_threshold": 70, "block_on_risk": False},
        "rules": [{
            "name": "no-tautologies",
            "when": {"filter_is_tautology": True, "kind": ["write"]},
            "action": "block",
            "risk": 95,
            "message": "a filter matching every row is not a filter",
        }],
    })
    interceptor = Interceptor(policy=policy, tracked=("users",))

    harmless = interceptor.inspect(
        protocol.Message(protocol.QUERY, b"UPDATE users SET salary = 0 WHERE 1 = 2\x00")
    )
    assert not harmless.refused

    dangerous = interceptor.inspect(
        protocol.Message(protocol.QUERY, b"UPDATE users SET salary = 0 WHERE 1 = 1\x00")
    )
    assert dangerous.refused, "the tautology inherited a verdict it must not have"
