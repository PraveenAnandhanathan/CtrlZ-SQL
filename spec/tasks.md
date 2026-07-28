# ctrlz — Phase 1 Task Breakdown

**Status:** draft, awaiting approval
**Scope:** Phase 1 only (policy core) — decision D-1
**Author:** Praveen Anandhanathan
**Co-author:** Claude
**Companion documents:** [`spec.md`](./spec.md) · [`plan.md`](./plan.md)

---

## What Phase 1 delivers

> 🟢 **In plain terms**
> Right now the tool checks dangerous SQL by pattern-matching the text, the way
> you might scan a letter for the word "urgent". It works, but it is easy to
> fool and it cannot answer detailed questions. Phase 1 replaces that with
> genuine reading comprehension, adds a rulebook the team writes for itself,
> and signs every change with who made it.
>
> Nothing about undo changes. Undo already works and deliberately does not
> depend on any of this.

Three new subsystems, one schema migration, and the CLI surface for them:

| Subsystem | Question it answers |
|---|---|
| `ctrlz/analysis/` | *What does this statement actually say?* |
| `ctrlz/policy/` | *Are we allowed to run it, and how risky is it?* |
| `ctrlz/actor.py` | *Who is running it, and for what?* |

---

## Commit plan

Five commits, each green on its own. Every commit authored by **Praveen
Anandhanathan**, co-authored by **Claude**.

| # | Commit | Tasks |
|---|---|---|
| C1 | Analysis layer: parse SQL properly, behind a stable interface | T1–T6 |
| C2 | Policy engine: declarative rules, risk scoring | T7–T11 |
| C3 | Actor attribution and schema migration to v2 | T12–T15 |
| C4 | CLI surface: `ctrlz check` rebuilt, `ctrlz policy` added | T16–T18 |
| C5 | Docs, isolation guard, benchmark | T19–T22 |

---

## C1 — Analysis layer

### T1 · `Analysis` model
`ctrlz/analysis/model.py`

Frozen dataclass, the single currency every backend produces and the policy
engine consumes.

```python
@dataclass(frozen=True)
class Analysis:
    sql: str
    statement: str            # UPDATE | DELETE | INSERT | MERGE | SELECT | CREATE | …
    kind: str                 # 'write' | 'read' | 'ddl' | 'other'
    written_tables: tuple[str, ...]
    read_tables: tuple[str, ...]
    has_filter: bool          # a row-restricting predicate exists
    filter_is_tautology: bool # WHERE 1=1 and friends
    written_columns: tuple[str, ...]
    has_join: bool
    has_subquery: bool
    has_cte: bool
    confidence: float         # 0.0 – 1.0
    backend: str              # 'sqlglot' | 'pglast' | 'regex'
    notes: tuple[str, ...]    # why confidence is not 1.0
```

**Done when:** dataclass is frozen, fully typed, and has no imports from
`ctrlz.engines` or `ctrlz.policy`.

### T2 · Backend interface + registry
`ctrlz/analysis/__init__.py`, `ctrlz/analysis/backends/base.py`

`analyze(sql, dialect=None, prefer=None) -> Analysis`. Tries the preferred
backend, falls back down the chain on `ImportError` or parse failure.

**Done when:** `analyze()` never raises for any input string, including `""`,
binary junk, and 1 MB of nonsense. Test asserts this over a fuzz corpus.

> 🟢 The reader is never allowed to crash the thing it is reading for.

### T3 · Regex backend
`ctrlz/analysis/backends/regex.py`

Port the existing `preflight.py` logic to emit an `Analysis` with
`confidence ≈ 0.5`. This is the always-available floor — no dependency, no
build step.

**Done when:** every existing `preflight` test passes through the new backend.

### T4 · sqlglot backend (default)
`ctrlz/analysis/backends/sqlglot_backend.py`

Walk the AST for target tables, `WHERE`/`USING` predicates, assigned columns,
joins, CTEs, subqueries. `confidence = 0.9`; drop to `0.6` with a note when the
statement parses but contains constructs we do not model.

**Done when:** correctly analyses `UPDATE … FROM`, `DELETE … USING`, CTEs,
`INSERT … SELECT`, `MERGE`, and multi-statement scripts.

