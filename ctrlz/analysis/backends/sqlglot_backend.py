"""sqlglot backend -- the default.

Pure Python, no build step, and it understands 20-odd dialects, which is why it
is the default under NFR-8 (no mandatory C extension).

The distinction that matters here is between the table a statement *writes* and
the tables it merely *reads*. `UPDATE a SET x = b.x FROM b` touches `a` and
reads `b`; a policy that cannot tell them apart is useless. sqlglot's AST keeps
the write target in `this` and everything else in the clause arguments, so the
separation is structural rather than guessed.
"""

from __future__ import annotations

from ..model import (
    ALTER,
    CONFIDENCE_PARSED,
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


class SqlglotBackend(Backend):
    name = "sqlglot"

    @classmethod
    def available(cls) -> bool:
        try:
            import sqlglot  # noqa: F401
        except ImportError:
            return False
        return True

    def analyze_script(self, sql: str, dialect: str | None = None) -> list[Analysis]:
        import sqlglot

        expressions = sqlglot.parse(sql, read=_dialect(dialect))
        return [
            self._analyze_one(expression, sql)
            for expression in expressions
            if expression is not None
        ]

    def _analyze_one(self, expression, original: str) -> Analysis:
        from sqlglot import exp

        sql = expression.sql(dialect=None)
        statement = _statement_of(expression, exp)
        notes: tuple[str, ...] = ()
        confidence = CONFIDENCE_PARSED

        written = _written_tables(expression, statement, exp)
        read = tuple(t for t in _all_tables(expression, exp) if t not in written)

        # Only the statement's own WHERE counts. A WHERE buried in a subquery
        # restricts the subquery, not the rows being written -- treating it as a
        # filter would turn the single most dangerous statement shape into one
        # that looks safe.
        where = expression.args.get("where") if statement in (UPDATE, DELETE) else None
        has_filter = where is not None

        if statement in (UPDATE, DELETE) and not written:
            notes += ("could not identify the target table",)
            confidence = CONFIDENCE_PARTIAL
        if statement == UNKNOWN:
            notes += (f"unmodelled statement type {type(expression).__name__}",)
            confidence = CONFIDENCE_PARTIAL

        return Analysis(
            sql=sql or original,
            statement=statement,
            kind=kind_for(statement),
            written_tables=written,
            read_tables=read,
            has_filter=has_filter,
            filter_is_tautology=_is_tautology(where, exp),
            written_columns=_written_columns(expression, statement, exp),
            has_join=bool(list(expression.find_all(exp.Join))),
            has_subquery=_has_subquery(expression, exp),
            has_cte=expression.args.get("with") is not None
            or bool(list(expression.find_all(exp.CTE))),
            confidence=confidence,
            backend=self.name,
            notes=notes,
        )


# -- helpers ---------------------------------------------------------------


def _dialect(dialect: str | None) -> str | None:
    if not dialect:
        return None
    known = {"postgresql": "postgres", "postgres": "postgres", "sqlite": "sqlite",
             "mysql": "mysql", "mssql": "tsql", "sqlserver": "tsql", "oracle": "oracle"}
    return known.get(dialect.lower())


def _statement_of(expression, exp) -> str:
    mapping = (
        (exp.Update, UPDATE),
        (exp.Delete, DELETE),
        (exp.Insert, INSERT),
        (exp.Merge, MERGE),
        (exp.Select, SELECT),
        (exp.Union, SELECT),
        (exp.Create, CREATE),
        (exp.Alter, ALTER),
        (exp.Drop, DROP),
    )
    for node_type, statement in mapping:
        if isinstance(expression, node_type):
            return statement
    # TruncateTable has moved between sqlglot majors; look it up by name so an
    # upgrade degrades to UNKNOWN instead of raising.
    truncate = getattr(exp, "TruncateTable", None)
    if truncate is not None and isinstance(expression, truncate):
        return TRUNCATE
    return UNKNOWN


def _table_name(node, exp) -> str:
    if node is None:
        return ""
    if isinstance(node, exp.Table):
        parts = [p for p in (node.text("db"), node.name) if p]
        return ".".join(parts)
    inner = node.find(exp.Table) if hasattr(node, "find") else None
    return _table_name(inner, exp) if inner is not None else ""


def _written_tables(expression, statement: str, exp) -> tuple[str, ...]:
    if statement == TRUNCATE:
        targets = expression.args.get("expressions") or []
        return _dedupe(_table_name(t, exp) for t in targets)
    if statement in (UPDATE, DELETE, INSERT, MERGE, CREATE, ALTER, DROP):
        name = _table_name(expression.this, exp)
        if statement == DELETE and not name:
            # DELETE FROM a, b USING ... keeps its targets in `tables`.
            targets = expression.args.get("tables") or []
            return _dedupe(_table_name(t, exp) for t in targets)
        return _dedupe((name,))
    return ()


def _all_tables(expression, exp) -> tuple[str, ...]:
    return _dedupe(_table_name(t, exp) for t in expression.find_all(exp.Table))


def _written_columns(expression, statement: str, exp) -> tuple[str, ...]:
    if statement == UPDATE:
        names = []
        for assignment in expression.args.get("expressions") or []:
            column = assignment.this if hasattr(assignment, "this") else None
            if isinstance(column, exp.Column):
                names.append(column.name)
            elif isinstance(column, exp.Identifier):
                names.append(column.name)
        return _dedupe(names)
    if statement == INSERT:
        target = expression.this
        if isinstance(target, exp.Schema):
            return _dedupe(
                c.name for c in target.expressions if isinstance(c, exp.Identifier | exp.Column)
            )
    return ()


def _has_subquery(expression, exp) -> bool:
    for node in expression.find_all(exp.Select):
        if node is not expression:
            return True
    return bool(list(expression.find_all(exp.Subquery)))


def _is_tautology(where, exp) -> bool:
    """Detect `WHERE 1 = 1` and friends -- a filter that filters nothing."""
    if where is None:
        return False
    condition = where.this
    if isinstance(condition, exp.Boolean) and condition.this is True:
        return True
    if isinstance(condition, exp.EQ):
        left, right = condition.left, condition.right
        if isinstance(left, exp.Literal) and isinstance(right, exp.Literal):
            return left.name == right.name
    return False


def _dedupe(values) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return tuple(seen)
