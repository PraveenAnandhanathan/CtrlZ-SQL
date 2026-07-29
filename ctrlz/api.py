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

from .actor import CLI, LIBRARY, Actor
from .engines.base import Engine
from .errors import ConfigError, PreflightBlocked
from .model import ExecutionResult, Operation, Undoability, UndoResult
from .policy import Context, Decision, PolicyEngine, Policy, load_policy

DEFAULT_CONFIRM_OVER = 100


def connect(
    dsn: Optional[str] = None,
    policy: Optional[Policy] = None,
    actor: Optional[Actor] = None,
    environment: Optional[str] = None,
) -> "Toolkit":
    """Open a toolkit against a database URL.

    Accepts ``postgresql://...``, ``mysql://...``, ``sqlite:///path.db``, or a
    bare path to a SQLite file. Falls back to ``$CTRLZ_DSN``.
    """
    dsn = dsn or os.environ.get("CTRLZ_DSN")
    if not dsn:
        raise ConfigError(
            "no database given -- pass --dsn or set CTRLZ_DSN "
            "(e.g. postgresql://user@host/db or sqlite:///app.db)"
        )
    return Toolkit(
        _engine_for(dsn), policy=policy, actor=actor, environment=environment
    )


def _engine_for(dsn: str) -> Engine:
    scheme = urlparse(dsn).scheme.lower()
    if scheme in ("postgres", "postgresql", "psql"):
        return _open("postgres", dsn, "psycopg2", "ctrlz-sql[postgres]")
    if scheme in ("mysql", "mariadb"):
        return _open("mysql", dsn, "pymysql", "ctrlz-sql[mysql]")
    if scheme == "sqlite":
        # sqlite:///relative.db and sqlite:////absolute/path.db, per the usual
        # convention: exactly one slash belongs to the authority separator.
        rest = dsn.split(":", 1)[1]
        rest = rest[2:] if rest.startswith("//") else rest
        path = rest[1:] if rest.startswith("/") else rest
        return _sqlite(path or ":memory:")
    if scheme == "file":
        return _sqlite(dsn[len("file://"):])
    if scheme == "":
        return _sqlite(_bare_path(dsn))
    raise ConfigError(f"unsupported database URL scheme: {scheme!r}")


#: Suffixes that make a scheme-less string recognisable as a SQLite file.
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")


def _bare_path(dsn: str) -> str:
    """Accept ``app.db`` as a SQLite path -- but only when it looks like one.

    A scheme-less DSN used to be treated as a SQLite path unconditionally, so a
    mistyped or unexpanded connection string quietly created an empty local
    database instead of failing. Everything then reported success: `init` said
    it had installed, `doctor` said `initialized: yes`, and the production
    database the user meant was never protected at all.

    A tool whose entire purpose is protecting data must not have a failure mode
    where it appears to be working and is not. So the convenience shorthand now
    has to earn it: a path separator, a SQLite suffix, `:memory:`, or a file
    that already exists. Anything else is more likely a mistake than an intent,
    and is refused by name.
    """
    candidate = dsn.strip()
    looks_like_a_path = (
        candidate == ":memory:"
        or os.sep in candidate
        or "/" in candidate
        or candidate.lower().endswith(SQLITE_SUFFIXES)
        or os.path.exists(candidate)
    )
    if not looks_like_a_path:
        raise ConfigError(
            f"{dsn!r} is not a database URL, and does not look like a path to a "
            f"SQLite file, so ctrlz will not create one under that name.\n"
            f"  For SQLite, be explicit:  sqlite:///{candidate or 'app'}.db\n"
            f"  For a server, include the scheme:  "
            f"postgresql://user@host/db  or  mysql://user@host/db"
        )
    return candidate


