# ctrlz — Specification

**Status:** all four phases delivered
**Author:** Praveen Anandhanathan
**Co-author:** Claude

---

## How to read this document

Every section has two layers:

> 🟢 **In plain terms** — what this means if you have never written SQL.

…followed by the technical detail. Skip the green boxes if you want the
engineering; read only the green boxes if you want the shape of the thing.

---

## 1. The problem

> 🟢 **In plain terms**
> A database is the filing cabinet a company runs on — customers, orders,
> salaries. People change it by typing instructions. One mistyped instruction
> can wipe or corrupt thousands of records in under a second, and unlike a Word
> document, there is no Ctrl+Z. Today the recovery path is "restore last night's
> backup and lose everything since." We are building the Ctrl+Z.

Databases cannot reverse arbitrary committed SQL. The information needed to
invert a statement — the values that existed *before* it ran — is gone the
moment it commits, unless something recorded it in advance.

Existing recovery mechanisms are administrative, not developer-facing:
point-in-time recovery, Oracle Flashback, temporal tables, CDC pipelines. All
require an operator, a maintenance window, or a full restore. None gives a
developer a reviewable "undo this specific mistake" button.

**What we are building:** a change-capture and reversal layer that records
enough per-row information to safely invert a change, tells the user honestly
whether that inversion is still safe, and refuses when it is not.

---

## 2. Users and use cases

| User | Situation | What they need |
|---|---|---|
| Developer on a staging DB | Ran `UPDATE` without `WHERE` | Undo it in seconds, no ticket |
| Support engineer | Fixed 200 records, got the filter wrong | See exactly what changed, revert the wrong subset |
| Data engineer | Backfill script misfired | Reverse one batch without touching later work |
| Team lead | Needs to know who changed production data | Attributed, queryable history |
| Compliance / audit | Needs a record of data changes | Tamper-evident, exportable log |

**Primary environment:** development, staging, and controlled production
maintenance. This is not positioned as an always-on production write-path
component (see §9, Non-goals).

---

## 3. What already exists (v0.1, merged)

> 🟢 **In plain terms**
> The engine already works. It watches the database from the inside, writes
> down every row before and after it changes, and can put things back. What is
> missing is the layer that *stops* the mistake before it happens, and the
> ability to see the history across a whole team.

Delivered and tested against a real PostgreSQL 16 and SQLite:

- **Capture:** row-level `AFTER` triggers storing `to_jsonb(OLD)` / `to_jsonb(NEW)`
  in the same transaction as the change.
- **Reversal:** type-safe inverse application (`jsonb_populate_record` on
  Postgres, bound parameters on SQLite).
- **Trust contract:** per-row verdicts — `clean` / `drifted` / `missing` /
  `occupied` / `blocked` — computed before undo is offered.
- **Foreign-key ordering** with a savepoint retry loop; sequence resync.
- **Guardrails:** regex-based pre-flight (`preflight.py`), explicitly
  non-load-bearing.
- **CLI + Python API**; 63 tests passing across both engines.

**Known gaps this spec addresses:** the guardrails cannot really parse SQL;
there is no way to enforce policy for clients that do not use the CLI; changes
are not attributed to a human; history is per-database, not per-team.

---

## 4. The core principle: intent vs effect

> 🟢 **In plain terms**
> There are two different questions: *"what did the person ask for?"* and
> *"what actually happened?"* They are not the same. Someone asks to delete one
> customer; the database also silently deletes that customer's twelve orders,
> because it was told to keep things tidy. If you only record the request, you
> lose the orders forever. So we record the request in one place and the actual
> effects in another, and never confuse the two.

This principle decides the entire architecture:

| | **Intent** | **Effect** |
|---|---|---|
| What it is | the statement the user submitted | the rows the database actually changed |
| Where it is visible | client, gateway, proxy — *before* execution | inside the database, *during* the transaction |
| Used for | policy, blocking, risk scoring, attribution | undo, diffs, conflict detection |
| Consequence of getting it wrong | a missed warning | **silent data loss** |

Measured evidence from the v0.1 test suite:

```sql
DELETE FROM customers WHERE id <= 3;
```

- Rows an intent-layer analysis can see: **3** (customers)
- Rows the database actually changed: **6** (3 customers + 3 cascaded orders)

A system that builds its undo from intent restores 3 rows, loses 3, and reports
success. Therefore:

> **Rule 1.** Undo correctness may never depend on parsing SQL.
> **Rule 2.** Before/after images are written in the same transaction as the
> change they describe, in the same database.
> **Rule 3.** Any component that can be bypassed must never be the only thing
> standing between a user and unrecoverable data loss.

---

