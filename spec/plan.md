# ctrlz — Implementation Plan

**Status:** all four phases delivered
**Author:** Praveen Anandhanathan
**Co-author:** Claude
**Companion document:** [`spec.md`](./spec.md)

---

## 0. Summary

> 🟢 **In plain terms**
> The recorder inside the database already works. Now we build the gatekeeper
> at the door. We do it in four steps, and each step is useful on its own even
> if we stop there. We do the boring, invisible step first — teaching the tool
> to actually *read* SQL — because everything else depends on it.

Four phases. Each ends in a mergeable, independently valuable state.

| Phase | Deliverable | Depends on | Independently useful? |
|---|---|---|---|
| **1** | Policy core: real SQL analysis + rule engine + attribution | v0.1 | ✅ **merged** (PR #1) |
| **2** | Gateway: PostgreSQL wire proxy + SDK wrapper | Phase 1 | ✅ **merged** (PR #2) |
| **3** | MySQL engine | v0.1 | ✅ **merged** (PR #3) — see [`tasks-phase3.md`](./tasks-phase3.md) |
| **4** | Control plane: team history | Phases 1–3 | ✅ **merged** (PR #4) |

**All four phases are delivered.** 556 tests pass across SQLite, PostgreSQL 16
and MySQL 8, with five strict xfails recording the one thing MySQL genuinely
cannot do.

**Decided at review: the first pull request is Phase 1 only.** A tighter,
reviewable change. Phase 2 follows as its own PR.

> 🟢 We build the reading comprehension before we build the checkpoint that
> depends on it, and we let you review each on its own.

---

## 1. Sequencing rationale

> 🟢 Why this order and not another.

**Why the parser comes first.** The gateway's entire job is to decide whether to
let a statement through. That decision is only as good as the analysis behind
it. Building the proxy first would mean building a checkpoint staffed by someone
who cannot read.

**Why the gateway comes before MySQL** — a deliberate change from my earlier
recommendation. I previously argued the second engine should come first, because
the engine abstraction is the product and an untested abstraction is a guess.
That is still true. But the gateway is the piece that changes who can use the
tool at all: today it protects people who type `ctrlz run`, which is almost
nobody. Reach beats generality here, and Phase 3 is not blocked by Phase 2 — the
two can proceed in parallel if there is capacity.

**Why the control plane comes last.** It is the only phase with no correctness
risk and the most product risk. It should be built once we know what a team
actually wants to search for.

---

## 2. Phase 1 — Policy core

### Goal

> 🟢 Teach the tool to genuinely understand a SQL instruction, and let a team
> write down its own safety rules in a file.

Replace the regex pre-flight with real parsing behind a stable interface, add a
declarative policy engine, and record *who* made each change.

### Design decisions

**D1.1 — Parser: `sqlglot` by default, `pglast` optional.**

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| `sqlglot` | pure Python, 20+ dialects, no build step | its own grammar, not the server's | **default** |
| `pglast` | the real PostgreSQL grammar | C extension, Postgres only | **optional extra** |
| `sqlparse` | tiny | tokenizer, not a parser — cannot answer our questions | rejected |
| regex | always works | fragile | **retained as fallback** |

Rationale: NFR-8 forbids a mandatory C dependency, so the default must be pure
Python. Teams wanting maximum Postgres fidelity install `ctrlz-sql[pg-parser]`.

**D1.2 — Analysis is advisory, permanently.** The `Analysis` dataclass carries
`parse_confidence`. No undo path may import it. This is enforced by a test that
asserts `ctrlz.engines.*` does not import `ctrlz.analysis`.

> 🟢 The reader at the door can be wrong without anything being lost, because
> the recorder inside is what undo actually relies on.

**D1.3 — Policy as data, not code.** `ctrlz.policy.yaml`, version-controlled, so
a rule change is a reviewable diff rather than a release.

**D1.4 — Attribution rides the existing session-settings mechanism.** Postgres
already uses `SET LOCAL ctrlz.label`; actor fields extend the same path, so no
capture-layer redesign.

### Deliverables

- `ctrlz/analysis/` — `Analysis` model, backend interface, three backends
- `ctrlz/policy/` — rule model, YAML loader, evaluator, risk scoring
- `ctrlz/actor.py` — actor context resolution and propagation
- Schema migration: actor + risk columns on `ctrlz.operations`
- `ctrlz check` upgraded; `ctrlz policy test` added
- Tests: parser-equivalence across backends, rule evaluation, degradation on
  unparseable SQL, migration from a v0.1 database

### Risks

| Risk | Mitigation |
|---|---|
| Backends disagree on the same SQL | differential test corpus; disagreement lowers confidence |
| Schema migration breaks existing installs | versioned migration + test that upgrades a v0.1 database |
| Policy YAML becomes a config language | keep rules flat and declarative; no expressions |

### Exit criteria

Rules load from YAML and change behaviour with no code change · unparseable SQL
degrades rather than crashes · actor and risk appear in `ctrlz log` · a v0.1
database upgrades cleanly · full suite green on both engines.

---

## 3. Phase 2 — Gateway

### Goal

> 🟢 A checkpoint you point your existing database tools at. Nothing about them
> changes. Dangerous instructions get stopped with a clear explanation.

A PostgreSQL wire-protocol proxy that applies Phase 1 policy to every client.

### Design decisions

**D2.1 — Understand only what we must; proxy the rest byte-for-byte.** We
inspect `Query` (`Q`) and `Parse` (`P`) messages for SQL text. Every other
message type — including `COPY`, function calls, and replication — is relayed
untouched.

> 🟢 The checkpoint reads the label on the envelope, not the letter inside, and
> everything it does not recognise it hands over unopened.

**D2.2 — Fail open, always.** Any internal exception logs and forwards the
query. A crash in our code must never take the database offline (NFR-3),
verified by fault injection.

**D2.3 — Block with a protocol-native `ErrorResponse`, never a disconnect.**
`psql` then renders our message the way it renders any database error — no
plugin, no special client.

**D2.4 — Authentication is passed through, not intercepted.** The gateway does
not read, store, or re-issue credentials. `cleartext`/`md5`/`SCRAM` exchanges
are relayed. Consequence: no session-level user identity from SCRAM, so
attribution uses the startup packet's `user` and `application_name` — honest
about what it knows.

**D2.5 — asyncio, single process, no new runtime dependency.**

**D2.6 — The proxy and the SDK wrapper share one policy evaluator.** Both call
the same `ctrlz.policy` entry point built in Phase 1. Two implementations of the
same rules would drift, and a safety rule that holds at one door but not the
other is worse than no rule at all.

### Deliverables

- `ctrlz/gateway/` — protocol codec, connection handler, policy interceptor
- `ctrlz gateway --listen :6543 --upstream postgresql://…`
- `ctrlz/sdk/` — DB-API 2.0 connection/cursor wrapper + SQLAlchemy event hook
  (FR-4.8), for applications that cannot re-point their DSN
- Attribution injection on session start
- Tests: real `psql` driven end-to-end; extended-protocol client via psycopg2;
  fault injection; latency benchmark against NFR-2

### Risks

| Risk | Mitigation |
|---|---|
| Wire-protocol edge case corrupts a session | pass through anything not explicitly handled; test with real clients |
| Added latency | benchmark in CI; analysis is pure and cached by statement hash |
| Users assume the gateway is required for undo | `ctrlz doctor` states plainly that capture is independent |

### Exit criteria

`psql` connects through the gateway with no client changes · a policy-blocked
statement produces a native error and the session survives · the gateway dying
mid-session does not lose captured history · p99 analysis overhead < 1 ms.

---

## 4. Phase 3 — MySQL engine

### Goal

> 🟢 Make it work on a second, quite different database — the only real proof
> that the design is not accidentally shaped around PostgreSQL.

### Design decisions

**D3.1 — Triggers, not binlog, for v1.** Binlog gives richer capture but needs
server configuration, a replication user, and an out-of-process reader. Triggers
keep the existing "same transaction, same database" invariant (Rule 2). Binlog
becomes an optional high-fidelity backend later.

**D3.2 — Known MySQL constraints are findings, not workarounds.** No `SET LOCAL`
equivalent for operation grouping; no transactional DDL; `JSON_OBJECT` type
fidelity differs. Each gets documented in `spec.md` §9 rather than hidden.

### Deliverables

- `ctrlz/engines/mysql.py`; CI matrix extended; findings written up

### Exit criteria

The behavioural suite passes **unmodified** on MySQL 8, or every deviation is
documented with a reason.

---

## 5. Phase 4 — Control plane

### Goal

> 🟢 One place a team can search: who changed what, where, and when.

### Design decisions

**D4.1 — Replica, never source of truth.** Shipping crosses a transaction
boundary and can fail; the in-database log stays authoritative.

**D4.2 — Metadata by default, row values opt-in.** Shipping before/after images
off-box is a data-governance decision, not a default.

**D4.3 — Undo always executes against the source database** (FR-6.4).

### Deliverables

- `ctrlz ship` (idempotent, resumable); central schema; `ctrlz log --all`;
  retention enforcement

### Exit criteria

Shipping is idempotent under repeated runs and interruption · no row values
leave the database unless explicitly enabled.

---

## 6. Repository layout after Phases 1–2

```
ctrlz/
  analysis/          # NEW  Phase 1 — reading SQL (advisory only)
    __init__.py
    model.py         #      Analysis dataclass, parse_confidence
    backends/
      regex.py       #      current preflight logic, kept as fallback
      sqlglot.py     #      default
      pglast.py      #      optional extra
  policy/            # NEW  Phase 1 — the rulebook
    model.py
    loader.py        #      ctrlz.policy.yaml
    engine.py        #      evaluation + risk score
    rules/           #      built-in rules
  gateway/           # NEW  Phase 2 — the checkpoint
    protocol.py      #      PostgreSQL wire codec
    server.py        #      asyncio listener
    interceptor.py   #      policy application
  engines/           # unchanged — capture and undo (must not import analysis/)
  actor.py           # NEW  Phase 1
  preflight.py       # becomes a thin shim over analysis/backends/regex.py
spec/
  spec.md · plan.md · tasks.md
```

---

## 7. Testing strategy

> 🟢 Every claim in the spec has a test that would fail if the claim stopped
> being true.

| Layer | Approach |
|---|---|
| Analysis | differential corpus across all three backends |
| Policy | table-driven rule evaluation |
| Gateway | real `psql` subprocess + psycopg2 extended protocol |
| Isolation | test asserting `engines/` never imports `analysis/` (D1.2) |
| Fault tolerance | inject exceptions into the interceptor; assert fail-open |
| Migration | build a v0.1 database, upgrade it, assert history survives |
| Performance | benchmarks asserting NFR-1 and NFR-2 |

CI matrix: PostgreSQL 16, SQLite, MySQL 8 (Phase 3) × Python 3.10–3.12.

---

## 8. Definition of done for the pull request

- [x] All acceptance criteria in `spec.md` §10 met for the phases in scope —
      with one qualification, stated rather than ticked past: §10.4 asks for the
      behavioural suite to pass "unchanged" on all three engines, and MySQL
      carries 5 `xfail(strict=True)` for the InnoDB cascade gap. That is a
      documented refusal, not a pass.
- [x] Full suite green on every engine in the matrix — 629 passed, 5 xfailed
- [x] Benchmarks published for NFR-1 / NFR-2 — job summary on every build.
      NFR-1 had never been measured at all until this was done; NFR-2 turns out
      to be missed at p99 by the default backend on a cold parse. Both numbers
      are in `spec.md` §7.
- [x] `README.md` updated, including the honest limits section
- [x] Migration path from v0.1 documented and tested
- [x] Every commit authored by Praveen Anandhanathan, co-authored by Claude —
      except the merge commits for #5 and #6, where I dropped the co-author
      trailer. Recorded rather than fixed: rewriting merged history on a public
      branch to correct a commit message is a worse trade than the note.

---

## 9. What we are explicitly deferring

> 🟢 Named now so nobody assumes they are coming.

MySQL binlog capture · SQL Server and Oracle engines · IDE extensions ·
gateway support for MySQL/SQL Server wire protocols · DDL undo · row-value
shipping to the control plane by default · AI query-impact explanation.

---

## 10. Decisions taken at review

| # | Decision | Effect on this plan |
|---|---|---|
| D-1 | First PR = **Phase 1 only** | §0 table updated; Phase 2 becomes a separate PR |
| D-2 | Gateway = **wire proxy + Python SDK wrapper** | Phase 2 gains `ctrlz/sdk/` and decision D2.6 |
| D-3 | Risk score **warns by default** | Phase 1 ships `block_on_risk: false` in the default policy |
| D-4 | Third engine = **MySQL 8** | Phase 3 unchanged; SQL Server stays deferred (§9) |

Full rationale, including what was rejected and why, is in `spec.md` §12.

**Next artefact:** [`tasks.md`](./tasks.md) — the executable task breakdown for
Phase 1.
