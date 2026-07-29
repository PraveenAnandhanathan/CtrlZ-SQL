# ctrlz — undo for SQL

You ran the `UPDATE` without the `WHERE`. `ctrlz` gets your rows back.

```console
$ ctrlz run "UPDATE customers SET status = 'inactive'" --force
Committed. 284 row(s) affected. Undo with: ctrlz undo 97e9ce62

$ ctrlz preview
Operation 97e9ce62  (unlabelled)
  284 row(s) across public.customers by app 40s ago via ctrlz

Undoing reverses: 284 update

  UPDATE public.customers [id=1] clean
      status: inactive → active
  UPDATE public.customers [id=2] clean
      status: inactive → churned
  … and 282 more row(s)

UNDOABLE  (284 clean)

$ ctrlz undo --yes
Undone. 284 row(s) restored across public.customers.
  This undo is itself operation 4f10c8a1 -- run `ctrlz redo` to reverse it.
```

Works on **PostgreSQL** and **SQLite**.

---

## How it works

Databases cannot reverse arbitrary SQL after the fact, so something has to
record the "before" state while the change is happening. The obvious design is
to sit in front of the database, parse each statement, and `SELECT` the rows it
is about to touch. **`ctrlz` deliberately does not do that**, because that
approach is racy, needs a SQL parser per dialect, and is blind to anything the
database does on its own.

Instead capture happens *inside* the database, with row-level `AFTER` triggers
that store a JSON image of `OLD` and `NEW`:

| | parse-and-select | trigger capture (what `ctrlz` does) |
|---|---|---|
| Row images | read separately, can change in between | written in the same transaction as the change |
| Needs a SQL parser | yes, one per dialect | no |
| `ON DELETE CASCADE` rows | invisible | captured as their own changes |
| Rows changed by other triggers | invisible | captured |
| Multi-table statements | needs analysis | falls out for free |

Restores hand the stored image back to the database and let *it* do the type
conversion — `jsonb_populate_record` on Postgres, bound parameters on SQLite —
so no value is ever rendered into SQL text. Arrays, `jsonb`, ranges, enums,
composites, `numeric`, generated columns and blobs all round-trip without
special-casing.

An operation is **one transaction**, because that is what the database
committed atomically, and therefore what can be reversed atomically.

## The trust contract

A safety net that silently fails is worse than no safety net. `ctrlz` never
offers an undo it cannot deliver. Every operation is assessed against the live
rows before you are asked to confirm:

| verdict | meaning | what undo does |
|---|---|---|
| `clean` | the row is exactly as we left it | reverses it |
| `drifted` | someone else changed it since | **refuses**, unless `--allow-conflicts` |
| `missing` | the row is already gone | skips it (not a failure) |
| `occupied` | another row took that primary key | **always skipped**, never overwritten |
| `blocked` | the operation was never fully captured | refuses outright |

Drift detection is not just a pre-check. Every inverse statement carries a
guard asserting the row still holds the values we wrote, so a change committed
between the preview and the apply causes the statement to match nothing rather
than clobber someone's work. There is a test for exactly that race.

If an operation exceeds the capture limit, it is marked and reported as **not
undoable** — `ctrlz` will not half-restore an operation and call it done.

## Guardrails

Undo is the fallback. The thing that actually saves you is the pre-flight:

```console
$ ctrlz run "DELETE FROM orders"
Blocked by a guardrail:
  - DELETE with no WHERE clause on orders -- this touches every row in the table.
  Re-run with --force if you really mean it.

$ ctrlz run "UPDATE orders SET total = 0 WHERE created_at < '2020-01-01'" --dry-run
Rolled back (dry run). It would have affected 1,284 row(s).
```

`--dry-run` executes the statement inside a transaction and rolls it back, so
the row count it reports is the number the database *actually touched* — not an
estimate from a query plan. Above the `--confirm-over` threshold (default 100
rows) you get asked before the commit lands.

### These checks parse SQL, they do not pattern-match it

Consider:

```sql
DELETE FROM audit USING (SELECT id FROM staging WHERE ok) q;
```

This deletes **every row** of `audit`. The `WHERE` restricts the subquery, not
the delete. A guardrail that greps for the word `WHERE` waves it through;
`ctrlz` blocks it:

```console
$ ctrlz check "DELETE FROM audit USING (SELECT id FROM staging WHERE ok) q"
DELETE  audit   read by sqlglot (confidence 0.90)

  [block] unfiltered-write
      DELETE with no WHERE clause on audit -- this touches every row in the table.

BLOCKED  risk 90/70
  decided by 'unfiltered-write'
```