## 5. Target architecture

> 🟢 **In plain terms**
> Three layers. A **gatekeeper** at the door that reads what you are about to do
> and can stop you. A **black-box recorder** inside the database that writes
> down everything that actually happened — it cannot be switched off or walked
> around. And a **logbook** that collects records from many databases so a team
> can search them.

```
        Any client: psql · DBeaver · VS Code · BI tool · application
                                  │
                    ┌─────────────▼──────────────┐
                    │  LAYER 1 — GATEWAY         │   sees INTENT
                    │  · SQL analysis (parser)   │   bypassable by design
                    │  · policy engine           │   fails open, never
                    │  · risk scoring            │     blocks recovery
                    │  · actor attribution       │
                    └─────────────┬──────────────┘
                                  │  wire protocol, unmodified
                    ┌─────────────▼──────────────┐
                    │  LAYER 2 — CAPTURE         │   sees EFFECT
                    │  · in-database triggers    │   NOT bypassable
                    │  · before/after images     │   same transaction
                    │  · change log              │   same database
                    │  · inverse application     │
                    └─────────────┬──────────────┘
                                  │  async, best-effort
                    ┌─────────────▼──────────────┐
                    │  LAYER 3 — CONTROL PLANE   │   replica, never
                    │  · team history            │     the source of truth
                    │  · retention · audit       │
                    └────────────────────────────┘
```

**Why the gateway is not the capture point.** A proxy sees the statement, not
its consequences (§4). It also sits in the write path: if it is required for
capture, then every outage of the proxy is an outage of the database, and every
client that connects around it silently loses protection. Capture belongs where
the writes land.

**Why the gateway is still worth building.** Blocking a bad statement is worth
more than reversing it, and blocking must happen *before* the database sees it.
The gateway is also the only place that knows *who* is connected and from where.
It gives every client — including ones we will never write a plugin for — policy
and attribution with zero code changes.

**Layer 3 is a replica.** Shipping history to a central store crosses a
transaction boundary and can fail. The in-database log remains authoritative;
the control plane is a queryable copy.

---

## 6. Functional requirements

### FR-1 — SQL analysis (Layer 1)

> 🟢 Read the instruction properly instead of pattern-matching the text, so the
> warnings are accurate and hard to fool.

Replace regex pre-flight with a real parser behind a stable interface.

- **FR-1.1** `Analysis` result exposing: statement kind, target tables, whether
  a row-restricting predicate exists, columns written, presence of subqueries /
  CTEs / joins, and a `parse_confidence` field.
- **FR-1.2** Two backends behind one interface:
  - `sqlglot` (pure Python, multi-dialect) — **default**
  - `pglast` (real PostgreSQL grammar) — optional, higher fidelity for Postgres
  - `regex` — retained as the always-available fallback
- **FR-1.3** Parse failure must **degrade to the regex fallback and lower
  `parse_confidence`**, never raise, never silently pass.
- **FR-1.4** Analysis must be pure and side-effect free (no DB round-trip), so
  it can run in the gateway hot path.

### FR-2 — Policy engine (Layer 1)

> 🟢 A house-rules file. "Nobody deletes everything from a table." "Warn if more
> than 1,000 rows change." Rules are written down, versioned, and reviewable.

- **FR-2.1** Declarative rules in `ctrlz.policy.yaml`, version-controlled.
- **FR-2.2** Built-in rules: unfiltered `UPDATE`/`DELETE`; `TRUNCATE`;
  destructive DDL; writes to untracked (unprotected) tables; writes to tables
  matching a deny-list; `WHERE 1=1`.
- **FR-2.3** Three outcomes per rule: `allow` · `warn` · `block`.
- **FR-2.4** Rules can be scoped by table pattern, actor, and environment.
- **FR-2.5** A **risk score** (0–100) aggregated from matched rules, surfaced to
  clients and stored in history. The score **warns by default and never blocks**
  unless a team sets `block_on_risk: true`. Named rules (unfiltered write,
  `TRUNCATE`) still block on their own merit.
  > 🟢 A new install never refuses work on day one just because a statement
  > *looks* risky. Teams turn that up deliberately, in a file they can review.
  > A tool that gets uninstalled protects nobody.
- **FR-2.6** Overrides must be explicit, recorded, and attributed — never silent.

### FR-3 — Attribution (Layers 1 + 2)

> 🟢 Every change is signed. Who, from which machine, for which ticket.

- **FR-3.1** Capture an actor context: user, host, application, and optional
  ticket/PR reference.
- **FR-3.2** Propagate it into the in-database change log so the *database's own
  record* carries it — via `SET LOCAL ctrlz.*` on Postgres, the current-op table
  on SQLite.
