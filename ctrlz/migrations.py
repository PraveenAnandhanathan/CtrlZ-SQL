"""Schema versions and how to move between them.

Two rules govern everything here, because the thing being migrated is a record
of how to undo mistakes -- losing it during an upgrade would be a particularly
cruel failure:

**Additive only.** Migrations add nullable columns. They never rewrite a table,
drop a column, or backfill. An `ALTER TABLE ... ADD COLUMN` with no default is
a catalogue change on both PostgreSQL and SQLite, so it stays fast on a history
table with millions of rows and does not hold a long lock.

**Idempotent.** Every migration is safe to run twice. `initialize()` runs the
whole chain on every call, fresh installs included, which means the upgrade
path is exercised by the entire test suite rather than by one lonely test.

> In plain terms: upgrading ctrlz must never cost you the undo history you
> already have, and must never take your database offline while it happens.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The schema this version of ctrlz expects.
CURRENT_VERSION = 3


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    #: Statements per engine. Missing engines are a no-op for that migration.
    postgres: tuple[str, ...] = ()
    sqlite: tuple[str, ...] = ()
    mysql: tuple[str, ...] = ()
    #: Columns to add to a table, checked before adding on engines without
    #: ADD COLUMN IF NOT EXISTS.
    sqlite_columns: tuple[tuple[str, str, str], ...] = ()


#: Columns recording who made a change and what the guardrails thought of it.
#: Nullable throughout: operations captured before the upgrade genuinely have
#: no actor, and inventing one would be a lie in an audit trail.
_ATTRIBUTION_COLUMNS = (
    ("actor_user", "text", "TEXT"),
    ("actor_host", "text", "TEXT"),
    ("actor_app", "text", "TEXT"),
    ("ticket", "text", "TEXT"),
    ("channel", "text", "TEXT"),
    ("risk", "integer", "INTEGER"),
    ("policy_outcome", "text", "TEXT"),
)

_UNDONE_INDEX = "ctrlz_operations_undone_idx"

MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=2,
        description="record who made each change, and what policy thought of it",
        postgres=(
            "ALTER TABLE ctrlz.operations "
            + ", ".join(
                f"ADD COLUMN IF NOT EXISTS {name} {pg_type}"
                for name, pg_type, _ in _ATTRIBUTION_COLUMNS
            ),
        ),
        sqlite_columns=tuple(
            (table, name, sqlite_type)
            # SQLite has no session variables, so the attribution a statement
            # carries is staged in ctrlz_current_op before the triggers copy it
            # into ctrlz_operations. Both tables need the columns; upgrading
            # only the destination leaves capture unable to write at all.
            for table in ("ctrlz_operations", "ctrlz_current_op")
            for name, _, sqlite_type in _ATTRIBUTION_COLUMNS
        ),
    ),
    Migration(
        version=3,
        description="index undone_at so a follower can find undos in one query",
        # Anything replicating the history has to ask "what was undone since
        # I last looked", and that question is a table scan without this.
        postgres=(
            f"CREATE INDEX IF NOT EXISTS {_UNDONE_INDEX} "
            f"ON ctrlz.operations (undone_at) WHERE undone_at IS NOT NULL",
        ),
        sqlite=(
            f"CREATE INDEX IF NOT EXISTS {_UNDONE_INDEX} "
            f"ON ctrlz_operations (undone_at)",
        ),
        mysql=(
            f"CREATE INDEX {_UNDONE_INDEX} ON ctrlz_operations (undone_at)",
        ),
    ),
)


def pending(current: int) -> tuple[Migration, ...]:
    """Migrations not yet applied to a store at ``current`` version."""
    return tuple(m for m in MIGRATIONS if m.version > current)


def attribution_columns() -> tuple[str, ...]:
    return tuple(name for name, _, _ in _ATTRIBUTION_COLUMNS)
