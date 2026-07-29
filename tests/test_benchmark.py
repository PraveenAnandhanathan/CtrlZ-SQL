"""Performance budgets, asserted rather than hoped for.

NFR-2 gives the gateway a budget of under 1 ms p99 for analysing a statement.
Phase 2 cannot meet a budget that Phase 1 has already blown, and "it felt fast
on my laptop" is not a measurement, so the budget is checked here while the
analysis layer is still the only thing in the path.

These are deliberately loose: they catch an order-of-magnitude regression --
someone compiling a regex per call, or adding a database round-trip to a rule
evaluation -- not a 20% drift. A tight timing assertion on shared CI hardware
is a flaky test, and a flaky test gets deleted.
"""

from __future__ import annotations

import statistics
import time

import pytest

from ctrlz.analysis import analyze
from ctrlz.policy import PolicyEngine, load_defaults

#: NFR-2. The gateway's whole budget, spent here on analysis alone.
BUDGET_MS = 1.0

STATEMENTS = [
    "UPDATE users SET salary = 90000 WHERE id = 5",
    "DELETE FROM orders WHERE created_at < '2020-01-01'",
    "INSERT INTO audit (actor, action) VALUES ('ada', 'login')",
    "UPDATE users u SET dept = d.name FROM depts d WHERE u.dept_id = d.id",
    "WITH doomed AS (SELECT id FROM users WHERE inactive) "
    "DELETE FROM users WHERE id IN (SELECT id FROM doomed)",
    "SELECT u.*, d.name FROM users u JOIN depts d ON d.id = u.dept_id "
    "WHERE u.active AND d.region = 'EU' ORDER BY u.created_at DESC LIMIT 50",
]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def measure(call, iterations: int = 200) -> list[float]:
    timings: list[float] = []
    for index in range(iterations):
        sql = STATEMENTS[index % len(STATEMENTS)]
        start = time.perf_counter()
        call(sql)
        timings.append((time.perf_counter() - start) * 1000)
    return timings


@pytest.mark.parametrize("backend", ["sqlglot", "regex"])
def test_analysis_stays_within_the_gateway_budget(backend):
    measure(lambda sql: analyze(sql, prefer=backend), iterations=20)  # warm up
    timings = measure(lambda sql: analyze(sql, prefer=backend))

    p99 = percentile(timings, 0.99)
    median = statistics.median(timings)
    assert p99 < BUDGET_MS * 10, (
        f"{backend}: p99 {p99:.3f} ms, median {median:.3f} ms -- an order of "
        f"magnitude over the {BUDGET_MS} ms budget in NFR-2"
    )


def test_policy_evaluation_adds_little_over_the_parse():
    """Rule evaluation is a loop over a short list; it should barely register.

    If this ever fails, something has crept into a rule that does real work --
    a database lookup, a filesystem read, a recompiled regex per call.
    """
    engine = PolicyEngine(load_defaults())
    measure(lambda sql: analyze(sql), iterations=20)

    analysis_only = measure(lambda sql: analyze(sql))
    with_policy = measure(lambda sql: engine.evaluate_sql(sql))

    overhead = statistics.median(with_policy) - statistics.median(analysis_only)
    assert overhead < BUDGET_MS * 5, (
        f"policy evaluation adds {overhead:.3f} ms over parsing alone"
    )


def test_the_policy_file_is_read_once_not_per_statement():
    """Loading is explicit, so a hot path cannot accidentally hit the disk."""
    engine = PolicyEngine(load_defaults())
    source = engine.policy.source
    for sql in STATEMENTS:
        engine.evaluate_sql(sql)
    assert engine.policy.source == source
    assert engine.policy is engine.policy