- **FR-3.3** Attribution must survive the gateway being bypassed: a direct
  `psql` connection still records OS user and application name.

### FR-4 — Gateway (Layer 1)

> 🟢 A checkpoint you point your database tools at instead of the database. It
> passes everything through untouched, except the dangerous things, which it
> stops and explains.

- **FR-4.1** Speak the PostgreSQL wire protocol; accept client connections and
  proxy to the upstream server.
- **FR-4.2** Intercept SQL text at `Query` (simple protocol) and `Parse`
  (extended protocol); pass every other message through byte-for-byte.
- **FR-4.3** On `block`: return a well-formed `ErrorResponse` the client renders
  natively, with the rule name and how to override. Do **not** drop the
  connection.
- **FR-4.4** Inject attribution into the session before the first statement.
- **FR-4.5** **Fail-open on gateway internal error** — a bug in our analysis
  must never make the database unreachable. Log it, pass the query through.
- **FR-4.6** Support authentication passthrough; support TLS to the upstream.
- **FR-4.7** Overhead budget: **< 1 ms p99** added latency per statement for
  analysis (measured, not asserted).
- **FR-4.8** A **Python SDK wrapper** (DB-API 2.0 connection/cursor proxy, plus
  a SQLAlchemy event hook) applying the same policy for applications that cannot
  re-point their connection string.
  > 🟢 Two doors to the same checkpoint. Most tools can simply be told to
  > connect somewhere else; some applications cannot, so they get a small
  > library that does the same job from the inside.

  The wrapper and the gateway **must share one policy evaluation path** — two
  implementations of the rules would eventually disagree, and a safety rule that
  is true in one place and false in another is worse than no rule.

### FR-5 — MySQL engine (Layer 2)

> 🟢 Prove the design is not secretly Postgres-shaped by making it work on a
> second, different database.

- **FR-5.1** Implement the existing `Engine` interface for MySQL 8.
- **FR-5.2** Trigger-based capture with `JSON_OBJECT` row images.
- **FR-5.3** All existing behavioural tests must pass unmodified against MySQL.
- **FR-5.4** Any interface change forced by MySQL is a **finding**, to be
  documented — that is the point of the exercise.

### FR-6 — Control plane (Layer 3)

> 🟢 One searchable place for the whole team's history.

- **FR-6.1** Ship operations and change metadata from each database to a central
  store, asynchronously and idempotently.
- **FR-6.2** Cross-database `ctrlz log` with filters (actor, table, time, risk).
- **FR-6.3** Retention policy enforcement.
- **FR-6.4** Undo is **always executed against the source database**, never
  orchestrated from the replica.

---

## 7. Non-functional requirements

| ID | Requirement | Measured how |
|---|---|---|
| NFR-1 | Capture overhead ≤ 2× baseline write latency on tracked tables | benchmark in CI |
| NFR-2 | Gateway analysis adds < 1 ms p99 | benchmark in CI — **partially met, see below** |
| NFR-3 | Gateway failure never blocks database access | fault-injection test |
| NFR-4 | Zero client-side changes required to use the gateway | integration test with real `psql` |
| NFR-5 | No credentials, row values, or connection strings in logs | test asserts redaction |
| NFR-6 | Undo is idempotent — undoing twice is refused, not applied twice | existing test |
| NFR-7 | Every engine passes the identical behavioural suite | CI matrix |
| NFR-8 | Python 3.10+; no mandatory C-extension dependency | `pip install ctrlz-sql` on clean env |

### NFR-2, measured

> 🟢 **In plain terms** — we promised the checkpoint would add less than a
> thousandth of a second to a query. For the same query asked twice it adds
> about a millionth. For a query it has never seen before, using the default
> parser, the slowest one in a hundred takes a bit *over* the promise. We are
> writing that down rather than quietly moving the goalposts.

The budget was asserted for a long time and never reported, which meant the
only thing CI could tell you was that nothing had regressed by an order of
magnitude. Now that the numbers are published, they say this (one CI runner,
`sqlglot` default backend):

| measurement | median | p99 | verdict |
|---|---:|---:|---|
| analysis, cold, `sqlglot` | ~0.48 ms | ~1.3 ms | **over the 1 ms budget** |
| analysis, cold, `regex` | ~0.03 ms | ~0.06 ms | 18× inside |
| what the gateway adds per statement | ~0.0013 ms | ~0.002 ms | ~500× inside |

The last row is the one NFR-2 was written about: analysis is pure and therefore
memoised, and real traffic repeats itself constantly — ORMs, dashboards, health
checks. But a first look at a novel statement does exceed the stated p99 with
the default backend, and the requirement does not say "amortised".

