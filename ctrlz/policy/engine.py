"""Evaluating a rulebook against a statement.

Two decisions here shape everything the user sees.

**Risk aggregates with max(), not a sum.** A summed score is not explainable:
"why is this 84?" has no answer, and an unexplainable number in a safety tool
gets ignored. With max() the score always points at exactly one rule, and
`Decision.explain()` can name it.

**The score warns; it does not block** unless the rulebook says otherwise
(decision D-3). Rules that carry `action: block` still block on their own
merit -- a DELETE with no WHERE is refused because of what it is, not because
of what it scored.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from ..analysis import analyze_script
from ..analysis.model import Analysis, merge
from .loader import load_policy
from .model import (
    ALLOW,
    BLOCK,
    SEVERITY,
    WARN,
    Context,
    Decision,
    Policy,
    Rule,
    RuleMatch,
)


class PolicyEngine:
    """Applies one rulebook. Shared by every entry point (D2.6)."""

    def __init__(self, policy: Optional[Policy] = None):
        self.policy = policy if policy is not None else load_policy()

    # -- evaluation --------------------------------------------------------

    def evaluate(self, analysis: Analysis, context: Optional[Context] = None) -> Decision:
        """Judge one statement."""
        context = context or Context()
        decision = Decision(
            analysis=analysis,
            risk_threshold=self.policy.risk_threshold,
            block_on_risk=self.policy.block_on_risk,
        )

        for rule in self.policy.rules:
            if not rule.applies(analysis, context):
                continue
            match = RuleMatch(rule=rule, message=_render(rule, analysis))
            decision.matched.append(match)

            if SEVERITY[rule.action] > SEVERITY[decision.outcome]:
                decision.outcome = rule.action
                decision.decided_by = match
            if rule.risk > decision.risk:
                decision.risk = rule.risk
                decision.scored_by = match

        if (
            self.policy.block_on_risk
            and decision.risk >= self.policy.risk_threshold
            and decision.outcome != BLOCK
        ):
            decision.outcome = BLOCK
            decision.decided_by = decision.scored_by

        return decision

    def evaluate_sql(
        self,
        sql: str,
        context: Optional[Context] = None,
        dialect: Optional[str] = None,
        prefer: Optional[str] = None,
    ) -> Decision:
        """Judge a statement or script, given its text.

        Each statement in a script is judged on its own and the verdicts are
        merged by severity, so one dangerous statement is not diluted by the
        safe ones around it.
        """
        analyses = analyze_script(sql, dialect=dialect, prefer=prefer)
        decisions = [self.evaluate(a, context) for a in analyses]
        return merge_decisions(decisions, sql)


def merge_decisions(decisions: Sequence[Decision], sql: str) -> Decision:
    """Combine per-statement verdicts into one, taking the worst of each."""
    decisions = list(decisions)
    if not decisions:
        return Decision(analysis=Analysis(sql=sql))
    if len(decisions) == 1:
        return decisions[0]

    combined = Decision(
        analysis=merge((d.analysis for d in decisions), sql),
        risk_threshold=decisions[0].risk_threshold,
        block_on_risk=decisions[0].block_on_risk,
    )
    seen: set[str] = set()
    for decision in decisions:
        for match in decision.matched:
            # One message per rule: a script touching ten tables should not
            # print the same warning ten times.
            if match.name in seen:
                continue
            seen.add(match.name)
            combined.matched.append(match)
        if SEVERITY[decision.outcome] > SEVERITY[combined.outcome]:
            combined.outcome = decision.outcome
            combined.decided_by = decision.decided_by
        if decision.risk > combined.risk:
            combined.risk = decision.risk
            combined.scored_by = decision.scored_by
    return combined


def _render(rule: Rule, analysis: Analysis) -> str:
    """Fill a rule's message template with what we learned about the statement."""
    tables = ", ".join(analysis.written_tables or analysis.tables) or "the target table"
    try:
        text = rule.message.format(
            statement=analysis.statement,
            tables=tables,
            kind=analysis.kind,
            confidence=f"{analysis.confidence:.2f}",
            backend=analysis.backend,
        )
    except (KeyError, IndexError, ValueError):
        # A bad placeholder in a rule must not take down the check that rule
        # exists to perform.
        text = rule.message
    return " ".join(text.split())


def evaluate_sql(
    sql: str,
    tracked: Iterable[str] = (),
    environment: str = "default",
    actor: str = "",
    policy: Optional[Policy] = None,
    dialect: Optional[str] = None,
) -> Decision:
    """Convenience entry point for one-off checks."""
    engine = PolicyEngine(policy)
    context = Context.build(tracked=tracked, environment=environment, actor=actor)
    return engine.evaluate_sql(sql, context=context, dialect=dialect)
