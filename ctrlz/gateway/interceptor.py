"""Deciding what to do with a statement on its way to the database.

One rule dominates this module: **fail open**. Any exception here logs and
forwards the statement unchanged. A bug in the checkpoint must never be able
to take the database offline, and it never needs to be able to: the recorder
inside the database is running either way, so a statement we failed to judge
is still a statement we can undo.

The evaluator is the one built in Phase 1. The gateway and the SDK wrapper
share it (D2.6) rather than each implementing the rules, because two
implementations drift and a safety rule that holds at one door but not the
other is worse than no rule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..policy import BLOCK, Context, Decision, PolicyEngine, Policy
from . import protocol
from .fingerprint import fingerprint

log = logging.getLogger("ctrlz.gateway")

#: What to do with a message.
FORWARD = "forward"
REFUSE = "refuse"


@dataclass
class Verdict:
    action: str
    decision: Optional[Decision] = None
    #: The bytes to send back to the client instead of forwarding.
    reply: bytes = b""

    @property
    def refused(self) -> bool:
        return self.action == REFUSE


class Interceptor:
    """Applies the rulebook to statements passing through."""

    def __init__(
        self,
        policy: Optional[Policy] = None,
        tracked: tuple[str, ...] = (),
        environment: str = "default",
        dialect: str = "postgres",
    ):
        self.engine = PolicyEngine(policy)
        self.environment = environment
        self.dialect = dialect
        self._tracked = tuple(tracked)
        #: Statement text is repetitive in real traffic (ORMs, dashboards),
        #: and analysis is pure, so the verdict is safe to memoise.
        self._cache: dict[tuple[str, str], Decision] = {}
        self.evaluated = 0
        self.refused = 0
        self.failed_open = 0

    def set_tracked(self, tracked: tuple[str, ...]) -> None:
        if tuple(tracked) != self._tracked:
            self._tracked = tuple(tracked)
            self._cache.clear()

    def context(self, actor: str = "") -> Context:
        return Context.build(
            tracked=self._tracked, environment=self.environment, actor=actor
        )

    # -- the hot path ------------------------------------------------------

    def inspect(self, message: protocol.Message, actor: str = "") -> Verdict:
        """Judge one message. Never raises."""
        try:
            sql = protocol.statement_of(message)
        except Exception as exc:  # noqa: BLE001 - fail open (D2.2)
            # Type, not traceback. A traceback renders the exception's message,
            # and the exceptions raised here are decoding errors that quote the
            # bytes they failed on -- which are the statement, and therefore the
            # row values (NFR-5).
            log.error(
                "could not read a statement out of a %r message (%s); forwarding",
                message.tag, type(exc).__name__,
            )
            self.failed_open += 1
            return Verdict(FORWARD)

        if sql is None or not sql.strip():
            return Verdict(FORWARD)

        try:
            decision = self._evaluate(sql, actor)
        except Exception as exc:  # noqa: BLE001 - fail open (D2.2)
            # As above: no traceback, because anything raised while judging a
            # statement is liable to quote it.
            log.error(
                "policy evaluation failed (%s); forwarding the statement",
                type(exc).__name__,
            )
            self.failed_open += 1
            return Verdict(FORWARD)

        self.evaluated += 1
        if decision.outcome != BLOCK:
            return Verdict(FORWARD, decision=decision)

        self.refused += 1
        return Verdict(REFUSE, decision=decision, reply=self._refusal(decision))

    def _evaluate(self, sql: str, actor: str) -> Decision:
        # Keyed on a fingerprint rather than the statement, so that a client
        # which interpolates its parameters -- psycopg2 does -- gets cache hits
        # instead of paying a first look on every statement. The fingerprint
        # declines to normalise anything whose literals could change the
        # verdict; see gateway/fingerprint.py for why that is not optional.
        key = (fingerprint(sql), actor)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        decision = self.engine.evaluate_sql(
            sql, context=self.context(actor), dialect=self.dialect
        )
        if len(self._cache) < 2048:
            self._cache[key] = decision
        return decision

    def _refusal(self, decision: Decision) -> bytes:
        rule = decision.decided_by.name if decision.decided_by else "policy"
        message = decision.blockers[0] if decision.blockers else "refused by ctrlz policy"
        return protocol.error_response(
            message=f"ctrlz: {message}",
            detail=(
                f"Blocked by rule '{rule}' (risk {decision.risk}/"
                f"{decision.risk_threshold}). Read by "
                f"{decision.analysis.backend}, confidence "
                f"{decision.analysis.confidence:.2f}."
            ),
            hint=(
                "Run it through `ctrlz run --force` if you are sure, or adjust "
                "the rule in ctrlz.policy.yaml."
            ),
        )