def _open(engine: str, dsn: str, module: str, extra: str) -> Engine:
    """Open an engine, turning a missing driver into an answerable message.

    Postgres and MySQL drivers are optional extras, so the most likely first
    run for a Postgres user is the one where the driver is absent. A traceback
    ending in ModuleNotFoundError is a true statement of the problem and a
    useless statement of the remedy.

    Both the import and the construction are wrapped, because the two engines
    do not agree on when they reach for their driver: Postgres imports psycopg2
    at module level, MySQL imports pymysql inside __init__. Covering only the
    import would have left MySQL users with the traceback this exists to
    prevent -- which is exactly what happened until a test said so.
    """
    try:
        if engine == "postgres":
            from .engines.postgres import PostgresEngine

            return PostgresEngine(dsn)
        from .engines.mysql import MySQLEngine

        return MySQLEngine(dsn)
    except ImportError as exc:
        if module not in str(exc):
            raise
        raise ConfigError(
            f"{engine} support needs the {module!r} driver, which is not "
            f"installed.\n  Install it with:  pip install '{extra}'"
        ) from exc


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
    """Guardrails and history on top of an engine.

    **Not thread-safe.** A Toolkit holds one database connection, and the
    drivers underneath it (psycopg2, sqlite3, PyMySQL) do not support
    concurrent use of a single connection. Give each thread its own
    ``connect()``; they are cheap, and sharing one would corrupt the protocol
    state rather than merely serialise.

    The gateway is unaffected -- it opens a connection per client and never
    uses a Toolkit -- and so is the SDK wrapper, which guards whatever
    connection the application already had.
    """

    def __init__(
        self,
        engine: Engine,
        policy: Optional[Policy] = None,
        actor: Optional[Actor] = None,
        environment: Optional[str] = None,
    ):
        self.engine = engine
        self.policy = policy if policy is not None else load_policy()
        self.policy_engine = PolicyEngine(self.policy)
        self.actor = actor or Actor.resolve(channel=LIBRARY)
        self.environment = environment or os.environ.get("CTRLZ_ENVIRONMENT", "default")

    def check(self, sql: str) -> Decision:
        """Judge a statement without running it."""
        return self.policy_engine.evaluate_sql(sql, context=self._context())

    def _context(self) -> Context:
        return Context.build(
            tracked=[name for name, _ in self.tracked()],
            environment=self.environment,
            actor=self.actor.user,
        )

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
        decision = self.policy_engine.evaluate_sql(sql, context=self._context())
        if decision.blocked and not force:
            raise PreflightBlocked("; ".join(decision.blockers) or decision.explain())

        threshold = DEFAULT_CONFIRM_OVER if confirm_over is None else confirm_over

        def decide(rowcount: int) -> bool:
            if confirm is None or threshold < 0 or rowcount <= threshold:
                return True
            return confirm(rowcount, sql)

        session = dict(self.actor.as_settings())
        session["ctrlz.risk"] = str(decision.risk)
        # A statement that ran despite a block was forced; record that rather
        # than the verdict we would have given, so the history shows what
        # actually happened.
        session["ctrlz.policy_outcome"] = (
            "forced" if decision.blocked and force else decision.outcome
        )

        result = self.engine.execute(
            sql, label=label, dry_run=dry_run, decide=decide, session=session
        )
        result.warnings = decision.warnings + result.warnings
        if decision.blocked and force:
            result.warnings.insert(
                0, f"guardrail overridden with --force: {'; '.join(decision.blockers)}"
            )
        result.decision = decision
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
            "target": self.engine.describe_target(),
            "initialized": self.engine.is_initialized(),
            "caveats": list(self.engine.caveats),
            "policy_source": self.policy.source,
            "policy_rules": len(self.policy.rules),
            "block_on_risk": self.policy.block_on_risk,
            "actor": self.actor.describe(),
            "environment": self.environment,
        }
        if info["initialized"]:
            info["schema_version"] = getattr(self.engine, "schema_version", lambda: 0)()
            risks = getattr(self.engine, "cascade_risks", None)
            if risks is not None:
                tracked_names = {n.lower() for n, _ in self.tracked()}
                info["cascade_risks"] = {
                    parent: sorted({e["child"] for e in entries})
                    for parent, entries in risks().items()
                    if parent in tracked_names
                }
            tracked = self.tracked()
            all_tables = self.engine.tables()
            info["tracked"] = tracked
            info["untracked"] = sorted(
                set(all_tables) - {name for name, _ in tracked}
            )
            info["operations"] = len(self.engine.operations(limit=1000))
        return info
