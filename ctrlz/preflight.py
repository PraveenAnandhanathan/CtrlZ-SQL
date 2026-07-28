"""Guardrails that run *before* a statement executes.

Important: nothing in this module is load-bearing for undo correctness. Undo is
built from row images captured by the database itself, never from parsing SQL.
These checks are a seatbelt -- a fast, deliberately shallow look at the text to
catch the classic "I forgot the WHERE clause" mistake before it happens. A
false negative here costs nothing, because capture still recorded every row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Strip string literals, dollar-quoted bodies, and comments before looking for
# keywords, so a WHERE inside a string doesn't fool us in either direction.
_DOLLAR_QUOTED = re.compile(r"\$([A-Za-z_]\w*)?\$.*?\$\1?\$", re.S)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_SINGLE_QUOTED = re.compile(r"'(?:''|[^'])*'", re.S)
_DOUBLE_QUOTED = re.compile(r'"(?:""|[^"])*"', re.S)


def normalize(sql: str) -> str:
    """Return SQL with comments and quoted text blanked out."""
    out = _DOLLAR_QUOTED.sub(" ", sql)
    out = _BLOCK_COMMENT.sub(" ", out)
    out = _LINE_COMMENT.sub(" ", out)
    out = _SINGLE_QUOTED.sub(" '' ", out)
    out = _DOUBLE_QUOTED.sub(" ident ", out)
    return re.sub(r"\s+", " ", out).strip()


def leading_keyword(sql: str) -> str:
    m = re.match(r"[A-Za-z]+", normalize(sql))
    return m.group(0).upper() if m else ""


def is_dml(sql: str) -> bool:
    return leading_keyword(sql) in {"INSERT", "UPDATE", "DELETE", "MERGE"}


def is_ddl(sql: str) -> bool:
    return leading_keyword(sql) in {
        "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "COMMENT",
    }


@dataclass
class Preflight:
    """The verdict on a statement we are about to run."""

    sql: str
    keyword: str
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)


def inspect(sql: str, tracked: set[str] | None = None) -> Preflight:
    """Look at a statement and report anything alarming about it."""
    text = normalize(sql)
    keyword = leading_keyword(sql)
    pf = Preflight(sql=sql, keyword=keyword)

    if keyword in {"UPDATE", "DELETE"} and not re.search(r"\bWHERE\b", text, re.I):
        pf.blockers.append(
            f"{keyword} with no WHERE clause -- this touches every row in the table."
        )

    if keyword == "TRUNCATE":
        pf.blockers.append(
            "TRUNCATE does not fire row triggers, so ctrlz cannot capture or "
            "undo it. Use DELETE if you want an undoable operation."
        )

    if re.search(r"\bWHERE\s+1\s*=\s*1\b", text, re.I):
        pf.warnings.append("WHERE 1=1 matches every row.")

    if keyword == "DROP":
        pf.warnings.append(
            "DDL is not captured. ctrlz cannot undo this; take a backup first."
        )
    elif is_ddl(sql):
        pf.warnings.append("DDL is not captured. ctrlz cannot undo this statement.")

    if tracked is not None and is_dml(sql):
        target = _target_table(text)
        if target and not _matches_tracked(target, tracked):
            pf.warnings.append(
                f"{target} does not look tracked -- changes to it will not be undoable. "
                f"Run: ctrlz track {target}"
            )

    return pf


def _target_table(text: str) -> str | None:
    """Best-effort extraction of the table a DML statement writes to.

    Only used to warn about untracked tables. Wrong answers here degrade to a
    missing or spurious warning, never to a wrong undo.
    """
    patterns = (
        r"\bUPDATE\s+(?:ONLY\s+)?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
        r"\bDELETE\s+FROM\s+(?:ONLY\s+)?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
        r"\bINSERT\s+INTO\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
        r"\bMERGE\s+INTO\s+([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)",
    )
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1)
    return None


def _matches_tracked(target: str, tracked: set[str]) -> bool:
    target = target.lower()
    for name in tracked:
        name = name.lower()
        if target == name or name.endswith("." + target):
            return True
    return False
