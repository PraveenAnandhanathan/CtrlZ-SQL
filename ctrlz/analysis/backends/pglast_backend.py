"""pglast backend -- PostgreSQL's own grammar, optional.

pglast wraps libpg_query, which is the parser the PostgreSQL server itself
uses. When it says a statement has no WHERE clause, that is not an opinion.

It is optional because it is a C extension, and NFR-8 forbids making one
mandatory. Install with ``pip install ctrlz-sql[pg-parser]``. If it is absent,
the registry silently uses sqlglot -- absence must cost accuracy, never
availability.
"""

from __future__ import annotations

from ..model import (
    ALTER,
    CONFIDENCE_EXACT,
    CONFIDENCE_PARTIAL,
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

_STATEMENT_BY_NODE = {
    "SelectStmt": SELECT,
    "InsertStmt": INSERT,
    "UpdateStmt": UPDATE,
    "DeleteStmt": DELETE,
    "MergeStmt": MERGE,
    "TruncateStmt": TRUNCATE,
    "CreateStmt": CREATE,
    "CreateTableAsStmt": CREATE,
    "AlterTableStmt": ALTER,
    "DropStmt": DROP,
}


class PglastBackend(Backend):
    name = "pglast"

    @classmethod
    def available(cls) -> bool:
        try:
            import pglast  # noqa: F401
        except ImportError:
            return False
        return True

    def analyze_script(self, sql: str, dialect: str | None = None) -> list[Analysis]:
        import pglast

        statements = pglast.parse_sql(sql)
        return [self._analyze_one(raw, sql) for raw in statements]

    def _analyze_one(self, raw, original: str) -> Analysis:
        node = raw.stmt
        node_name = type(node).__name__
        statement = _STATEMENT_BY_NODE.get(node_name, UNKNOWN)

        notes: tuple[str, ...] = ()
        confidence = CONFIDENCE_EXACT
        if statement == UNKNOWN:
            notes += (f"unmodelled statement type {node_name}",)
            confidence = CONFIDENCE_PARTIAL

        written = _written_tables(node, statement)
        read = tuple(t for t in _all_tables(node) if t not in written)
        where = getattr(node, "whereClause", None) if statement in (UPDATE, DELETE) else None

        return Analysis(
            sql=original,
            statement=statement,
            kind=kind_for(statement),
            written_tables=written,
            read_tables=read,
            has_filter=where is not None,
            filter_is_tautology=_is_tautology(where),
            written_columns=_written_columns(node, statement),
            has_join=_contains(node, "JoinExpr"),
            has_subquery=_contains(node, "SubLink"),
            has_cte=getattr(node, "withClause", None) is not None,
            confidence=confidence,
            backend=self.name,
            notes=notes,
        )


# -- helpers ---------------------------------------------------------------


def _range_var_name(node) -> str:
    if node is None:
        return ""
    schema = getattr(node, "schemaname", None)
    name = getattr(node, "relname", None)
    if not name:
        return ""
    return f"{schema}.{name}" if schema else name


def _written_tables(node, statement: str) -> tuple[str, ...]:
    if statement == TRUNCATE:
        return _dedupe(_range_var_name(r) for r in (getattr(node, "relations", None) or ()))
    if statement == DROP:
        # DropStmt keeps its targets as dotted name parts rather than RangeVars.
        names = []
        for target in getattr(node, "objects", None) or ():
            parts = [getattr(p, "sval", None) for p in (target or ())]
            names.append(".".join(p for p in parts if p))
        return _dedupe(names)
    relation = getattr(node, "relation", None)
    if relation is not None:
        return _dedupe((_range_var_name(relation),))
    return ()


def _all_tables(node) -> tuple[str, ...]:
    found: list[str] = []
    _walk(node, "RangeVar", lambda n: found.append(_range_var_name(n)))
    return _dedupe(found)


def _written_columns(node, statement: str) -> tuple[str, ...]:
    targets = getattr(node, "targetList", None) or ()
    if statement in (UPDATE, INSERT):
        return _dedupe(getattr(t, "name", None) or "" for t in targets)
    return ()


def _is_tautology(where) -> bool:
    if where is None:
        return False
    node_type = type(where).__name__
    if node_type == "A_Const":
        # WHERE true -- a bare boolean constant.
        boolean = getattr(getattr(where, "val", None), "boolval", None)
        return boolean is True
    if node_type != "A_Expr":
        return False
    left, right = getattr(where, "lexpr", None), getattr(where, "rexpr", None)
    if type(left).__name__ == "A_Const" and type(right).__name__ == "A_Const":
        return _const_text(left) == _const_text(right)
    return False


def _const_text(const) -> str:
    value = getattr(const, "val", None)
    for attribute in ("ival", "sval", "fval", "boolval"):
        holder = getattr(value, attribute, None)
        if holder is not None:
            return str(getattr(holder, attribute, holder))
    return repr(value)


def _contains(node, type_name: str) -> bool:
    found = []
    _walk(node, type_name, lambda n: found.append(n))
    return bool(found)


def _walk(node, type_name: str, visit, depth: int = 0) -> None:
    """Depth-limited walk over the pglast node tree."""
    if node is None or depth > 40:
        return
    if type(node).__name__ == type_name:
        visit(node)
    children = getattr(node, "__slots__", None)
    if children:
        for slot in children:
            _walk(getattr(node, slot, None), type_name, visit, depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk(item, type_name, visit, depth + 1)


def _dedupe(values) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return tuple(seen)