Three backends sit behind one interface: `sqlglot` (pure Python, the default),
`pglast` (PostgreSQL's own grammar, `pip install ctrlz-sql[pg-parser]`), and a
regex fallback that is always available. Analysis never raises — a statement
too malformed to parse degrades to the fallback with a lower confidence score
and a note saying so.

**None of it is load-bearing for undo.** A missed warning costs you a warning,
not your data. A test walks the AST of every capture engine and fails the build
if one ever imports the analysis or policy packages.

`ctrlz check` exits `0` for allow, `1` for warn, `2` for block, so CI can act
on a verdict without parsing prose.

## The rulebook

Rules live in `ctrlz.policy.yaml`, so changing what your team considers
dangerous is a reviewable diff rather than a release:

```yaml
version: 1

defaults:
  risk_threshold: 70
  block_on_risk: false      # a high score warns; it does not refuse

rules:
  - name: protect-ledger
    when:
      tables: [ledger, "billing.*"]
      kind: [write]
    action: block
    risk: 99
    message: "{tables} is append-only -- raise a ticket instead"

  - name: prod-needs-a-filter
    when: {unfiltered: true}
    scope: {environments: [production]}
    action: block
    risk: 95
    message: "{statement} with no WHERE clause on {tables}"
```

```console
$ ctrlz policy show          # the rules actually in force, and where from
$ ctrlz policy test "<sql>"  # what would happen to this statement
$ ctrlz policy lint          # validate the file
$ ctrlz policy path          # which file was loaded
```

Three deliberate choices:

- **Risk aggregates with `max()`, not a sum.** A summed score cannot be
  explained — "why is this 84?" has no answer — and an unexplainable number in
  a safety tool gets ignored. With `max()` the score always points at one rule,
  and `--explain` names it.
- **A high score warns; it does not block** unless you set
  `block_on_risk: true`. A fresh install that refuses legitimate bulk work on
  day one gets uninstalled, and a tool nobody runs protects nobody. Rules
  carrying `action: block` still block on their own merit.
- **The loader is strict.** An unknown field, a duplicate rule name, or a
  version mismatch is an error with a suggestion — never a warning. A typo in a
  safety rule must not silently disable it.

Conditions are flat named fields — `statement`, `kind`, `unfiltered`,
`has_filter`, `filter_is_tautology`, `tables`, `writes_untracked`,
`min_confidence`, `max_confidence` — deliberately not an expression language. A
rulebook that needs its own parser cannot be read at a glance.

## Attribution

Every operation records who made it:

```console
$ export CTRLZ_ACTOR=praveen CTRLZ_TICKET=OPS-1234

$ ctrlz log
ID        WHEN    STATE     ROWS  RISK  ACTOR    TABLES           LABEL
97e9ce62  2m ago  undoable  284   90!   praveen  public.customers deactivate
4f10c8a1  1h ago  undoable  3     0     ada      public.orders    fix totals
```

The `!` marks a statement that ran despite a block, via `--force`. The history
shows what happened, not what we would have preferred.

Attribution is written into the database's own change log, so it survives
`ctrlz` being bypassed — a change made directly in `psql` still carries the
database role that made it. Where `ctrlz` genuinely does not know who acted the
column is `NULL`; a guessed actor in an audit trail is worse than an absent
one.

Configure with `CTRLZ_ACTOR`, `CTRLZ_TICKET`, `CTRLZ_HOST`,
`CTRLZ_APPLICATION`, `CTRLZ_ENVIRONMENT`. All optional — resolution falls back
to the OS user and hostname, and never fails, even on a stripped container with
neither.

## Install

```bash
pip install -e ".[postgres]"     # or just: pip install -e .   for SQLite only
pip install -e ".[pg-parser]"    # optional: PostgreSQL's own SQL grammar
```

The only required dependencies are `sqlglot` and `PyYAML`, both pure Python.
Nothing needs a compiler.

### Upgrading from 0.1

Run `ctrlz init` against an existing database. The migration is additive —
nullable columns only, no rewrites, no backfill — so it is fast on a large
history table and cannot lose anything. Operations captured before the upgrade
stay undoable and show no actor, which is the honest answer. There is a test
that builds a real v0.1 database, captures history through v0.1 triggers,
upgrades it, and undoes a pre-upgrade change.

## Getting started

```bash
export CTRLZ_DSN="postgresql://user@localhost/app"   # or sqlite:///app.db

ctrlz init          # install the capture machinery
ctrlz track --all   # attach triggers to every table with a usable identity
ctrlz doctor        # see exactly what is and is not protected
```

`ctrlz doctor` is worth running before you rely on any of this:

```console
engine:      postgresql
initialized: yes
operations:  12 in history

Protected (3 table(s))
  public.customers  identity: id
  public.orders     identity: id
  public.order_items  identity: order_id, sku

NOT protected (1 table(s))
  public.event_log
  Changes to these tables cannot be undone.

Known limits
  - TRUNCATE does not fire row triggers and cannot be captured.
  - DDL is not captured; schema changes are outside the undo history.
```

## Commands

| command | what it does |
|---|---|
| `ctrlz init` | install the capture schema and trigger function |
| `ctrlz track <table>` / `--all` | attach capture triggers |
| `ctrlz run "<sql>"` | execute behind the guardrails, in a labelled transaction |
| `ctrlz log` | recent operations and whether each is still undoable |
| `ctrlz preview [op]` | row-by-row diff of what an undo would do |
| `ctrlz undo [op]` | reverse it, with conflict detection |
| `ctrlz redo` | reverse the last undo |
| `ctrlz check "<sql>"` | run the guardrails without executing (exit 0/1/2) |
| `ctrlz purge --older-than 24h` | trim history |
| `ctrlz doctor` | what is protected, what is not, and what cannot be promised |

Operations can be named by id prefix (`ctrlz undo 97e9ce62`) or by `last`.
Every command takes `--json` for scripting.

Changes made by *other* clients are captured too — the triggers do not care who
is connected — so `ctrlz undo` works on a mistake made in `psql` or a GUI, not
just on statements run through `ctrlz run`.

## Python API

```python
import ctrlz

with ctrlz.connect("postgresql://localhost/app") as cz:
    cz.init()
    cz.track("public.users")

    cz.run("UPDATE users SET salary = 90000 WHERE id = 5", label="raise")

    report = cz.preview("last")
    print(report.status, report.counts)     # 'undoable' {'clean': 1, ...}

    cz.undo("last")
```

## What this does not do

Stated plainly, because a safety tool that overstates its coverage is worse
than useless:

- **DDL is not captured.** `ALTER TABLE`, `DROP COLUMN` and friends are outside
  the undo history. Reversing a `DROP COLUMN` means snapshotting the whole
  column first, which turns a fast `ALTER` into a slow one — a footgun dressed
  as a feature. `ctrlz` warns and stays out of the way.
- **`TRUNCATE` cannot be captured** — it does not fire row triggers. The
  guardrail blocks it and suggests `DELETE` instead.
- **Tables need a stable row identity**: a primary key, a `NOT NULL` unique
  index, or (on SQLite) a rowid. Otherwise pass `--identity col1,col2`.
- **Capture costs writes.** Every changed row writes a second row. This is a
  developer-and-staging safety tool, not something to switch on for a
  high-throughput production write path without measuring first.
- **On SQLite**, writes made outside `ctrlz` are captured but grouped into one
  operation per `ctrlz` session rather than per statement — SQLite has no
  session variables to key the grouping on. Postgres groups per transaction
  exactly.
- **Undo is not a backup.** It reverses recorded row changes. It is not
  point-in-time recovery and does not replace one.

## Tests

```bash
pip install -e ".[dev]"
pytest                                              # SQLite only
CTRLZ_TEST_PG_DSN=postgresql://user@localhost/db pytest   # both engines
```

The suite runs the same behavioural tests against every available engine, and
covers the cases that make this hard rather than only the happy path: cascading
deletes, concurrent modification, re-used primary keys, sequence resync after
restore, foreign-key ordering, delete-then-reinsert over the same key,
capture-limit refusal, and the race between assessment and apply.

Beyond that:

- a **differential corpus** of SQL statements every analysis backend must read
  the same way, with each fallback limitation recorded and explained rather
  than quietly tolerated;
- a **fuzz corpus** asserting `analyze()` never raises, on empty strings,
  unterminated literals, binary junk and 500 nested parentheses;
- a **layering guard** that fails the build if a capture engine ever imports
  the analysis or policy packages;
- a **migration test** that upgrades a genuine v0.1 database and undoes history
  captured before the upgrade;
- **benchmarks** asserting the analysis budget (measured: 0.25 ms median,
  0.46 ms p99 for parse plus policy evaluation).

## License

MIT.
