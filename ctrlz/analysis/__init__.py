"""Reading SQL.

    from ctrlz.analysis import analyze

    a = analyze("UPDATE users SET salary = 1")
    a.unfiltered      # True  -- this touches every row
    a.written_tables  # ('users',)
    a.confidence      # 0.9   -- how much to trust the above

The one invariant of this package: **``analyze`` never raises.** It is called on
whatever text a user typed, in a hot path, and a crash here would be a crash in
the thing meant to keep people safe. Every failure degrades to a weaker backend
and a lower confidence score, and says so in ``notes``.

Nothing in ``ctrlz.engines`` may import this package. Analysis is advisory;
undo correctness is not allowed to depend on it (spec.md, Rule 1). There is a
test that enforces this.
"""

from __future__ import annotations

import logging

from .backends.base import Backend
from .backends.pglast_backend import PglastBackend
from .backends.regex_backend import RegexBackend
from .backends.sqlglot_backend import SqlglotBackend
from .model import (
    CONFIDENCE_EXACT,
    CONFIDENCE_GUESS,
    CONFIDENCE_PARSED,
    CONFIDENCE_PARTIAL,
    CONFIDENCE_TEXTUAL,
    Analysis,
    merge,
)

log = logging.getLogger("ctrlz.analysis")

#: Preference order. sqlglot leads (D1.1): pure Python, so it is present on
#: every install. pglast is more accurate but optional, so it is opt-in via
#: ``prefer``. regex is last and always succeeds.
BACKENDS: tuple[type[Backend], ...] = (SqlglotBackend, PglastBackend, RegexBackend)

DEFAULT_ORDER = ("sqlglot", "regex")

__all__ = [
    "Analysis",
    "analyze",
    "analyze_script",
    "available_backends",
    "CONFIDENCE_EXACT",
    "CONFIDENCE_PARSED",
    "CONFIDENCE_PARTIAL",
    "CONFIDENCE_TEXTUAL",
    "CONFIDENCE_GUESS",
]


def available_backends() -> tuple[str, ...]:
    """Backends usable in this installation, in preference order."""
    return tuple(b.name for b in BACKENDS if b.available())


def _chain(prefer: str | None) -> list[type[Backend]]:
    by_name = {b.name: b for b in BACKENDS}
    order: list[type[Backend]] = []
    if prefer:
        chosen = by_name.get(prefer)
        if chosen is None:
            log.warning("unknown analysis backend %r; using the default order", prefer)
        elif not chosen.available():
            log.warning("analysis backend %r is not installed; falling back", prefer)
        else:
            order.append(chosen)
    for name in DEFAULT_ORDER:
        candidate = by_name.get(name)
        if candidate is not None and candidate not in order and candidate.available():
            order.append(candidate)
    if RegexBackend not in order:
        order.append(RegexBackend)  # the floor: no dependencies, always works
    return order


def analyze_script(
    sql: str, dialect: str | None = None, prefer: str | None = None
) -> tuple[Analysis, ...]:
    """Analyse every statement in ``sql``. Never raises."""
    if not isinstance(sql, str) or not sql.strip():
        return (Analysis(sql=sql if isinstance(sql, str) else "", backend="none"),)

    failures: list[str] = []
    for backend_type in _chain(prefer):
        try:
            results = backend_type().analyze_script(sql, dialect)
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            log.debug("analysis backend %s failed: %s", backend_type.name, exc)
            failures.append(f"{backend_type.name} could not parse this ({type(exc).__name__})")
            continue
        if not results:
            failures.append(f"{backend_type.name} produced no statements")
            continue
        if failures:
            # A fallback answer is worth less than a first-choice one, and the
            # user is entitled to know which one they got.
            results = [
                r.with_note(note, min(r.confidence, CONFIDENCE_TEXTUAL))
                for r in results
                for note in [" ; ".join(failures)]
            ]
        return tuple(results)

    # Unreachable in practice: RegexBackend does not raise. Belt and braces.
    return (
        Analysis(
            sql=sql,
            backend="none",
            confidence=CONFIDENCE_GUESS,
            notes=tuple(failures) or ("no analysis backend produced a result",),
        ),
    )


def analyze(sql: str, dialect: str | None = None, prefer: str | None = None) -> Analysis:
    """Analyse ``sql`` as a single reading.

    A multi-statement script collapses conservatively -- see ``model.merge``.
    Use ``analyze_script`` when you need each statement separately.
    """
    return merge(analyze_script(sql, dialect=dialect, prefer=prefer), sql)
