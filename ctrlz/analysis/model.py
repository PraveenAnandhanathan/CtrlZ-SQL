"""What a SQL statement says.

This is the single currency of the analysis layer: every backend produces an
``Analysis``, and the policy engine consumes nothing else. Swapping the parser
must never change the shape of what policy sees.

Everything here is *advisory*. Analysis answers "what did the user ask for",
which is not the same question as "what did the database do" -- see spec.md §4.
No undo path may import this module, and a test enforces that.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

# Statement classes we distinguish. Anything else is UNKNOWN, which is a
# perfectly respectable answer for a tool that must never guess.
SELECT = "SELECT"
INSERT = "INSERT"
UPDATE = "UPDATE"
DELETE = "DELETE"
MERGE = "MERGE"
TRUNCATE = "TRUNCATE"
CREATE = "CREATE"
ALTER = "ALTER"
DROP = "DROP"
UNKNOWN = "UNKNOWN"

# Coarse grouping used by policy conditions.
WRITE = "write"
READ = "read"
DDL = "ddl"
OTHER = "other"

WRITE_STATEMENTS = frozenset({INSERT, UPDATE, DELETE, MERGE})
DDL_STATEMENTS = frozenset({CREATE, ALTER, DROP, TRUNCATE})

#: Statements where the absence of a row filter means "every row in the table".
FILTERABLE = frozenset({UPDATE, DELETE})

# Confidence bands. These are not probabilities; they are a ranking that lets
# policy say "do not act strongly on a reading I am unsure of".
CONFIDENCE_EXACT = 1.0      # the database's own grammar
CONFIDENCE_PARSED = 0.9     # a real parser, different grammar
CONFIDENCE_PARTIAL = 0.6    # parsed, but contains constructs we do not model
CONFIDENCE_TEXTUAL = 0.5    # pattern matching over normalised text
CONFIDENCE_GUESS = 0.3      # nothing parsed; we are reading tea leaves


@dataclass(frozen=True)
class Analysis:
    """A structured reading of one statement."""

    sql: str
    statement: str = UNKNOWN
    kind: str = OTHER
    written_tables: tuple[str, ...] = ()
    read_tables: tuple[str, ...] = ()
    has_filter: bool = False
    filter_is_tautology: bool = False
    written_columns: tuple[str, ...] = ()
    has_join: bool = False
    has_subquery: bool = False
    has_cte: bool = False
    confidence: float = CONFIDENCE_GUESS
    backend: str = "none"
    notes: tuple[str, ...] = ()

    @property
    def is_write(self) -> bool:
        return self.statement in WRITE_STATEMENTS

    @property
    def is_ddl(self) -> bool:
        return self.statement in DDL_STATEMENTS

    @property
    def needs_filter(self) -> bool:
        """True when a missing filter means "every row in the table"."""
        return self.statement in FILTERABLE

    @property
    def unfiltered(self) -> bool:
        """The classic disaster: an UPDATE or DELETE with nothing to limit it."""
        return self.needs_filter and not self.has_filter

    @property
    def tables(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for name in self.written_tables + self.read_tables:
            seen.setdefault(name, None)
        return tuple(seen)

    def with_note(self, note: str, confidence: float | None = None) -> "Analysis":
        return replace(
            self,
            notes=self.notes + (note,),
            confidence=self.confidence if confidence is None else confidence,
        )


def kind_for(statement: str) -> str:
    if statement in WRITE_STATEMENTS:
        return WRITE
    if statement in DDL_STATEMENTS:
        return DDL
    if statement == SELECT:
        return READ
    return OTHER


def merge(analyses: Iterable[Analysis], sql: str) -> Analysis:
    """Collapse a multi-statement script into one conservative reading.

    "Conservative" means every judgement lands on the alarming side: if any
    write in the script is unfiltered, the whole script reads as unfiltered.
    A script is exactly as dangerous as its most dangerous statement.
    """
    analyses = list(analyses)
    if not analyses:
        return Analysis(sql=sql)
    if len(analyses) == 1:
        return analyses[0]

    writes = [a for a in analyses if a.is_write]
    ddl = [a for a in analyses if a.is_ddl]
    lead = (writes or ddl or analyses)[0]

    # has_filter is only meaningful for statements that need one. If none of
    # them do, inherit the leading statement's value rather than inventing one.
    filterable = [a for a in analyses if a.needs_filter]
    has_filter = all(a.has_filter for a in filterable) if filterable else lead.has_filter

    return Analysis(
        sql=sql,
        statement=lead.statement,
        kind=lead.kind,
        written_tables=_dedupe(t for a in analyses for t in a.written_tables),
        read_tables=_dedupe(t for a in analyses for t in a.read_tables),
        has_filter=has_filter,
        filter_is_tautology=any(a.filter_is_tautology for a in analyses),
        written_columns=_dedupe(c for a in analyses for c in a.written_columns),
        has_join=any(a.has_join for a in analyses),
        has_subquery=any(a.has_subquery for a in analyses),
        has_cte=any(a.has_cte for a in analyses),
        confidence=min(a.confidence for a in analyses),
        backend=analyses[0].backend,
        notes=_dedupe(
            (f"script of {len(analyses)} statements",)
            + tuple(n for a in analyses for n in a.notes)
        ),
    )


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return tuple(seen)
