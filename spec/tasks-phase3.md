# ctrlz — Phase 3: MySQL, and what it found

**Status:** complete
**Scope:** Phase 3 (third engine) — decision D-4
**Author:** Praveen Anandhanathan
**Co-author:** Claude
**Companion documents:** [`spec.md`](./spec.md) · [`plan.md`](./plan.md)

---

## Why a third engine

> 🟢 **In plain terms**
> The tool was designed against PostgreSQL. Making it work on a second, quite
> different database is the only way to find out whether the design is actually
> general or whether it just happens to fit the one database it grew up on.
>
> It turned out to be mostly general — and to have one real hole, in the exact
> place the whole tool exists to protect.

`plan.md` §4 said any interface change forced by MySQL is a **finding**, to be
documented rather than papered over. That was the point of the exercise, and
this document is the result.

---

## The result

| | Outcome |
|---|---|
| Shared behavioural tests passing on MySQL 8 | **15 of 20**, unmodified |
| Failing | **5**, all one root cause |
| Changes to the `Engine` interface | **none** |
| Changes to shared code | one bug fix, which was a latent SQLite defect too |

The interface held. `initialize` / `track` / `changes` / `assess` / `undo` and
the shared ordering and verdict logic needed no reshaping at all, which is the
strongest evidence available that the abstraction is real.

---

## Finding 1 — foreign-key cascades are invisible (the serious one)

> 🟢 When you delete a customer, the database can be told to tidy up their
> orders automatically. MySQL does that tidying in a place our recorder cannot
> see. So we would write down "one customer deleted" and never learn that
> twelve orders went with them.

InnoDB performs `ON DELETE CASCADE` and `ON UPDATE CASCADE` itself, below the
trigger layer. Measured directly:

```sql
CREATE TRIGGER c_child_del AFTER DELETE ON c_child FOR EACH ROW
  INSERT INTO c_log VALUES ('child trigger fired');
DELETE FROM c_parent WHERE id = 1;
```

| | child rows left | child triggers fired |
|---|---|---|
| MySQL 8.0.46 | 0 | **0** |
| PostgreSQL 16, same schema | 0 | 1 |

This is the exact failure mode the architecture was built to prevent — the one
measured in `spec.md` §4, where a statement that looks like 3 rows is really 6.
On PostgreSQL, trigger capture closes it. On MySQL, the database will not let
us see it at all.

### What the engine does about it

**It refuses.** An operation that deletes from a table with cascading children,
or changes a key referenced with `ON UPDATE CASCADE`, is reported as *not
undoable*:

```
ctrlz: users has a ON DELETE CASCADE foreign key from orders, and InnoDB
performs cascades without firing triggers -- rows removed from orders were
never captured, so this operation cannot be reversed completely.
```

Restoring the parent and silently losing the children would be worse than
offering no undo at all. The trust contract is the only thing that makes the
tool worth installing.

`ctrlz doctor` names the affected tables, so the blind spot is visible before
somebody relies on it:

```
Cascade blind spot (1 table(s))
  users  ->  orders
```

### The boundary, measured

A limitation nobody has measured the edges of is indistinguishable from one
that is everywhere. Each of these is a test in `tests/test_mysql.py`:

| Case | Undoable on MySQL? |
|---|---|
| `DELETE` from a parent with `ON DELETE CASCADE` children | ❌ refused |
| The identical `DELETE` with an `ON DELETE RESTRICT` foreign key | ✅ |
| `UPDATE` of a non-referenced column on a cascading parent | ✅ |
| `DELETE` from a table with no children | ✅ |
| Everything on tables not involved in a cascade | ✅ |

### Options considered

| Option | Rejected because |
|---|---|
| Restore the parent, accept losing children | silent data loss — the thing the tool exists to prevent |
| Reconstruct the cascade from the FK definition | we would be *inferring* what the database did. That is intent, not effect (spec.md §4, Rule 1) |
| Read the binlog instead of using triggers | breaks Rule 2 — capture would no longer be in the same transaction as the change. Worth revisiting as an *optional* backend, not as the default |
| Require users to drop `ON DELETE CASCADE` | not our call to make about someone's schema |
| **Refuse, and say exactly why** | ✅ chosen |

---

## Finding 2 — no reliable transaction marker

MySQL gives a trigger no way to distinguish one transaction from the next.
`information_schema.innodb_trx.trx_id` looked promising and is not: two
successive transactions on the same session both reported `2330`.

**Consequence.** Statements run through ctrlz are grouped exactly, because
ctrlz sets `@ctrlz_op_id` itself. Writes from other clients group per
connection. Better than SQLite, where the marker is global; not as good as
PostgreSQL, where one operation is exactly one transaction.

---

## Finding 3 — a trigger cannot touch a table the statement is using

MySQL error 1442. The first drift guard read the stored image inline:

```sql
... AND JSON_OBJECT(...) = (SELECT `after` FROM ctrlz_change_log WHERE seq = ?)
```

The capture trigger *writes* to `ctrlz_change_log`, so every tracked write
failed. The guard now reads the stored image in a separate statement and binds
it as a parameter, which sidesteps the restriction entirely.

---

## Finding 4 — decoded images cannot be compared column by column

A `json` column does not equal a re-serialised JSON string, and a `DECIMAL`
does not equal the float a careless round-trip produces. Comparing a decoded
image against separately-read column values reported every such row as drifted,
and undo silently matched nothing.

Both sides now go through one conversion: the server builds the live image with
the *same* `JSON_OBJECT` expression the trigger used. Decoding uses
`parse_float=Decimal`, because money columns are exactly what people undo.

---

## Finding 5 — AUTO_INCREMENT needs no resync

A pleasant one. InnoDB advances the counter when a row is inserted with an
explicit higher key, so restoring rows cannot strand it. The `setval` fix-up
PostgreSQL needs has no MySQL equivalent to write, and `UndoResult`'s
`sequences_fixed` is legitimately always empty here.

---

## Fixed as a side effect

MySQL surfaced a latent defect in the **SQLite** engine: rebuilding triggers
during an upgrade failed outright if a tracked table had since been dropped.
That is a stale bookkeeping row, not a reason to refuse an upgrade. Both
engines now untrack it and carry on.

---

## How the gap is recorded

`conftest.MYSQL_CASCADE_GAP` — one labelled list, marked
`xfail(strict=True)`, applied by a collection hook so the shared test bodies
stay identical across engines.

**Strict matters.** If MySQL ever starts passing those tests — because a
workaround is found, or MySQL changes — the build fails and somebody has to
come back and delete the list. An xfail that can quietly become a pass is a lie
with a longer shelf life.

---

## Definition of done

- [x] MySQL 8 engine implementing the existing `Engine` interface
- [x] Shared behavioural suite runs against it with test bodies unmodified
- [x] Every deviation documented with its reason (this file)
- [x] The gap's boundary asserted, not just described
- [x] `ctrlz doctor` reports the blind spot
- [x] 538 tests pass across SQLite, PostgreSQL 16 and MySQL 8
- [x] Commits authored by Praveen Anandhanathan, co-authored by Claude

## What this says about the spec

`spec.md` §9 should now list, as a non-goal rather than a gap:

> **Undo of cascading deletes on MySQL.** InnoDB performs referential actions
> without firing triggers. ctrlz detects the situation and refuses; it does not
> attempt to reconstruct what the database did out of sight.
