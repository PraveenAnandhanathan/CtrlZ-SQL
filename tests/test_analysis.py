"""Analysis layer: the differential corpus, and the promise never to raise."""

from __future__ import annotations

import pathlib

import pytest
import yaml

from ctrlz.analysis import analyze, analyze_script, available_backends
from ctrlz.analysis.backends import PglastBackend, RegexBackend, SqlglotBackend
from ctrlz.analysis.model import CONFIDENCE_TEXTUAL, UNKNOWN, Analysis, merge

CORPUS = yaml.safe_load(
    (pathlib.Path(__file__).parent / "corpus" / "statements.yaml").read_text()
)

PARSING_BACKENDS = [b for b in (SqlglotBackend, PglastBackend) if b.available()]
ALL_BACKENDS = PARSING_BACKENDS + [RegexBackend]


def ids(entries):
    return [e["sql"][:58] for e in entries]


def names(values):
    """Table names, case-folded.

    PostgreSQL folds unquoted identifiers to lower case, so pglast reports
    `USERS` as `users` while sqlglot preserves what was typed. Both are
    defensible; neither is a safety difference. Policy matching is
    case-insensitive for the same reason, so the corpus compares that way too.
    """
    return {v.lower() for v in values}


# -- the corpus ------------------------------------------------------------


@pytest.mark.parametrize("backend", PARSING_BACKENDS, ids=lambda b: b.name)
@pytest.mark.parametrize("entry", CORPUS, ids=ids(CORPUS))
def test_parsing_backends_match_the_corpus(backend, entry):
    """Every real parser must read every corpus statement the same way."""
    analysis = analyze(entry["sql"], dialect="postgres", prefer=backend.name)
    assert analysis.backend == backend.name

    assert analysis.statement == entry["statement"]
    assert analysis.has_filter is entry["has_filter"]
    assert names(analysis.written_tables) == names(entry["written_tables"])

    if "read_tables" in entry:
        assert names(entry["read_tables"]) <= names(analysis.read_tables)
    if "tautology" in entry:
        assert analysis.filter_is_tautology is entry["tautology"]


@pytest.mark.parametrize("entry", CORPUS, ids=ids(CORPUS))
def test_regex_backend_matches_or_diverges_on_the_record(entry):
    """The fallback is held to a lower bar, but the gap is documented.

    Where regex cannot answer correctly, the corpus says so explicitly and
    gives the reason. An undocumented divergence fails this test -- that is how
    we find out when the floor has quietly dropped.
    """
    analysis = analyze(entry["sql"], prefer="regex")
    divergence = entry.get("regex", {})

    assert analysis.statement == entry["statement"]

    expected_filter = divergence.get("has_filter", entry["has_filter"])
    assert analysis.has_filter is expected_filter

    expected_tables = divergence.get("written_tables", entry["written_tables"])
    assert names(analysis.written_tables) == names(expected_tables)

    if divergence:
        assert entry.get("reason"), "a documented divergence must explain itself"


@pytest.mark.parametrize("entry", CORPUS, ids=ids(CORPUS))
def test_every_backend_agrees_on_whether_this_is_a_write(entry):
    """Backends may differ on detail, never on 'does this change data'."""
    verdicts = {
        backend.name: analyze(entry["sql"], prefer=backend.name).is_write
        for backend in ALL_BACKENDS
    }
    assert len(set(verdicts.values())) == 1, verdicts


# -- the one invariant of this package -------------------------------------


HOSTILE_INPUTS = [
    "",
    "   ",
    ";",
    ";;;;",
    "this is not sql at all !!!",
    "SELECT",
    "UPDATE users SET",
    "DELETE FROM",
    "SELECT * FROM t WHERE x = '",       # unterminated literal
    "/* never closed",
    "$$ dangling dollar quote",
    "SELECT " + "1," * 5000 + "1",       # very wide
    "\x00\x01\x02 binary junk \xff",
    "'" * 999,
    "(" * 500,
    "WITH " + "a AS (SELECT 1), " * 200 + "b AS (SELECT 1) SELECT 1",
]


