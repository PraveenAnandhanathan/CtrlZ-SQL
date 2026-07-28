"""The public Python API.

    from ctrlz import connect

    cz = connect("postgresql://localhost/app")
    cz.init()
    cz.track("public.users")

    result = cz.run("UPDATE users SET salary = 90000 WHERE id = 5", label="raise")
    report = cz.preview("last")      # what would come back, and what drifted
    cz.undo("last")
"""

from __future__ import annotations

import os
import re
from typing import Callable, Optional
from urllib.parse import urlparse

from . import preflight
from .engines.base import Engine
from .errors import ConfigError, PreflightBlocked
from .model import ExecutionResult, Operation, Undoability, UndoResult

DEFAULT_CONFIRM_OVER = 100


def connect(dsn: Optional[str] = None) -> "Toolkit":
    """Open a toolkit against a database URL.

    Accepts ``postgresql://...``, ``sqlite:///path.db``, or a bare path to a
    SQLite file. Falls back to ``$CTRLZ_DSN``.
    """
    dsn = dsn or os.environ.get("CTRLZ_DSN")
    if not dsn:
        raise ConfigError(
            "no database given -- pass --dsn or set CTRLZ_DSN "
            "(e.g. postgresql://user@host/db or sqlite:///app.db)"
        )
    return Toolkit(_engine_for(dsn))


def _engine_for(dsn: str) -> Engine:
    scheme = urlparse(dsn).scheme.lower()
    if scheme in ("postgres", "postgresql", "psql"):
        from .engines.postgres import PostgresEngine

        return PostgresEngine(dsn)
    if scheme == "sqlite":
        # sqlite:///relative.db and sqlite:////absolute/path.db, per the usual
        # convention: exactly one slash belongs to the authority separator.
        rest = dsn.split(":", 1)[1]
        rest = rest[2:] if rest.startswith("//") else rest
        path = rest[1:] if rest.startswith("/") else rest
        return _sqlite(path or ":memory:")
    if scheme in ("", "file"):
        return _sqlite(dsn[len("file://"):] if scheme == "file" else dsn)
    raise ConfigError(f"unsupported database URL scheme: {scheme!r}")


def _sqlite(path: str) -> Engine:
    from .engines.sqlite import SQLiteEngine

    return SQLiteEngine(path)


def parse_duration(text: str) -> int:
    """Turn ``30m``, ``2h``, ``7d`` or a bare number of seconds into seconds."""
    text = text.strip().lower()
    match = re.fullmatch(r"(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)?", text)
    if not match:
        raise ConfigError(f"cannot read duration {text!r} (try 30m, 2h, 7d)")
    value = int(match.group(1))
    unit = match.group(2) or "s"
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit[0]]
    return value * factor


class Toolkit:
    """Guardrails and history on top of an engine."""

    def __init__(self, engine: Engine):
        self.engine = engine

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "Toolkit":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.engine.close()

    # -- setup -------------------------------------------------------------

    def init(self) -> None:
        self.engine.initialize()

    def uninstall(self) -> None:
        self.engine.uninstall()

    def track(self, table: str, identity: Optional[list[str]] = None) -> list[str]:
        return self.engine.track(table, identity)

    def track_all(self) -> dict[str, object]:
        """Track every user table, reporting the ones that cannot be tracked."""
        from .errors import NoIdentity

        results: dict[str, object] = {}
        for table in self.engine.tables():
            try:
                results[table] = self.engine.track(table)
            except NoIdentity as exc:
                results[table] = exc
        return results

    def untrack(self, table: str) -> None:
        self.engine.untrack(table)

    def tracked(self) -> list[tuple[str, list[str]]]:
        return self.engine.tracked()

    # -- running -----------------------------------------------------------

    def run(
        self,
        sql: str,
        label: Optional[str] = None,
        dry_run: bool = False,
        force: bool = False,
        confirm_over: Optional[int] = None,
        confirm: Optional[Callable[[int, str], bool]] = None,
    ) -> ExecutionResult:
        """Execute a statement behind the guardrails.

        ``confirm`` is called with the real affected-row count while the
        transaction is still open, so the number it is given is what the
        database actually touched -- not an estimate from a query plan.
        """
        tracked = {name for name, _ in self.tracked()}
        checks = preflight.inspect(sql, tracked=tracked)
        if checks.blocked and not force:
            raise PreflightBlocked("; ".join(checks.blockers))

        threshold = DEFAULT_CONFIRM_OVER if confirm_over is None else confirm_over

        def decide(rowcount: int) -> bool:
            if confirm is None or threshold < 0 or rowcount <= threshold:
                return True
            return confirm(rowcount, sql)

        result = self.engine.execute(sql, label=label, dry_run=dry_run, decide=decide)
        result.warnings = checks.warnings + result.warnings
        return result

    # -- history -----------------------------------------------------------

    def log(
        self, limit: int = 20, include_undone: bool = True, include_undos: bool = True
    ) -> list[Operation]:
        return self.engine.operations(
            limit=limit, include_undone=include_undone, include_undos=include_undos
        )

    def preview(self, ref: str = "last") -> Undoability:
        return self.engine.assess(self.engine.resolve_op_id(ref))

    def undo(
        self, ref: str = "last", allow_conflicts: bool = False, label: Optional[str] = None
    ) -> UndoResult:
        return self.engine.undo(
            self.engine.resolve_op_id(ref), allow_conflicts=allow_conflicts, label=label
        )

    def redo(self, allow_conflicts: bool = False) -> UndoResult:
        """Undo the most recent undo."""
        return self.engine.undo(
            self.engine.resolve_op_id("last-undo"),
            allow_conflicts=allow_conflicts,
            label="redo",
        )

    def purge(self, older_than: Optional[str] = None) -> int:
        seconds = parse_duration(older_than) if older_than else None
        return self.engine.purge(seconds)

    # -- diagnostics -------------------------------------------------------

    def doctor(self) -> dict[str, object]:
        info: dict[str, object] = {
            "engine": self.engine.name,
            "initialized": self.engine.is_initialized(),
            "caveats": list(self.engine.caveats),
        }
        if info["initialized"]:
            tracked = self.tracked()
            all_tables = self.engine.tables()
            info["tracked"] = tracked
            info["untracked"] = sorted(
                set(all_tables) - {name for name, _ in tracked}
            )
            info["operations"] = len(self.engine.operations(limit=1000))
        return info
