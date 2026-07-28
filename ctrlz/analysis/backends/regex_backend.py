"""Pattern-matching backend -- the floor that is always available.

This is the original pre-flight logic, promoted to a proper backend. It has no
dependencies and no build step, so it is what remains when a parser is missing
or a statement is too malformed for one.

It reports low confidence on purpose. Reading SQL with regular expressions is
guessing, and the system is designed so that guessing here is survivable: a
missed warning, never a wrong undo.
"""

from __future__ import annotations

import re

from ..model import (
    ALTER,
    CONFIDENCE_TEXTUAL,
    FILTERABLE,
    CREATE,
    DELETE,
    DROP,
    INSERT,
    MERGE,
    SELECT,
    TRUNCATE,
    UNKNOWN,
    UPDATE,
    Analysis,
    kind_for,
)
from .base import Backend

# Blank out anything that could contain a keyword without meaning it, so a
# WHERE inside a string or a comment never counts as a filter.
_DOLLAR_QUOTED = re.compile(r"\$([A-Za-z_]\w*)?\$.*?\$\1?\$", re.S)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_SINGLE_QUOTED = re.compile(r"'(?:''|[^'])*'", re.S)
_DOUBLE_QUOTED = re.compile(r'"(?:""|[^"])*"', re.S)

_IDENT = r"[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?"

_TARGET_PATTERNS = (
    (UPDATE, re.compile(rf"\bUPDATE\s+(?:ONLY\s+)?({_IDENT})", re.I)),
    (DELETE, re.compile(rf"\bDELETE\s+FROM\s+(?:ONLY\s+)?({_IDENT})", re.I)),
    (INSERT, re.compile(rf"\bINSERT\s+INTO\s+({_IDENT})", re.I)),
    (MERGE, re.compile(rf"\bMERGE\s+INTO\s+({_IDENT})", re.I)),
    (TRUNCATE, re.compile(rf"\bTRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?({_IDENT})", re.I)),
    (ALTER, re.compile(
        rf"\bALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?({_IDENT})", re.I)),
    (DROP, re.compile(
        rf"\bDROP\s+(?:TABLE|VIEW|INDEX|SEQUENCE)\s+(?:IF\s+EXISTS\s+)?({_IDENT})", re.I)),
    (CREATE, re.compile(
        rf"\bCREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP|TEMPORARY|UNLOGGED\s*)?\s*"
        rf"(?:TABLE|VIEW|SEQUENCE)\s+(?:IF\s+NOT\s+EXISTS\s+)?({_IDENT})", re.I)),
)

_KEYWORD_TO_STATEMENT = {
    "SELECT": SELECT,
    "WITH": SELECT,
    "INSERT": INSERT,
    "UPDATE": UPDATE,
    "DELETE": DELETE,
    "MERGE": MERGE,
    "TRUNCATE": TRUNCATE,
    "CREATE": CREATE,
    "ALTER": ALTER,
    "DROP": DROP,
}

_TAUTOLOGY = re.compile(
    r"\bWHERE\s+(?:1\s*=\s*1|true|'1'\s*=\s*'1')\b(?!\s+AND\b)", re.I
)

#: DML keywords we look for behind a leading CTE, longest first so that
#: "DELETE FROM" wins over a bare "DELETE".
_DML_KEYWORDS = (
    (INSERT, re.compile(r"\bINSERT\s+INTO\b", re.I)),
    (UPDATE, re.compile(r"\bUPDATE\b", re.I)),
    (DELETE, re.compile(r"\bDELETE\s+FROM\b", re.I)),
    (MERGE, re.compile(r"\bMERGE\s+INTO\b", re.I)),
)


def _paren_depths(text: str) -> list[int]:
    """Parenthesis nesting depth at each character position.

    Everything at depth 0 belongs to the statement itself; anything deeper
    belongs to a subquery or a CTE body and must not be mistaken for it.
    """
    depths: list[int] = []
    depth = 0
    for char in text:
        if char == "(":
            depths.append(depth)
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
            depths.append(depth)
        else:
            depths.append(depth)
    return depths


