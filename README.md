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

Works on **PostgreSQL**, **MySQL** and **SQLite**, from the command line, from
Python, or through a gateway that protects every client you already use.

---

**New here?** Jump to [Getting started](#getting-started) — install, a
sixty-second demo on a throwaway database, permissions per engine, and what to
do when something goes wrong. The sections before it explain *how* it works and
*why* it is built this way.

---

## Why you would want this

| Without `ctrlz` | With `ctrlz` |
|---|---|
| `UPDATE` without a `WHERE` → restore last night's backup, lose today's work | `ctrlz undo` — seconds, targeted, no downtime |
| "Who changed these 400 rows?" → nobody knows | `ctrlz log` shows the person, the ticket, the statement's risk |
| A dangerous statement is caught in review, or never | Blocked before it runs, by a rulebook you version-control |
| Recovery is a DBA task with a maintenance window | A developer runs one command |

**It is not a backup replacement.** Backups protect you from losing the whole
database; `ctrlz` protects you from a specific mistake you can point at. Keep
both.

---

# Getting started

## 1. Install

Needs **Python 3.10 or newer**. Nothing needs a compiler.

```bash
pip install ctrlz-sql                # SQLite support, no extra dependencies
pip install 'ctrlz-sql[postgres]'    # + PostgreSQL 12 or newer
pip install 'ctrlz-sql[mysql]'       # + MySQL 8
pip install 'ctrlz-sql[postgres,mysql]'   # both

ctrlz --version                      # confirm it landed
```

Quote the brackets — `zsh` on macOS treats them as a glob and will error
without the quotes.

If you point `ctrlz` at a database whose driver is missing, it tells you which
one to install rather than showing a traceback.

## 2. Try it in sixty seconds, on nothing

SQLite needs no server, so you can prove the whole loop works before touching
anything real. This uses only Python and `ctrlz` — no other tools required:

```bash
export CTRLZ_DSN="sqlite:///demo.db"

ctrlz init
ctrlz run "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, plan TEXT)"
ctrlz track customers
ctrlz run "INSERT INTO customers (name, plan) VALUES ('ada','pro'),('bob','free')"

# The mistake: no WHERE clause. ctrlz blocks it, so --force to insist.
ctrlz run "UPDATE customers SET plan = 'free'" --force

python -c "import sqlite3;print(sqlite3.connect('demo.db').execute('select * from customers').fetchall())"
# [(1, 'ada', 'free'), (2, 'bob', 'free')]   <- ada's plan is wrong

ctrlz undo --yes

python -c "import sqlite3;print(sqlite3.connect('demo.db').execute('select * from customers').fetchall())"
# [(1, 'ada', 'pro'), (2, 'bob', 'free')]    <- back
```

Try `ctrlz run "UPDATE customers SET plan = 'free'"` **without** `--force` first
— that refusal is the part that saves you more often than undo does.

If that works, the machinery works. Everything below is connecting it to a real
database.

### Upgrading from an earlier version

Run `ctrlz init` against the existing database. Migrations are additive —
nullable columns only, no rewrites, no backfill — so they are fast on a large
history table and cannot lose anything. Operations captured before the upgrade
stay undoable and show no actor, which is the honest answer. There is a test
that builds a real v0.1 database, captures history through its triggers,
upgrades it, and undoes a pre-upgrade change.

## 3. Connect to your database

Set `CTRLZ_DSN` once, or pass `--dsn` each time:

```bash
export CTRLZ_DSN="postgresql://user:password@localhost:5432/appdb"
export CTRLZ_DSN="mysql://user:password@localhost:3306/appdb"
export CTRLZ_DSN="sqlite:////absolute/path/app.db"     # four slashes = absolute
export CTRLZ_DSN="sqlite:///relative.db"               # three = relative
```

`ctrlz init` prints the database it actually opened. **Read that line** — it is
how a typo'd DSN shows up as a typo instead of as a success.

## 4. Grant the right permissions

`ctrlz` creates a small amount of bookkeeping and one trigger per tracked table,
so it needs more than read/write. This is the step that most often goes wrong.

### PostgreSQL

```sql
GRANT CREATE ON DATABASE appdb TO ctrlz_user;   -- for `ctrlz init` (verified)
GRANT TRIGGER ON TABLE customers TO ctrlz_user; -- for `ctrlz track`
-- Or simply: the tables' owner already has both.
```

`init` creates a schema called `ctrlz`; `track` creates one trigger per table.
Nothing else is modified.

### MySQL 8

```sql
GRANT TRIGGER, CREATE, SELECT, INSERT, UPDATE, DELETE ON appdb.* TO 'ctrlz_user'@'%';
```

**The gotcha that will cost you an hour.** With binary logging on — the default
on most managed MySQL — creating a trigger fails unless the server allows it:

```sql
SET GLOBAL log_bin_trust_function_creators = 1;
```

Without it, `ctrlz track` fails with *"You do not have the SUPER privilege and
binary logging is enabled"*. That is MySQL's rule, not ours. On RDS/Aurora it is
a parameter-group setting rather than a statement.

### SQLite

Nothing. Write access to the file is the whole requirement.

## 5. Choose what to protect

```bash
ctrlz track --all              # every table with a usable identity
ctrlz track public.customers   # or one at a time
ctrlz tracked                  # what is protected right now
```

A table needs a primary key or a `NOT NULL` unique index — without one there is
no way to say *which* row to put back, and `ctrlz` says so rather than guessing.

Capture costs about **1.2×–1.5× a plain write** on a tracked table, and stores a
before/after copy of every changed row (see *What it keeps, and where*). Track
the tables that would hurt to lose, not everything reflexively.

## 6. Check your work before relying on it

```bash
ctrlz doctor
```

```console
engine:      postgresql
target:      postgresql://localhost:5432/appdb
initialized: yes
operations:  12 in history

Protected (3 table(s))
  public.customers  identity: id
  public.orders     identity: id

NOT protected (1 table(s))
  public.event_log
  Changes to these tables cannot be undone.

Known limits
  - TRUNCATE does not fire row triggers and cannot be captured.
  - DDL is not captured; schema changes are outside the undo history.
```

**`doctor` is the honest answer to "am I covered?"** Run it before you need it.

---

# Everyday use

```bash
ctrlz run "UPDATE orders SET status = 'shipped' WHERE id = 42"   # run it safely
ctrlz log                       # what has happened, and who did it
ctrlz preview                   # what an undo would do, before doing it
ctrlz undo --yes                # reverse the last operation
ctrlz redo --yes                # changed your mind about the undo
```

Undo takes an id, so you are not limited to the most recent thing:

```bash
ctrlz log
ctrlz preview 97e9ce62
ctrlz undo 97e9ce62
```

**Always `preview` before `undo` on anything that matters.** It reports each row
as `clean`, `drifted`, `missing` or `occupied`, and refuses rather than
overwriting somebody else's later edit.

Changes made *outside* `ctrlz` — by your application, another tool, `psql` — are
still captured, because capture lives in the database. You do not have to route
everything through the command line to be protected.

## Three ways to use it

| | Use when | What you get |
|---|---|---|
| **CLI** — `ctrlz run …` | Manual fixes, migrations, on-call | Guardrails, prompts, labels |
| **Gateway** — `ctrlz gateway …` | Protecting tools you cannot change | Every client covered, no code edits |
| **Python** — `import ctrlz` | Scripts, backfills, your app | The same checks, programmatically |

```python
import ctrlz

cz = ctrlz.connect("postgresql://localhost/appdb")
result = cz.run("UPDATE customers SET plan = 'pro' WHERE id = 7", label="upgrade")
print(result.rowcount)

if something_went_wrong:
    cz.undo("last")
```

## Running the gateway

```bash
ctrlz gateway --listen 127.0.0.1:6543 --upstream "$CTRLZ_DSN"
```

Then point any client at port 6543 instead of the database — `psql`, DBeaver,
your application, a BI tool. Nothing about them changes, and refused statements
come back as ordinary database errors.

**Bind it to localhost or a trusted network unless you configure TLS**
(see *TLS* above). Stopping the gateway never stops the database, and never
stops capture — it only removes the checkpoint.

---

# Platform notes

| | Linux | macOS | Windows |
|---|---|---|---|
| CLI, Python API, all three engines | ✅ | ✅ | ✅ |
| Gateway | ✅ | ✅ | ✅ |
| Private-key permission check | ✅ | ✅ | skipped |
| Policy-file ownership check | ✅ | ✅ | skipped |
| `SIGHUP` certificate reload | ✅ | ✅ | restart instead |

The three skipped items are POSIX concepts. On Windows they are **not enforced**
rather than silently faked — file ownership and mode do not mean the same thing
there, so pretending to check them would be worse than saying we do not. Every
other feature behaves identically.

CI runs Python 3.10, 3.11 and 3.12 against PostgreSQL 16 and MySQL 8 on Linux.
macOS and Windows are supported but not covered by automated runs — if you test
there, that feedback is worth having.

---

# When something goes wrong

| What you see | What it means |
|---|---|
| `'X' is not a database URL, and does not look like a path to a SQLite file` | The DSN was mistyped or a shell variable did not expand. `ctrlz` refuses rather than creating an empty database under that name. |
| `postgres support needs the 'psycopg2' driver` | `pip install 'ctrlz-sql[postgres]'` |
| `permission denied for database` | Missing `CREATE ON DATABASE` — see *Grant the right permissions*. |
| `You do not have the SUPER privilege and binary logging is enabled` | MySQL. Set `log_bin_trust_function_creators = 1`. |
| `<table> has no primary key…` | No way to identify a row. Add a key, or pass `--identity col1,col2`. |
| `refusing to use the policy at …: it is owned by …` | A rulebook found by searching up the directory tree that somebody else owns. Deliberate — see *The rulebook*. |
| `Cannot undo: rows may have been removed by a cascade` | MySQL only. InnoDB cascades bypass triggers, so those rows were never captured and `ctrlz` refuses rather than half-restoring. |
| Undo reports `drifted` | Somebody changed the row since. Inspect it, then `--allow-conflicts` if you are sure. |

`ctrlz doctor` answers most of these. `ctrlz --version` belongs in any bug
report.

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

## The gateway

> Point your existing tools at a checkpoint instead of at the database. Nothing
> about them changes.

```console
$ ctrlz gateway --listen 127.0.0.1:6543 --upstream postgresql://localhost/app
ctrlz gateway listening on 127.0.0.1:6543 -> localhost:5432
  4 tracked table(s); 7 rule(s) from ctrlz.policy.yaml
```

Then connect anything — `psql`, DBeaver, a BI tool, an application — to
`127.0.0.1:6543` instead of the database:

```console
$ psql -h 127.0.0.1 -p 6543 -U app -d app
app=> DELETE FROM orders;
ERROR:  ctrlz: DELETE with no WHERE clause on orders -- this touches every row
DETAIL:  Blocked by rule 'unfiltered-write' (risk 90/70). Read by sqlglot,
         confidence 0.90.
HINT:  Run it through `ctrlz run --force` if you are sure, or adjust the rule
       in ctrlz.policy.yaml.
app=> SELECT count(*) FROM orders;   -- session still fine
```

That is a real `psql` with no plugin, no wrapper and no configuration: the
refusal is a protocol-native `ErrorResponse`, so every client already knows how
to render it.

### What it deliberately does not do

| | Why |
|---|---|
| **No connection pooling** | one upstream connection per client. Multiplexing sessions would break `SET LOCAL`, temp tables and transactions in ways that are hard to see and impossible to explain |
| **No credential handling** | authentication is relayed verbatim, which is exactly why `md5` and SCRAM work. The cost: we cannot learn the authenticated identity, so attribution uses the startup packet's `user` and `application_name` |
| **No connection multiplexing** | see above |
| **Not required for undo** | capture lives inside the database. Stop the gateway and every change is still recorded and still reversible |

### TLS

The two hops are configured separately, because they are different trust
decisions. The **client** hop is usually the one crossing untrusted network;
the **database** hop is usually the near one.

```bash
ctrlz gateway --listen 0.0.0.0:6543 \
  --upstream postgresql://localhost/app \
  --tls-cert /etc/ctrlz/server.crt \
  --tls-key  /etc/ctrlz/server.key \
  --require-tls
```

| flag | effect |
|---|---|
| `--tls-cert` / `--tls-key` | serve TLS to clients. Without them `SSLRequest` is declined and everything is plaintext |
| `--require-tls` | refuse plaintext clients — the `hostssl` equivalent. The refusal is a rendered error, not a dropped socket |
| `--tls-ca` | verify client certificates against this CA (mutual TLS) |
| `--require-client-cert` | reject clients presenting none. Needs `--tls-ca`, and says so if you forget |
| `--tls-allow-insecure-key` | permit a group- or world-readable private key |

A private key other users can read is **refused**, exactly as PostgreSQL
refuses to start with one. TLS 1.2 is the floor. Certificates load before the
port binds, so a bad path fails while you are watching. **`SIGHUP` reloads
them** — a certificate renewal should not cost every open session, and a
reload that fails keeps the running certificate rather than ending the service.

`?sslmode=` on the upstream DSN is honoured for the database hop — `require`,
`verify-ca` and `verify-full` all work, and only `verify-full` checks the
hostname, exactly as libpq defines them.

### Why you cannot encrypt both hops

The gateway **refuses to start** if you configure a client certificate *and* an
encrypted upstream. Not an oversight — the alternative is a configuration that
starts cleanly and then fails at authentication with a client-side error that
never mentions the gateway.

An encrypted upstream makes PostgreSQL offer **SCRAM-SHA-256-PLUS**, whose
channel binding ties the authentication exchange to one specific TLS session so
that a man-in-the-middle cannot relay it. There are two TLS sessions here and
the gateway is the thing in the middle, so the binding data cannot match:

```
server offered SCRAM-SHA-256-PLUS authentication over a non-SSL connection
```

That is channel binding working exactly as designed, against exactly the thing
it was designed against. **Encrypt the hop that crosses untrusted network and
leave the other plaintext** — usually: keep the certificate, `sslmode=disable`
upstream.

If both hops genuinely must be encrypted, `--strip-channel-binding` removes
SCRAM-SHA-256-PLUS from the server's offer so clients fall back to plain
SCRAM-SHA-256. Both hops stay encrypted and the password is still never sent in
clear; what you give up is the proof that nothing sits between client and
database — and something does. This. It is opt-in, it logs a warning at
startup and on every downgrade, and if stripping would leave the server with no
mechanisms at all the offer is passed through untouched.

### Limits, because it is a network daemon

The gateway holds **one database connection per client**, so the number of
clients it will serve has to be bounded or it becomes a way to use up
`max_connections` and lock out everybody — including people connecting
directly.

```bash
ctrlz gateway --max-connections 100 --handshake-timeout 30 ...
```

A client over the limit gets a protocol-native error carrying SQLSTATE `53300`,
the same code PostgreSQL uses, so pools treat it as "retry shortly" rather than
as fatal. **The check happens before any upstream connection is opened** —
refusing costs a client a retry; refusing after connecting would spend the
resource the cap exists to protect. Keep it below the database's own limit.

The handshake timeout drops a connection that opens and then says nothing. It
applies to the handshake only: an established session idling between statements
is ordinary and is left alone.

### It fails open, always

Any internal exception logs and forwards the statement unchanged. A bug in the
checkpoint must never be able to take the database offline — and it never needs
to, because the recorder inside the database is running either way. A statement
we failed to judge is still a statement we can undo. There is a fault-injection
test that forces evaluation to raise on every statement and asserts the client
still completes its work.

### What it costs you

Published on every CI build, not estimated. Medians; see NFR-2 in
[`spec/spec.md`](./spec/spec.md) for p99s and why a p99 from a shared runner is
an upper bound on the machine rather than a property of the code.

| the gateway sees | added per statement |
|---|---|
| a statement shape it has judged before | **0.008 ms** |
| the same shape with different values | **0.008 ms** |
| a shape it has never seen — ordinary DML | **0.36 ms** |
| a shape it has never seen — CTE or large join | **0.83 ms** |

Verdicts are memoised, because analysis is pure. The key is a **fingerprint with
literal values removed**, so `WHERE id = 5` and `WHERE id = 6` share one entry —
which matters because plenty of drivers (psycopg2 among them) interpolate
parameters client-side and would otherwise never get a cache hit at all. It was
0.32 ms per statement for those clients before the fingerprint existed.

Removing literals is done carefully rather than blindly: `WHERE 1 = 1` is a
tautology and `WHERE 1 = 2` is not, so a statement comparing one literal to
another keeps its exact text as the key. Getting that wrong would let a
statement touching every row inherit a verdict from one matching none.

Capture is separate and costs **1.2×–1.5× a plain write** on a tracked table.
The ratio is worse on faster storage, because a trigger's cost is roughly fixed
while the write beneath it gets cheaper — plan against 1.5×.

## For applications that cannot be re-pointed

Some deployments bake the DSN in, or the traffic never leaves the process. Those
get a wrapper rather than a proxy:

```python
import psycopg2
from ctrlz.sdk import guard

conn = guard(psycopg2.connect(DSN), tracked=["public.users"])
conn.cursor().execute("DELETE FROM users")   # raises PreflightBlocked
```

SQLAlchemy has a hook:

```python
from ctrlz.sdk import install_sqlalchemy_guard

remove = install_sqlalchemy_guard(engine, tracked=["public.users"])
```

The wrapper is a proxy, not a reimplementation — anything it does not define is
delegated to the real connection, so driver-specific features keep working.

**Both doors call the same evaluator.** A test runs the same statements through
the gateway, the SDK and the CLI and fails if any of them disagrees. A rule that
holds at one door and not another is worse than no rule, because the same
statement is refused or permitted depending on how the application happened to
connect, and nothing makes that visible.

## Team history

> 🟢 Every database keeps its own recording. This collects copies into one place
> a team can search, without logging into each database in turn.

```console
$ ctrlz ship --to sqlite:///team-history.db --name billing-prod
Shipped. 2 operation(s), 2 change(s) from billing-prod.
  Metadata only. Row values stayed in the source database; pass --include-values
  to ship them too.
  Watermark now 2. Safe to run again.

$ ctrlz hub log --at sqlite:///team-history.db
ID        SOURCE        WHEN    STATE   ROWS  RISK  ACTOR    TABLES  LABEL
2b310110  billing-prod  1m ago          1     0     praveen  users   remove bob
bf5c0bd3  billing-prod  2m ago  undone  1     0     ada      users   raise ada
```

Filter with `--source`, `--actor`, `--table`, `--min-risk`. The hub itself runs
on SQLite or PostgreSQL.

Three rules, each about not letting a convenience become a liability:

- **It is a replica, never the source of truth.** Shipping crosses a
  transaction boundary and can fail, lag or be interrupted. If the hub and the
  database disagree, the database is right.
- **Undo is never orchestrated from the hub.** It records *where* an operation
  happened; reversing it means connecting to that database. A hub that could
  undo could undo *stale* data, with no way to know it was stale. A test
  asserts the `Hub` class exposes no `undo`, `apply` or `execute` — the absence
  is the design.
- **Metadata by default, row values only on request.** Without
  `--include-values` the hub learns what changed and by how much, never what
  the values were. A test greps the entire hub for a salary that was never
  meant to leave.

`ctrlz ship` is idempotent and resumable — progress is a watermark over the
change log's sequence, advanced only after a batch lands, and every write is an
upsert. Run it from cron; an interruption costs a repeat, never a gap. An undo
that happens *after* shipping is picked up on the next run, because a watermark
cannot see a column being set on a row already copied.

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
| `ctrlz gateway` | run the checkpoint in front of the database |
| `ctrlz ship --to <dsn>` | copy this database's history to a shared hub |
| `ctrlz hub log\|sources\|purge --at <dsn>` | read the shared hub |
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

## What it keeps, and where

Undo works by keeping copies of your rows, so it is worth being explicit about
what that means before you switch it on.

**Inside your database.** `ctrlz_change_log` holds a before-and-after JSON image
of every row changed on a tracked table. It lives in the same database, under
the same permissions and the same backups as the data it copies — it is not
shipped anywhere unless you run `ctrlz ship`. Anyone who can read a tracked
table can generally read its change log too, so **tracking a table containing
secrets puts a second copy of those secrets in the same database.**

**It grows without limit until you trim it.** There is no automatic retention.

```bash
ctrlz purge --older-than 30d        # drop operations older than 30 days
ctrlz purge --yes                   # drop the whole history
```

`purge` deletes the rows. It does not scrub the pages — as with any `DELETE`,
the bytes survive on disk until the database reuses or vacuums the space, so
treat it as "no longer queryable", not as secure erasure.

**Nothing reaches the logs.** No row value, credential or connection string is
written to a log at any level, including debug — exception *types* are logged
rather than their messages on every path that handles a statement, because a
parser error quotes the statement it failed on, and a statement is a row value.
There is a test that plants marked secrets in the real paths and greps
everything the logger emits.

**The shared hub ships metadata only.** `ctrlz ship` sends who, what, when and
how many — never row values — unless you pass `--include-values`, which is a
data-governance decision and is therefore off by default rather than something
to discover later.

## What this does not do

Stated plainly, because a safety tool that overstates its coverage is worse
than useless:

- **DDL is not captured.** `ALTER TABLE`, `DROP COLUMN` and friends are outside
  the undo history. Reversing a `DROP COLUMN` means snapshotting the whole
  column first, which turns a fast `ALTER` into a slow one — a footgun dressed
  as a feature. `ctrlz` warns and stays out of the way.
- **`TRUNCATE` cannot be captured** — it does not fire row triggers. The
  guardrail blocks it and suggests `DELETE` instead.
- **Cascading deletes on MySQL cannot be undone.** InnoDB performs
  `ON DELETE CASCADE` below the trigger layer, so the rows it removes are never
  captured — measured: the child's trigger fires *zero* times, where PostgreSQL
  fires it once. ctrlz detects this and reports such operations as **not
  undoable** rather than restoring the parent and silently losing the children.
  `ctrlz doctor` names the affected tables. Everything outside a cascade is
  unaffected, and the boundary is asserted by tests — see
  [`spec/tasks-phase3.md`](spec/tasks-phase3.md).
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
pytest                                                     # SQLite only
CTRLZ_TEST_PG_DSN=postgresql://user@localhost/db \
CTRLZ_TEST_MYSQL_DSN=mysql://user:pw@localhost/db pytest   # all three engines
```

The same behavioural tests run against every available engine, with the test
bodies identical. Where an engine genuinely cannot do something — MySQL and
cascading deletes — it is recorded as a strict `xfail` in one labelled list in
`conftest.py`, never by weakening an assertion. Strict, so that if MySQL ever
starts passing them the build fails and somebody has to come back and remove
the entry.

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
- **gateway tests driven by real clients** — `psql` as a subprocess for the
  simple protocol, psycopg3 for the extended one — because a desynchronised
  session or a swallowed authentication challenge is invisible to anything that
  speaks a simplified dialect;
- a **fault-injection test** proving the gateway fails open;
- an **agreement test** asserting the gateway, the SDK and the CLI reach
  identical verdicts;
- **benchmarks that publish their numbers** into the CI job summary rather than
  asserting a budget and discarding the measurement — a budget that is only
  asserted tells you nothing collapsed, not what the thing costs or which way it
  has been moving. Both NFR-1 (capture overhead) and NFR-2 (gateway overhead)
  are measured on every build, on two engines, and the figures in this README
  come from them.

## License

MIT.
