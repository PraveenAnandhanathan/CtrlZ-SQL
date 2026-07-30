"""Performance budgets, asserted rather than hoped for.

NFR-2 budgets what the gateway adds per statement: under 1 ms p99 whether or
not it has seen the statement before, and under 2 ms on a first look at complex
analytical SQL. It is stated as cases rather than one number because cache
state moves the figure ~400x and parse difficulty moves it ~3x.
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

#: NFR-2, steady state: what the gateway adds to a statement it has judged
#: before. This is the figure a parameterised client experiences on almost
#: every statement.
BUDGET_MS = 1.0

#: NFR-2, first sight of complex analytical SQL -- a CTE, a many-table join.
#: Stated separately because the original single budget averaged cases that
#: differ by ~400x (cache state) and ~3x (parse difficulty), and an average of
#: incomparable things is not a checkable requirement.
#:
#: These statements spend tens of milliseconds in the database, so the extra
#: millisecond is not detectable; ordinary DML stays on the 1 ms budget above,
#: including on first sight, which is what clients that inline their literals
#: (psycopg2 among them) get on every statement.
COMPLEX_COLD_BUDGET_MS = 2.0

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

    # STATEMENTS is deliberately hard -- CTEs, a six-table join -- so this is
    # NFR-2's first-sight-complex case, not its steady-state one.
    budget = BUDGET_MS if backend == "regex" else COMPLEX_COLD_BUDGET_MS
    recorded = benchmark.record(
        f"analysis ({backend})", "first sight, complex SQL", timings,
        budget_ms=budget,
    )
    assert recorded.median_ms < budget, (
        f"{backend}: median {recorded.median_ms:.3f} ms, over the {budget} ms "
        f"NFR-2 allows for a first look at a statement this complex"
    )
    assert recorded.p99_ms < budget * 5, (
        f"{backend}: p99 {recorded.p99_ms:.3f} ms -- far enough over {budget} ms "
        f"to be a regression rather than a noisy machine"
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
        "analysis + policy", "first sight, complex SQL, judged", with_policy,
        budget_ms=COMPLEX_COLD_BUDGET_MS,
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


# -- NFR-1: what capture costs a write -------------------------------------


def write_cycle(execute, table: str, index: int) -> list[float]:
    """Time one insert/update/delete against `table`."""
    timings = []
    for sql in (
        f"INSERT INTO {table} (id, name, salary) VALUES ({index}, 'n', 1)",
        f"UPDATE {table} SET salary = 2 WHERE id = {index}",
        f"DELETE FROM {table} WHERE id = {index}",
    ):
        start = time.perf_counter()
        execute(sql)
        timings.append((time.perf_counter() - start) * 1000)
    return timings


def compare_writes(execute, rounds: int = 120) -> tuple[list[float], list[float]]:
    """Time both tables interleaved, a round each, alternating which goes first.

    Timing all of A and then all of B lets anything that drifts during the run
    -- page cache warming, a busy disk, CPU frequency -- land entirely on one
    side of the comparison. Measured that way here, the *untracked* table
    showed a 48 ms p99 against the tracked table's 7 ms, which is not a real
    property of triggers; it is the first run paying for a cold cache.

    Interleaving cancels drift, and swapping the order each round stops the
    table that goes first from systematically absorbing the flush.
    """
    baseline: list[float] = []
    captured: list[float] = []
    for index in range(rounds):
        if index % 2:
            baseline += write_cycle(execute, "plain", index)
            captured += write_cycle(execute, "watched", index)
        else:
            captured += write_cycle(execute, "watched", index)
            baseline += write_cycle(execute, "plain", index)
    return baseline, captured


def test_capture_overhead_is_measured_not_assumed(tmp_path, benchmark):
    """NFR-1: capture overhead <= 2x baseline write latency.

    This was in the plan's definition of done and had never been measured --
    the benchmarks covered NFR-2 alone. The spec's non-goals say plainly that
    "capture doubles row writes", which is the single number somebody weighing
    whether to switch this on actually needs, and it was an assertion rather
    than a measurement.

    The comparison is the same statements against the same schema in the same
    database, once on an untracked table and once on a tracked one, so the only
    difference between the two numbers is the trigger.
    """
    import sqlite3

    import ctrlz

    path = tmp_path / "overhead.db"
    tk = ctrlz.connect(f"sqlite:///{path}")
    columns = "(id INTEGER PRIMARY KEY, name TEXT, salary REAL)"
    tk.engine.conn.executescript(
        f"CREATE TABLE plain {columns}; CREATE TABLE watched {columns};"
    )
    tk.init()
    tk.track("watched")

    execute = tk.engine.conn.execute
    compare_writes(execute, rounds=20)                # warm up both tables

    baseline, captured = compare_writes(execute)

    benchmark.record(
        "write, untracked", "one INSERT/UPDATE/DELETE (SQLite)", baseline
    )
    recorded = benchmark.record(
        "write, capture on", "the same statement with a trigger", captured
    )

    overhead = statistics.median(captured) / statistics.median(baseline)
    recorded.note = f"{overhead:.2f}x baseline"

    # Deliberately loose, like the rest of this file: shared CI hardware makes
    # a tight ratio a flaky test, and a flaky test gets deleted. NFR-1's real
    # value is the published number, not this tripwire.
    assert overhead < 6, (
        f"capture costs {overhead:.2f}x a plain write -- NFR-1 budgets 2x, and "
        f"this is far enough past it to be a defect rather than noise"
    )

    # Capture must actually have happened, or the comparison measured nothing.
    assert tk.log(limit=5), "no operations recorded: the trigger did not fire"
    tk.close()


def test_capture_overhead_on_postgres(benchmark):
    """The same measurement where it actually matters.

    SQLite's number flatters capture: every statement is its own autocommit
    with an fsync costing milliseconds, so a trigger writing one extra row
    disappears into it. PostgreSQL's baseline write is far cheaper, which
    makes the *relative* cost of capture higher -- and PostgreSQL is the
    primary target. Publishing only the SQLite ratio would be reporting the
    friendlier of two numbers we can both measure.
    """
    import os
    import uuid

    dsn = os.environ.get("CTRLZ_TEST_PG_DSN")
    from .conftest import require

    require("postgres", dsn, "CTRLZ_TEST_PG_DSN")

    import psycopg2

    import ctrlz

    schema = f"bench_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    columns = "(id int PRIMARY KEY, name text, salary numeric)"
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
        cur.execute(f"CREATE TABLE {schema}.plain {columns}")
        cur.execute(f"CREATE TABLE {schema}.watched {columns}")

    tk = ctrlz.connect(dsn)
    tk.engine.default_schema = schema
    tk.init()
    tk.track(f"{schema}.watched")

    cursor = tk.engine.conn.cursor()

    def execute(sql: str) -> None:
        cursor.execute(sql.replace(" plain", f" {schema}.plain")
                          .replace(" watched", f" {schema}.watched"))
        tk.engine.conn.commit()

    try:
        compare_writes(execute, rounds=20)            # warm up
        baseline, captured = compare_writes(execute, rounds=80)

        benchmark.record(
            "write, untracked (pg)", "one INSERT/UPDATE/DELETE (PostgreSQL)",
            baseline,
        )
        recorded = benchmark.record(
            "write, capture on (pg)", "the same statement with a trigger",
            captured,
        )
        overhead = statistics.median(captured) / statistics.median(baseline)
        recorded.note = f"{overhead:.2f}x baseline"

        assert overhead < 6, (
            f"capture costs {overhead:.2f}x a plain write on PostgreSQL -- "
            f"NFR-1 budgets 2x"
        )
        assert tk.log(limit=5), "no operations recorded: the trigger did not fire"
    finally:
        cursor.close()
        tk.close()
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
            cur.execute("DROP SCHEMA IF EXISTS ctrlz CASCADE")
        admin.close()


def test_the_cost_when_every_statement_is_new(benchmark):
    """The workload the memoised number does not describe.

    The cache is keyed on exact statement text. psycopg2 interpolates
    parameters client-side, so `WHERE id = 5` and `WHERE id = 6` arrive as
    different statements and never share an entry -- for those clients the cold
    path *is* the hot path. That is not a hypothetical: psycopg2 is one of the
    two clients this project's own gateway tests drive.

    Restating NFR-2 around "real traffic repeats itself" without this number
    would have been true of parameterised clients and quietly false of a very
    large installed base.
    """
    from ctrlz.gateway import Interceptor, protocol

    interceptor = Interceptor(tracked=("users", "orders"))
    unique = [
        protocol.Message(
            protocol.QUERY,
            f"UPDATE users SET salary = {n} WHERE id = {n}\x00".encode(),
        )
        for n in range(400)
    ]
    for message in unique[:20]:
        interceptor.inspect(message)

    timings: list[float] = []
    for message in unique[20:]:
        start = time.perf_counter()
        interceptor.inspect(message)
        timings.append((time.perf_counter() - start) * 1000)

    recorded = benchmark.record(
        "gateway interceptor (all new)",
        "a client that inlines literals, e.g. psycopg2", timings,
        budget_ms=BUDGET_MS,
    )
    # The 1 ms budget, not the complex-SQL one: inlined-literal traffic is
    # ordinary DML, and NFR-2 holds the ordinary case to the original number
    # rather than leaning on a cache these clients never hit.
    #
    # Asserted on the median, with p99 as a looser tripwire, because that is
    # the methodology NFR-2 itself now states: runs of this suite on a busy
    # container have produced p99s of 11 ms and 27 ms for code that measures
    # 1.2 ms on a quiet one. Those are the scheduler. Writing a strict p99
    # assertion here would have contradicted the note in the spec and flaked
    # for the reason that note gives.
    assert recorded.median_ms < BUDGET_MS, (
        f"a cache-missing ordinary statement costs {recorded.median_ms:.3f} ms "
        f"at the median, over the {BUDGET_MS} ms NFR-2 allows"
    )
    assert recorded.p99_ms < BUDGET_MS * 5, (
        f"p99 {recorded.p99_ms:.3f} ms is far enough over the {BUDGET_MS} ms "
        f"budget to be a regression rather than a noisy machine"
    )