def _first_top_level(text: str, depths: list[int], keywords) -> str:
    """The earliest keyword occurring outside any parentheses."""
    best_position = len(text) + 1
    best_statement = ""
    for statement, pattern in keywords:
        for match in pattern.finditer(text):
            if depths[match.start()] == 0 and match.start() < best_position:
                best_position = match.start()
                best_statement = statement
            break
    return best_statement


def normalize(sql: str) -> str:
    """Return SQL with comments and quoted text blanked out."""
    out = _DOLLAR_QUOTED.sub(" ", sql)
    out = _BLOCK_COMMENT.sub(" ", out)
    out = _LINE_COMMENT.sub(" ", out)
    out = _SINGLE_QUOTED.sub(" '' ", out)
    out = _DOUBLE_QUOTED.sub(" ? ", out)
    return re.sub(r"\s+", " ", out).strip()


def leading_keyword(sql: str) -> str:
    match = re.match(r"[A-Za-z]+", normalize(sql))
    return match.group(0).upper() if match else ""


def split_statements(sql: str) -> list[str]:
    """Split on semicolons that are not inside a literal or comment.

    Works on the normalised text to find the boundaries, then slices the
    original so the returned statements keep their real content.
    """
    text = _DOLLAR_QUOTED.sub(lambda m: " " * len(m.group(0)), sql)
    text = _BLOCK_COMMENT.sub(lambda m: " " * len(m.group(0)), text)
    text = _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), text)
    text = _SINGLE_QUOTED.sub(lambda m: " " * len(m.group(0)), text)
    text = _DOUBLE_QUOTED.sub(lambda m: " " * len(m.group(0)), text)

    statements: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char == ";":
            chunk = sql[start:index].strip()
            if chunk:
                statements.append(chunk)
            start = index + 1
    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return statements


class RegexBackend(Backend):
    name = "regex"

    def analyze_script(self, sql: str, dialect: str | None = None) -> list[Analysis]:
        return [self._analyze_one(part) for part in split_statements(sql)] or [
            self._analyze_one(sql)
        ]

    def _analyze_one(self, sql: str) -> Analysis:
        text = normalize(sql)
        depths = _paren_depths(text)
        leading = leading_keyword(sql)
        statement = _KEYWORD_TO_STATEMENT.get(leading, UNKNOWN)

        if leading == "WITH":
            # A leading CTE hides the real statement behind it. Look for the
            # first DML keyword that is not inside the CTE's parentheses.
            statement = _first_top_level(text, depths, _DML_KEYWORDS) or SELECT

        written: tuple[str, ...] = ()
        for candidate, pattern in _TARGET_PATTERNS:
            if candidate != statement:
                continue
            for match in pattern.finditer(text):
                if depths[match.start()] == 0:
                    written = (match.group(1),)
                    break
            break

        # Only a top-level WHERE restricts the rows being written, and only
        # UPDATE and DELETE are dangerous without one. This still cannot see
        # that a WHERE belongs to a USING subquery -- the corpus records that
        # blind spot rather than pretending it does not exist.
        has_filter = statement in FILTERABLE and any(
            depths[m.start()] == 0 for m in re.finditer(r"\bWHERE\b", text, re.I)
        )

        notes: tuple[str, ...] = ()
        if statement in (UPDATE, DELETE) and not written:
            notes += ("could not identify the target table",)

        return Analysis(
            sql=sql,
            statement=statement,
            kind=kind_for(statement),
            written_tables=written,
            read_tables=(),
            has_filter=has_filter,
            filter_is_tautology=bool(_TAUTOLOGY.search(text)),
            written_columns=(),
            has_join=bool(re.search(r"\bJOIN\b", text, re.I)),
            has_subquery="(" in text and bool(re.search(r"\(\s*SELECT\b", text, re.I)),
            has_cte=bool(re.match(r"\s*WITH\b", text, re.I)),
            confidence=CONFIDENCE_TEXTUAL,
            backend=self.name,
            notes=notes + ("read by pattern matching, not parsed",),
        )
