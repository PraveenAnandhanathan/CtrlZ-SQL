"""Deprecated. Use :mod:`ctrlz.analysis` and :mod:`ctrlz.policy` instead.

This was the original guardrail: a handful of regular expressions run over a
statement before it executed. It has been replaced by real parsing behind
:func:`ctrlz.analysis.analyze`, and by a rulebook that lives in
``ctrlz.policy.yaml`` rather than in this file.

The module survives as a thin shim so that existing imports keep working. It
now delegates to the regex analysis backend, which is the same logic in its
new home, so there is one implementation rather than two drifting copies.

Nothing here was ever load-bearing for undo correctness, and that has not
changed. Undo is built from row images captured by the database itself.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

from .analysis import analyze
from .analysis.backends.regex_backend import (  # re-exported for compatibility
    leading_keyword,
    normalize,
)
from .analysis.model import DDL_STATEMENTS, WRITE_STATEMENTS
from .policy import Context, PolicyEngine

__all__ = [
    "Preflight",
    "inspect",
    "is_ddl",
    "is_dml",
    "leading_keyword",
    "normalize",
]


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


def is_dml(sql: str) -> bool:
    return analyze(sql).statement in WRITE_STATEMENTS


def is_ddl(sql: str) -> bool:
    return analyze(sql).statement in DDL_STATEMENTS


def inspect(sql: str, tracked: set[str] | None = None) -> Preflight:
    """Look at a statement and report anything alarming about it.

    Kept for compatibility. New code should call
    ``ctrlz.policy.evaluate_sql`` (or ``Toolkit.check``), which returns a
    richer verdict including a risk score and the rule that produced it.
    """
    warnings.warn(
        "ctrlz.preflight.inspect is deprecated; use ctrlz.policy.evaluate_sql "
        "or Toolkit.check instead",
        DeprecationWarning,
        stacklevel=2,
    )
    # Deliberately *not* pinned to the regex backend. Pinning it would preserve
    # the old mechanism at the cost of the old outcome: the shipped rulebook
    # only blocks an unfiltered write when it is confident in the reading, so a
    # regex-only verdict downgrades "DELETE FROM users" from blocked to warned.
    # Callers of this function care whether `blocked` is True, not which parser
    # decided it, and quietly weakening a safety default for the sake of
    # fidelity to an implementation detail would be the wrong trade.
    decision = PolicyEngine().evaluate_sql(
        sql, context=Context.build(tracked=tracked or ())
    )
    return Preflight(
        sql=sql,
        keyword=decision.analysis.statement,
        blockers=list(decision.blockers),
        warnings=list(decision.warnings),
    )