### T5 · pglast backend (optional extra)
`ctrlz/analysis/backends/pglast_backend.py`

Real PostgreSQL grammar. `confidence = 1.0`. Import guarded — absence must
degrade silently to sqlglot.

**Done when:** package imports cleanly with `pglast` uninstalled.

### T6 · Differential test corpus
`tests/test_analysis.py`, `tests/corpus/statements.yaml`

~60 statements with expected `statement`, `has_filter`, `written_tables`. Every
installed backend runs the whole corpus.

**Done when:** all backends agree on `has_filter` and `written_tables` for every
corpus entry, or the disagreement is asserted as expected and explained.

> 🟢 Three independent readers, same exam paper. Where they disagree we want to
> know about it deliberately, not discover it in production.

---

## C2 — Policy engine

### T7 · Rule and decision model
`ctrlz/policy/model.py`

```python
@dataclass(frozen=True)
class Rule:
    name: str
    when: Condition          # flat, declarative — no expressions (D1.3)
    action: str              # 'allow' | 'warn' | 'block'
    risk: int                # 0–100
    message: str
    scope: Scope             # tables / actors / environments

@dataclass
class Decision:
    outcome: str             # 'allow' | 'warn' | 'block'
    risk: int
    matched: list[RuleMatch]
    def explain(self) -> str: ...
```

**Condition fields:** `statement`, `kind`, `has_filter`, `filter_is_tautology`,
`tables` (glob), `writes_untracked`, `min_confidence`.

### T8 · YAML loader with strict validation
`ctrlz/policy/loader.py`

Loads `ctrlz.policy.yaml`; unknown keys are a **load error**, not a warning — a
typo in a safety rule must not silently disable it.

**Done when:** `version` mismatch, unknown condition field, and invalid action
each produce a precise, line-referenced error.

### T9 · Evaluator and risk aggregation
`ctrlz/policy/engine.py`

`evaluate(analysis, context) -> Decision`.

**Risk aggregation is `max()`, not a sum.** A sum is not explainable — "why is
this 84?" has no answer. `max()` always points at one rule.

Per D-3: risk never blocks unless `block_on_risk: true`; named `block` rules
always block.

**Done when:** `Decision.explain()` names the rule that set the outcome and the
rule that set the score.

### T10 · Built-in default policy
`ctrlz/policy/defaults.yaml`

Ships with `block_on_risk: false` (D-3). Rules: `unfiltered-write` (block, 90),
`truncate` (block, 85), `destructive-ddl` (warn, 70), `untracked-table`
(warn, 50), `tautology-filter` (warn, 60), `low-confidence-write` (warn, 30).

### T11 · Policy tests
`tests/test_policy.py`

Table-driven: statement × policy → expected outcome and risk. Includes an
override test proving `block_on_risk: true` changes behaviour with no code
change.

---

## C3 — Attribution and migration

### T12 · Actor context
`ctrlz/actor.py`

```python
@dataclass(frozen=True)
class Actor:
    user: str          # $CTRLZ_ACTOR, else OS user
    host: str
    application: str
    ticket: str | None # $CTRLZ_TICKET
    channel: str       # 'cli' | 'gateway' | 'sdk'
```

**Done when:** resolution works with no environment variables set, and never
raises on a host with no resolvable hostname.

### T13 · Schema migration v1 → v2
`ctrlz/migrations.py`, both engines

Adds to `operations`: `actor_user`, `actor_host`, `actor_app`, `ticket`,
`risk`, `policy_outcome`. Bumps `schema_version` to `2`.

**Rules:** additive only · idempotent · a v1 database keeps every existing row
and stays undoable throughout.

**Done when:** `ctrlz init` on a v1 database migrates it, and every operation
recorded before the migration is still undoable afterwards. Tested, not assumed.

> 🟢 Upgrading the tool must never cost you the undo history you already have.

### T14 · Propagate actor into capture
`ctrlz/engines/postgres.py`, `ctrlz/engines/sqlite.py`

Extend the existing session-settings path (`set_config('ctrlz.actor_user', …)`
on Postgres; the current-op row on SQLite). Capture triggers write the values
into `operations`.

**Constraint:** no change to the change-log write path or trigger hot loop
beyond additional column writes.

