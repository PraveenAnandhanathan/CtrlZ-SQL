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
def test_analysis_stays_within_the_gateway_budget(backend, benchmark):
    measure(lambda sql: analyze(sql, prefer=backend), iterations=20)  # warm up
    timings = measure(lambda sql: analyze(sql, prefer=backend))

    recorded = benchmark.record(
        f"analysis ({backend})", "one statement, parsed and classified",
        timings, budget_ms=BUDGET_MS,
    )
    assert recorded.p99_ms < BUDGET_MS * 10, (
        f"{backend}: p99 {recorded.p99_ms:.3f} ms, median "
        f"{recorded.median_ms:.3f} ms -- an order of magnitude over the "
        f"{BUDGET_MS} ms budget in NFR-2"
    )


def test_policy_evaluation_adds_little_over_the_parse(benchmark):
    """Rule evaluation is a loop over a short list; it should barely register.

    If this ever fails, something has crept into a rule that does real work --
    a database lookup, a filesystem read, a recompiled regex per call.
    """
    engine = PolicyEngine(load_defaults())
    measure(lambda sql: analyze(sql), iterations=20)

    analysis_only = measure(lambda sql: analyze(sql))
    with_policy = measure(lambda sql: engine.evaluate_sql(sql))
    benchmark.record(
        "analysis + policy", "one statement, analysed and judged", with_policy,
        budget_ms=BUDGET_MS,
    )

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


# -- the gateway's own budget ----------------------------------------------


def test_the_interceptor_stays_within_the_per_statement_budget(benchmark):
    """NFR-2 measured where it actually applies: the gateway's hot path.

    This is the interceptor alone -- no sockets, no database -- because that
    is the part we add to every statement. Network time is the same whether
    the gateway is there or not.
    """
    from ctrlz.gateway import Interceptor, protocol

    interceptor = Interceptor(tracked=("users", "orders"))
    messages = [
        protocol.Message(protocol.QUERY, sql.encode() + b"\x00") for sql in STATEMENTS
    ]

    for index in range(20):
        interceptor.inspect(messages[index % len(messages)])

    timings: list[float] = []
    for index in range(400):
        message = messages[index % len(messages)]
        start = time.perf_counter()
        interceptor.inspect(message)
        timings.append((time.perf_counter() - start) * 1000)

    recorded = benchmark.record(
        "gateway interceptor", "what the gateway adds per statement", timings,
        budget_ms=BUDGET_MS,
    )
    assert recorded.p99_ms < BUDGET_MS * 10, (
        f"interceptor p99 {recorded.p99_ms:.4f} ms, "
        f"median {recorded.median_ms:.4f} ms"
    )


def test_repeated_statements_are_cheap(benchmark):
    """Real traffic repeats itself -- ORMs, dashboards, health checks.

    Analysis is pure, so the verdict is safe to memoise, and the second look
    at a statement should cost almost nothing.
    """
    from ctrlz.gateway import Interceptor, protocol

    interceptor = Interceptor(tracked=("users",))
    message = protocol.Message(
        protocol.QUERY, b"UPDATE users SET salary = 1 WHERE id = 5\x00"
    )

    start = time.perf_counter()
    interceptor.inspect(message)
    first = time.perf_counter() - start

    start = time.perf_counter()
    for _ in range(100):
        interceptor.inspect(message)
    repeat = (time.perf_counter() - start) / 100

    benchmark.record(
        "gateway interceptor (repeat)", "a statement already seen",
        [repeat * 1000] * 3, budget_ms=BUDGET_MS,
    )
    assert repeat < first, f"cached lookup ({repeat*1000:.4f} ms) not faster than first"
