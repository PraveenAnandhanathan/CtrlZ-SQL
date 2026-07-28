"""Rules, and what evaluating them produces.

A rule is data, not code (design decision D1.3). Conditions are a flat set of
named fields, deliberately not an expression language: the moment a safety
rulebook needs a parser of its own, nobody can read it at a glance and nobody
can be sure what it does.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Optional

from ..analysis.model import Analysis

# What a rule can decide.
ALLOW = "allow"
WARN = "warn"
BLOCK = "block"

ACTIONS = (ALLOW, WARN, BLOCK)

#: Ordered by severity, so merging decisions is a max().
SEVERITY = {ALLOW: 0, WARN: 1, BLOCK: 2}

#: Every field a condition may name. Anything else is a load error -- a typo in
#: a safety rule must not silently disable it.
CONDITION_FIELDS = frozenset(
    {
        "statement",
        "kind",
        "has_filter",
        "filter_is_tautology",
        "unfiltered",
        "tables",
        "writes_untracked",
        "max_confidence",
        "min_confidence",
    }
)


@dataclass(frozen=True)
class Condition:
    """When a rule applies.

    Every field present must match. An absent field is not a constraint.
    """

    statement: tuple[str, ...] = ()
    kind: tuple[str, ...] = ()
    has_filter: Optional[bool] = None
    filter_is_tautology: Optional[bool] = None
    unfiltered: Optional[bool] = None
    tables: tuple[str, ...] = ()
    writes_untracked: Optional[bool] = None
    max_confidence: Optional[float] = None
    min_confidence: Optional[float] = None

    def matches(self, analysis: Analysis, context: "Context") -> bool:
        if self.statement and analysis.statement not in self.statement:
            return False
        if self.kind and analysis.kind not in self.kind:
            return False
        if self.has_filter is not None and analysis.has_filter is not self.has_filter:
            return False
        if (
            self.filter_is_tautology is not None
            and analysis.filter_is_tautology is not self.filter_is_tautology
        ):
            return False
        if self.unfiltered is not None and analysis.unfiltered is not self.unfiltered:
            return False
        if self.tables and not _any_table_matches(analysis, self.tables):
            return False
        if self.writes_untracked is not None:
            if _writes_untracked(analysis, context) is not self.writes_untracked:
                return False
        if self.max_confidence is not None and analysis.confidence > self.max_confidence:
            return False
        if self.min_confidence is not None and analysis.confidence < self.min_confidence:
            return False
        return True


@dataclass(frozen=True)
class Scope:
    """Where a rule applies. An empty scope means everywhere."""

    environments: tuple[str, ...] = ()
    actors: tuple[str, ...] = ()

    def covers(self, context: "Context") -> bool:
        if self.environments and not _glob_any(context.environment, self.environments):
            return False
        if self.actors and not _glob_any(context.actor, self.actors):
            return False
        return True


@dataclass(frozen=True)
class Rule:
    name: str
    when: Condition
    action: str = WARN
    risk: int = 0
    message: str = ""
    scope: Scope = field(default_factory=Scope)

    def applies(self, analysis: Analysis, context: "Context") -> bool:
        return self.scope.covers(context) and self.when.matches(analysis, context)


@dataclass(frozen=True)
class Context:
    """What the evaluator knows besides the statement itself."""

    tracked_tables: frozenset[str] = frozenset()
    environment: str = "default"
    actor: str = ""

    @classmethod
    def build(cls, tracked=(), environment="default", actor="") -> "Context":
        return cls(
            tracked_tables=frozenset(t.lower() for t in tracked),
            environment=environment,
            actor=actor,
        )


@dataclass(frozen=True)
class RuleMatch:
    rule: Rule
    message: str

    @property
    def name(self) -> str:
        return self.rule.name


@dataclass
class Decision:
    """The verdict on one statement, and why."""

    analysis: Analysis
    outcome: str = ALLOW
    risk: int = 0
    matched: list[RuleMatch] = field(default_factory=list)
    #: The rule that set `outcome`, and the rule that set `risk`. These can
    #: differ, and a user asking "why" deserves to be told which is which.
    decided_by: Optional[RuleMatch] = None
    scored_by: Optional[RuleMatch] = None
    risk_threshold: int = 100
    block_on_risk: bool = False

    @property
    def blocked(self) -> bool:
        return self.outcome == BLOCK

    @property
    def messages(self) -> list[str]:
        return [m.message for m in self.matched]

    @property
    def blockers(self) -> list[str]:
        return [m.message for m in self.matched if m.rule.action == BLOCK]

    @property
    def warnings(self) -> list[str]:
        return [m.message for m in self.matched if m.rule.action == WARN]

    def explain(self) -> str:
        """A human-readable account of how this verdict was reached."""
        lines = [
            f"outcome: {self.outcome}  risk: {self.risk}/{self.risk_threshold}"
        ]
        if self.decided_by:
            lines.append(f"  decided by rule '{self.decided_by.name}'")
        elif self.outcome == ALLOW:
            lines.append("  no rule objected")
        if self.scored_by:
            lines.append(
                f"  highest risk from rule '{self.scored_by.name}' ({self.scored_by.rule.risk})"
            )
        if self.risk >= self.risk_threshold and not self.block_on_risk:
            lines.append(
                "  risk is at or above the threshold, but block_on_risk is off, "
                "so this is a warning only"
            )
        lines.append(
            f"  read by {self.analysis.backend} "
            f"(confidence {self.analysis.confidence:.2f})"
        )
        for note in self.analysis.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


@dataclass
class Policy:
    """A loaded rulebook."""

    rules: list[Rule] = field(default_factory=list)
    risk_threshold: int = 70
    block_on_risk: bool = False
    version: int = 1
    source: str = "<defaults>"

    def rule(self, name: str) -> Optional[Rule]:
        for candidate in self.rules:
            if candidate.name == name:
                return candidate
        return None


# -- helpers ---------------------------------------------------------------


def _glob_any(value: str, patterns: tuple[str, ...]) -> bool:
    value = (value or "").lower()
    return any(fnmatch.fnmatch(value, p.lower()) for p in patterns)


def _any_table_matches(analysis: Analysis, patterns: tuple[str, ...]) -> bool:
    """Match a table pattern against qualified and bare names alike.

    `payments` in a rule should catch `public.payments`, because that is what
    somebody writing the rule means. Matching is case-insensitive: PostgreSQL
    folds unquoted identifiers, so `USERS` and `users` are the same table.
    """
    for table in analysis.tables:
        lowered = table.lower()
        bare = lowered.rpartition(".")[2]
        for pattern in patterns:
            pattern = pattern.lower()
            if fnmatch.fnmatch(lowered, pattern) or fnmatch.fnmatch(bare, pattern):
                return True
    return False


def _writes_untracked(analysis: Analysis, context: Context) -> bool:
    """True when the statement writes to a table with no capture triggers.

    Unknown target tables do not count: reporting "this is unprotected" when we
    simply could not read the statement would train people to ignore the
    warning.
    """
    if not analysis.is_write or not analysis.written_tables:
        return False
    for table in analysis.written_tables:
        lowered = table.lower()
        bare = lowered.rpartition(".")[2]
        known = any(
            lowered == t or t.rpartition(".")[2] == bare for t in context.tracked_tables
        )
        if not known:
            return True
    return False


def coerce_action(value: Any) -> str:
    text = str(value or WARN).strip().lower()
    if text not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}, not {value!r}")
    return text