### T15 · Attribution tests
`tests/test_actor.py`

Actor recorded on both engines · absent actor degrades to OS user · a v1
database migrated mid-history shows null actor for old rows and a real actor for
new ones.

---

## C4 — CLI

### T16 · `ctrlz check` rebuilt
Reports parsed analysis, matched rules, outcome, risk, and which backend
produced it. `--json` for scripting. Exit codes: `0` allow · `1` warn · `2` block.

### T17 · `ctrlz policy` command group
`show` (effective merged policy) · `test "<sql>"` (evaluate one statement) ·
`lint` (validate the file) · `path` (where the file was loaded from).

### T18 · `ctrlz run` and `ctrlz log` wiring
`run` uses the policy engine instead of `preflight`; the recorded operation
carries actor, risk, and outcome. `log` gains `ACTOR` and `RISK` columns.

`preflight.py` becomes a thin deprecation shim over the regex backend, so any
existing import keeps working.

---

## C5 — Guards, docs, benchmark

### T19 · Layering guard test
`tests/test_layering.py`

Walks the AST of every module under `ctrlz/engines/` and asserts none imports
`ctrlz.analysis` or `ctrlz.policy`.

**This is the most important test in Phase 1.** It is the executable form of
Rule 1 — undo correctness may never depend on parsing SQL. Without it, the
boundary erodes the first time someone is in a hurry.

> 🟢 A tripwire that goes off if anyone ever wires the fallible reader into the
> part that must not be fallible.

### T20 · Analysis benchmark
`tests/test_benchmark.py` — asserts p99 `analyze()` under 1 ms (NFR-2), so
Phase 2's gateway budget is known to be achievable before it is built.

### T21 · Documentation
`README.md`: policy section, `ctrlz.policy.yaml` example, actor configuration,
upgrade note for v0.1 users. Two-layer style preserved.

### T22 · Packaging
`pyproject.toml`: `sqlglot` as a real dependency; `[pg-parser]` extra for
`pglast`; `PyYAML` for the policy loader.

---

## Test additions

| File | Covers |
|---|---|
| `tests/test_analysis.py` | differential corpus, fuzz-never-raises |
| `tests/test_policy.py` | rule evaluation, risk, opt-in blocking |
| `tests/test_actor.py` | attribution across both engines |
| `tests/test_migration.py` | v1 → v2, history survives |
| `tests/test_layering.py` | architectural boundary (T19) |
| `tests/test_benchmark.py` | NFR-2 |

Existing 63 tests must pass **unmodified**. Any change to an existing test is a
signal that Phase 1 broke a behaviour it was not supposed to touch, and needs
explaining in the PR.

---

## Definition of done

- [ ] T1–T22 complete
- [ ] Existing 63 tests pass unmodified; new tests pass on PostgreSQL + SQLite
- [ ] Rules load from YAML; a rule change alters behaviour with no code change
- [ ] Unparseable SQL degrades to regex with lowered confidence, never raises
- [ ] A v0.1 database upgrades cleanly and stays undoable throughout
- [ ] `ctrlz log` shows actor and risk
- [ ] `ctrlz/engines/` imports neither `analysis` nor `policy` (enforced by T19)
- [ ] p99 `analyze()` < 1 ms
- [ ] `README.md` updated
- [ ] Five commits, authored by Praveen Anandhanathan, co-authored by Claude
- [ ] PR raised and merged

---

## Explicitly out of scope for this PR

> 🟢 Named so nobody expects them in this change.

The gateway and SDK wrapper (Phase 2) · MySQL (Phase 3) · control plane
(Phase 4) · any change to capture or undo semantics · DDL undo.

---

## Risks specific to this breakdown

| Risk | Signal | Response |
|---|---|---|
| sqlglot models a construct differently from PostgreSQL | corpus disagreement in T6 | lower confidence, add a note; never guess |
| Migration on a large `operations` table locks it | slow `ALTER` | additive columns only; no rewrite, no backfill |
| Policy YAML grows into a config language | conditions needing expressions | hold the line at flat fields; escalate to a spec change instead |
| The layering guard is felt as an obstacle | someone wants analysis inside an engine | that is the guard working; the answer is no |