@pytest.mark.parametrize("sql", HOSTILE_INPUTS, ids=lambda s: repr(s[:28]))
def test_analyze_never_raises(sql):
    """analyze() is called on whatever a human typed. It may not explode."""
    analysis = analyze(sql)
    assert isinstance(analysis, Analysis)
    assert 0.0 <= analysis.confidence <= 1.0


@pytest.mark.parametrize("sql", HOSTILE_INPUTS, ids=lambda s: repr(s[:28]))
@pytest.mark.parametrize("backend", ALL_BACKENDS, ids=lambda b: b.name)
def test_every_backend_survives_hostile_input(backend, sql):
    analysis = analyze(sql, prefer=backend.name)
    assert isinstance(analysis, Analysis)


def test_analyze_tolerates_non_string_input():
    assert analyze(None).statement == UNKNOWN  # type: ignore[arg-type]


# -- degradation -----------------------------------------------------------


def test_unparseable_sql_degrades_to_regex_with_a_note():
    """A parser failure costs accuracy, never availability."""
    analysis = analyze("UPDATE users SET WHERE ((((", prefer="sqlglot")
    assert analysis.confidence <= CONFIDENCE_TEXTUAL
    assert analysis.notes, "a degraded reading must say why"


def test_unknown_preference_falls_back_to_the_default_chain():
    analysis = analyze("DELETE FROM t WHERE id = 1", prefer="nonexistent-parser")
    assert analysis.backend in available_backends()
    assert analysis.has_filter is True


def test_missing_optional_backend_does_not_break_the_chain(monkeypatch):
    monkeypatch.setattr(PglastBackend, "available", classmethod(lambda cls: False))
    analysis = analyze("DELETE FROM t WHERE id = 1", prefer="pglast")
    assert analysis.backend != "pglast"
    assert analysis.has_filter is True


def test_regex_backend_is_always_available():
    assert RegexBackend.available() is True
    assert "regex" in available_backends()


# -- multi-statement scripts -----------------------------------------------


def test_script_is_split_into_statements():
    parts = analyze_script("UPDATE a SET x = 1 WHERE id = 1; DELETE FROM b WHERE id = 2")
    assert [p.statement for p in parts] == ["UPDATE", "DELETE"]


def test_script_merge_is_conservative_about_filters():
    """One unfiltered statement makes the whole script unfiltered.

    A script is exactly as dangerous as its most dangerous statement.
    """
    analysis = analyze("UPDATE a SET x = 1 WHERE id = 1; DELETE FROM b")
    assert analysis.has_filter is False
    assert set(analysis.written_tables) == {"a", "b"}


def test_script_merge_takes_the_lowest_confidence():
    high = Analysis(sql="a", statement="UPDATE", confidence=0.9, has_filter=True)
    low = Analysis(sql="b", statement="DELETE", confidence=0.5, has_filter=True)
    assert merge([high, low], "a; b").confidence == 0.5


def test_semicolon_inside_a_literal_does_not_split():
    parts = analyze_script("UPDATE t SET note = 'a;b' WHERE id = 1")
    assert len(parts) == 1


# -- properties on the model ------------------------------------------------


@pytest.mark.parametrize(
    "sql,unfiltered",
    [
        ("UPDATE t SET x = 1", True),
        ("UPDATE t SET x = 1 WHERE id = 2", False),
        ("DELETE FROM t", True),
        ("DELETE FROM t WHERE id = 2", False),
        ("INSERT INTO t (x) VALUES (1)", False),   # an INSERT needs no filter
        ("SELECT * FROM t", False),
    ],
)
def test_unfiltered_only_flags_statements_that_need_a_filter(sql, unfiltered):
    assert analyze(sql).unfiltered is unfiltered
