"""The other door: policy for applications that cannot be re-pointed.

Most tools can simply be told to connect somewhere else, which is what the
gateway is for. Some applications cannot -- the DSN is baked into a deployment,
or the traffic never leaves the process. They get a small wrapper instead.

    import psycopg2
    from ctrlz.sdk import guard

    conn = guard(psycopg2.connect(DSN))
    conn.cursor().execute("DELETE FROM users")   # raises PreflightBlocked

The wrapper is a proxy, not a reimplementation: every attribute it does not
define is delegated to the real connection or cursor, so a driver-specific
feature keeps working.

It calls the **same** evaluator as the gateway (D2.6). Two implementations of
the same rules would drift, and a rule that holds at one door but not the other
is worse than no rule at all. A test asserts the two agree.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

from .actor import SDK, Actor
from .errors import PreflightBlocked
from .policy import BLOCK, Context, Decision, Policy, PolicyEngine

log = logging.getLogger("ctrlz.sdk")


class GuardedCursor:
    """A DB-API cursor that consults the rulebook before executing."""

    def __init__(self, cursor: Any, guard: "GuardedConnection"):
        self._cursor = cursor
        self._guard = guard

    # -- the guarded calls -------------------------------------------------

    def execute(self, operation: str, parameters: Any = None, *args, **kwargs):
        self._guard.check(operation)
        if parameters is None:
            return self._cursor.execute(operation, *args, **kwargs)
        return self._cursor.execute(operation, parameters, *args, **kwargs)

    def executemany(self, operation: str, seq_of_parameters: Iterable, *args, **kwargs):
        self._guard.check(operation)
        return self._cursor.executemany(operation, seq_of_parameters, *args, **kwargs)

    # -- everything else is the real cursor --------------------------------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __iter__(self):
        return iter(self._cursor)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)


class GuardedConnection:
    """A DB-API connection whose cursors are guarded."""

    def __init__(
        self,
        connection: Any,
        policy: Optional[Policy] = None,
        tracked: Iterable[str] = (),
        environment: str = "default",
        actor: Optional[Actor] = None,
        dialect: str = "postgres",
        on_block: Optional[Callable[[Decision], None]] = None,
    ):
        self._connection = connection
        self._engine = PolicyEngine(policy)
        self._tracked = tuple(tracked)
        self._environment = environment
        self._dialect = dialect
        self._on_block = on_block
        self.actor = actor or Actor.resolve(channel=SDK)
        #: Set True to log refusals instead of raising. Off by default: a
        #: guardrail that only writes to a log is not a guardrail.
        self.warn_only = False

    # -- policy ------------------------------------------------------------

    def check(self, sql: str) -> Decision:
        """Evaluate a statement, raising if the rulebook refuses it."""
        decision = self._engine.evaluate_sql(
            sql,
            context=Context.build(
                tracked=self._tracked,
                environment=self._environment,
                actor=self.actor.user,
            ),
            dialect=self._dialect,
        )
        if decision.outcome != BLOCK:
            for warning in decision.warnings:
                log.warning("ctrlz: %s", warning)
            return decision

        if self._on_block is not None:
            self._on_block(decision)
        if self.warn_only:
            log.warning("ctrlz would have blocked this: %s", "; ".join(decision.blockers))
            return decision

        raise PreflightBlocked("; ".join(decision.blockers) or decision.explain())

    def attribute(self) -> None:
        """Record who this connection belongs to, for the capture layer.

        Best effort. A driver or database that will not take the settings costs
        us attribution on this connection, never the connection itself.
        """
        assignments = ", ".join(
            "set_config(%s, %s, false)" for _ in self.actor.as_settings()
        )
        values: list[str] = []
        for key, value in self.actor.as_settings().items():
            values.extend([key, value])
        try:
            cursor = self._connection.cursor()
            try:
                cursor.execute(f"SELECT {assignments}", values)
            finally:
                cursor.close()
        except Exception:  # noqa: BLE001 - attribution is not worth a failure
            log.debug("could not set attribution on this connection", exc_info=True)

    # -- delegation --------------------------------------------------------

    def cursor(self, *args, **kwargs) -> GuardedCursor:
        return GuardedCursor(self._connection.cursor(*args, **kwargs), self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *exc):
        return self._connection.__exit__(*exc)


def guard(connection: Any, attribute: bool = True, **kwargs) -> GuardedConnection:
    """Wrap a DB-API connection so the rulebook applies to it.

    ``attribute`` records the actor on the connection, so changes made through
    it are attributed in ``ctrlz log`` the same way the gateway attributes
    them.
    """
    guarded = GuardedConnection(connection, **kwargs)
    if attribute:
        guarded.attribute()
    return guarded


def install_sqlalchemy_guard(
    engine: Any,
    policy: Optional[Policy] = None,
    tracked: Iterable[str] = (),
    environment: str = "default",
    dialect: str = "postgres",
) -> Callable[[], None]:
    """Apply the rulebook to every statement a SQLAlchemy engine executes.

    Returns a function that removes the listener again.

    SQLAlchemy emits plenty of internal statements (reflection, savepoints,
    ``BEGIN``); they go through the same rules as anything else, which is
    correct -- the rulebook should not have a private exemption list nobody
    can see.
    """
    from sqlalchemy import event

    checker = GuardedConnection(
        None, policy=policy, tracked=tracked, environment=environment, dialect=dialect
    )

    def before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        checker.check(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)

    def remove() -> None:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)

    return remove
