"""The rulebook: what is allowed, what is warned about, what is refused.

    from ctrlz.policy import evaluate_sql

    decision = evaluate_sql("DELETE FROM users", tracked=["public.users"])
    decision.outcome     # 'block'
    decision.risk        # 90
    print(decision.explain())

Rules live in `ctrlz.policy.yaml`, so changing one is a reviewable diff rather
than a release. The shipped defaults are in `defaults.yaml` next to this file.

Like `ctrlz.analysis`, nothing in `ctrlz.engines` may import this package:
policy decides whether a statement runs, never whether an undo is correct.
"""

from .engine import PolicyEngine, evaluate_sql, merge_decisions
from .loader import (
    DEFAULTS_PATH,
    POLICY_FILENAMES,
    find_policy_file,
    load_defaults,
    load_file,
    load_policy,
    parse,
)
from .model import (
    ALLOW,
    BLOCK,
    WARN,
    Condition,
    Context,
    Decision,
    Policy,
    Rule,
    RuleMatch,
    Scope,
)

__all__ = [
    "ALLOW",
    "WARN",
    "BLOCK",
    "Condition",
    "Context",
    "Decision",
    "Policy",
    "PolicyEngine",
    "Rule",
    "RuleMatch",
    "Scope",
    "DEFAULTS_PATH",
    "POLICY_FILENAMES",
    "evaluate_sql",
    "find_policy_file",
    "load_defaults",
    "load_file",
    "load_policy",
    "merge_decisions",
    "parse",
]