Two honest options, neither taken unilaterally: restate NFR-2 to describe the
per-statement cost the gateway actually adds, or change the default backend to
`regex` and accept lower analysis confidence. The measurement is published on
every build either way.

---

## 8. The trust contract

> 🟢 The tool never says "you can undo this" unless it actually can. If someone
> else has touched the same records since, it stops and shows you, rather than
> quietly overwriting their work.

| Verdict | Meaning | Undo behaviour |
|---|---|---|
| `clean` | row is exactly as we left it | reversed |
| `drifted` | someone changed it since capture | **refused** unless `--allow-conflicts` |
| `missing` | row already gone | skipped (not an error) |
| `occupied` | another row took that key | **always skipped**, never overwritten |
| `blocked` | operation not fully captured | refused outright |

Two invariants that must survive every change in this spec:

1. **Drift is enforced inside the inverse statement**, not only as a pre-check —
   a change landing between preview and apply matches zero rows instead of
   clobbering.
2. **Partial capture is never partially applied.** An operation that exceeded
   the capture limit is reported as not-undoable, not half-restored.

---

## 9. Non-goals

> 🟢 Things we are deliberately *not* promising, because a safety tool that
> overstates what it covers is worse than no safety tool.

- **DDL undo.** Reversing `DROP COLUMN` requires snapshotting the column's data,
  turning a fast schema change into a slow one. We warn and stay out of the way.
- **`TRUNCATE` capture.** It does not fire row triggers. Blocked, with a
  suggestion to use `DELETE`.
- **Replacing backups.** This is not point-in-time recovery and must never be
  described as such.
- **Always-on high-throughput production capture.** Capture doubles row writes.
  Documented as a measured trade-off, not switched on blindly.
- **Database branching / sandboxes.** A different product.
- **Undo of cascading deletes on MySQL.** InnoDB performs referential actions
  without firing triggers, so the rows it removes are never captured. ctrlz
  detects the situation and refuses; it does not attempt to reconstruct what
  the database did out of sight. Measured and bounded in
  [`tasks-phase3.md`](./tasks-phase3.md).
- **Undo orchestrated from the control plane.** The hub is a replica and can be
  stale; reversing an operation means connecting to the database it happened
  in.
- **Making the gateway mandatory.** It is optional by construction (Rule 3).

---

## 10. Acceptance criteria

The specification is satisfied when all of the following hold:

1. A `DELETE` issued from an **unmodified `psql`** through the gateway is
   blocked with a native error message, and the same statement issued directly
   to the database is still fully captured and undoable.
2. Policy rules load from `ctrlz.policy.yaml`; a rule change alters behaviour
   with no code change.
3. `ctrlz log` shows the actor and risk score for every operation.
4. The full behavioural test suite passes unchanged on PostgreSQL, SQLite and
   MySQL.
5. Killing the gateway mid-session does not prevent database access, and does
   not lose any already-captured history.
6. Benchmarks for NFR-1 and NFR-2 run in CI and publish numbers.

---

## 11. Risks and open questions

| Risk | Impact | Mitigation |
|---|---|---|
| Wire-protocol edge cases (COPY, replication, cursors) | gateway corrupts a session | pass through everything not explicitly understood; fuzz against real clients |
| Parser disagrees with the server | wrong warning | `parse_confidence`; never load-bearing |
| Capture write amplification | production write latency | benchmark; per-table opt-in; documented limits |
| MySQL trigger limitations (no `AFTER` on some engines, no DDL triggers) | FR-5 partially unmet | treat as a finding; document rather than paper over |
| Central store becomes a perceived source of truth | undo run against stale data | undo only ever executes against the source DB (FR-6.4) |

## 12. Decisions taken at review

| # | Question | Decision | Consequence |
|---|---|---|---|
| D-1 | Scope of the first pull request | **Phase 1 only** (policy core) | Tighter review; the gateway lands as a second PR. Nothing changes for non-CLI clients until Phase 2 |
| D-2 | Gateway form | **Wire proxy + Python SDK wrapper** (FR-4.8) | ~40% more Phase 2 surface; both paths must share one policy evaluator |
| D-3 | Risk-score behaviour | **Warn by default, block on opt-in** (FR-2.5) | Named rules still block; `block_on_risk` is off in the shipped default policy |
| D-4 | Third engine | **MySQL 8** (FR-5) | Testable in our CI environment. SQL Server rejected: no server or client available here, so it would ship untested — which this spec forbids |

> 🟢 These four were genuine forks in the road. They are written down with their
> consequences so that in six months nobody has to guess why the thing is shaped
> the way it is.
