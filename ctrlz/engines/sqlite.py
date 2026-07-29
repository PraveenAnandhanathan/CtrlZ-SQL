"""SQLite engine.

Same shape as the Postgres engine -- row-level AFTER triggers store a JSON
image of OLD and NEW -- with two differences that come from the database
rather than from us:

* SQLite has no session variables, so the "which operation am I part of"
  marker lives in a table (``ctrlz_current_op``). ctrlz sets it around every
  statement it runs. Writes made by other tools are still captured, but they
  are grouped into one operation per ctrlz session rather than per statement.
  This is reported honestly by ``ctrlz doctor``.
* JSON cannot hold BLOBs, so blob values are stored as ``{"$blob": "<hex>"}``
  and decoded on the way back.

Restores are executed from Python with bound parameters, so values never pass
through SQL text.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..errors import (
    NoIdentity,
    NotInitialized,
    NotTracked,
    NotUndoable,
    UndoConflict,
    UnknownOperation,
)
from ..model import (
    CLEAN,
    DELETE,
    DRIFTED,
    INSERT,
    MISSING,
    OCCUPIED,
    UPDATE,
    Change,
    ExecutionResult,
    Operation,
    RowVerdict,
    Undoability,
    UndoResult,
)
from ..migrations import CURRENT_VERSION, pending
from ..ordering import order_verdicts, topological_rank
from .base import Engine

BLOB_KEY = "$blob"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ctrlz_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO ctrlz_settings (key, value) VALUES ('schema_version', '1');
INSERT OR IGNORE INTO ctrlz_settings (key, value) VALUES ('max_rows_per_operation', '100000');

CREATE TABLE IF NOT EXISTS ctrlz_tracked (
    table_name TEXT PRIMARY KEY,
    identity   TEXT NOT NULL,
    tracked_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ctrlz_operations (
    op_id      TEXT PRIMARY KEY,
    label      TEXT,
    source     TEXT NOT NULL DEFAULT 'external',
    actor      TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    row_count  INTEGER NOT NULL DEFAULT 0,
    capped     INTEGER NOT NULL DEFAULT 0,
    undo_of    TEXT,
    undone_at  TEXT,
    undone_by  TEXT
);

CREATE TABLE IF NOT EXISTS ctrlz_change_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id       TEXT NOT NULL REFERENCES ctrlz_operations(op_id) ON DELETE CASCADE,
    table_name  TEXT NOT NULL,
    action      TEXT NOT NULL,
    identity    TEXT NOT NULL,
    before      TEXT,
    after       TEXT,
    captured_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS ctrlz_change_log_op_idx ON ctrlz_change_log (op_id, seq);

CREATE TABLE IF NOT EXISTS ctrlz_current_op (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    op_id      TEXT,
    label      TEXT,
    source     TEXT,
    undo_of    TEXT,
    actor_user TEXT,
    actor_host TEXT,
    actor_app  TEXT,
    ticket     TEXT,
    channel    TEXT,
    risk       INTEGER,
    policy_outcome TEXT
);
INSERT OR IGNORE INTO ctrlz_current_op (id, op_id) VALUES (1, NULL);
"""


class SQLiteEngine(Engine):
    name = "sqlite"
    caveats = (
        "Writes made outside ctrlz are captured but grouped per ctrlz session, "
        "not per statement.",
        "DDL is not captured; schema changes are outside the undo history.",
    )

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.is_initialized():
            self._close_external_operation()

    # -- plumbing ----------------------------------------------------------

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _all(self, query: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(query, params).fetchall()

    def _one(self, query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(query, params).fetchone()

    def _require_init(self) -> None:
        if not self.is_initialized():
            raise NotInitialized("ctrlz is not installed in this database; run: ctrlz init")

    def _close_external_operation(self) -> None:
        """Start a fresh operation for the next batch of outside writes."""
        self.conn.execute(
            "UPDATE ctrlz_current_op SET op_id = NULL, label = NULL, source = NULL, "
            "undo_of = NULL WHERE id = 1 AND (source IS NULL OR source = 'external')"
        )

    @staticmethod
    def _quote(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    # -- lifecycle ---------------------------------------------------------

    def is_initialized(self) -> bool:
        row = self._one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ctrlz_change_log'"
        )
        return row is not None

    def initialize(self) -> None:
        """Create or upgrade the metadata store.

        SQLite has no ADD COLUMN IF NOT EXISTS, so each column is checked
        against the catalogue first. Adding a nullable column is a catalogue
        change here too -- no table rewrite, no long lock.
        """
        fresh = not self.is_initialized()
        self.conn.executescript(SCHEMA_SQL)

        for migration in pending(self.schema_version()):
            for table, column, column_type in migration.sqlite_columns:
                if not self._has_column(table, column):
                    self.conn.execute(
                        f"ALTER TABLE {self._quote(table)} "
                        f"ADD COLUMN {self._quote(column)} {column_type}"
                    )
            for statement in migration.sqlite:
                self.conn.execute(statement)

        self.conn.execute(
            "INSERT INTO ctrlz_settings (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (str(CURRENT_VERSION),),
        )

        # The trigger bodies name the columns they write, so an upgraded
        # database needs its triggers rebuilt before they can record anything
        # the migration just added. A table tracked earlier may have been
        # dropped since; that is a stale bookkeeping row, not a reason to
        # refuse the upgrade.
        if not fresh:
            for table, identity in self.tracked():
                try:
                    self.track(table, identity)
                except (NotTracked, NoIdentity):
                    self.untrack(table)

    def schema_version(self) -> int:
        row = self._one("SELECT value FROM ctrlz_settings WHERE key = 'schema_version'")
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _has_column(self, table: str, column: str) -> bool:
        rows = self._all(f"PRAGMA table_info({self._quote(table)})")
        return any(r["name"] == column for r in rows)

    def uninstall(self) -> None:
        if not self.is_initialized():
            return
        for table, _ident in self.tracked():
            try:
                self.untrack(table)
            except Exception:
                pass
        self.conn.executescript(
            """
            DROP TABLE IF EXISTS ctrlz_change_log;
            DROP TABLE IF EXISTS ctrlz_operations;
            DROP TABLE IF EXISTS ctrlz_tracked;
            DROP TABLE IF EXISTS ctrlz_current_op;
            DROP TABLE IF EXISTS ctrlz_settings;
            """
        )

    # -- introspection -----------------------------------------------------

    def tables(self) -> list[str]:
        rows = self._all(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'ctrlz_%' ORDER BY name"
        )
        return [r["name"] for r in rows]

    def _columns(self, table: str) -> list[sqlite3.Row]:
        rows = self._all(f"PRAGMA table_info({self._quote(table)})")
        if not rows:
            raise NotTracked(f"table {table} does not exist")
        return rows

    def _column_names(self, table: str) -> list[str]:
        return [r["name"] for r in self._columns(table)]

    def _has_rowid(self, table: str) -> bool:
        try:
            self.conn.execute(f"SELECT rowid FROM {self._quote(table)} LIMIT 1").fetchall()
            return True
        except sqlite3.OperationalError:
            return False

    def _detect_identity(self, table: str) -> list[str]:
        pk = [r["name"] for r in sorted(self._columns(table), key=lambda r: r["pk"]) if r["pk"]]
        if pk:
            return pk

        for idx in self._all(f"PRAGMA index_list({self._quote(table)})"):
            if not idx["unique"]:
                continue
            cols = [r["name"] for r in self._all(f"PRAGMA index_info({self._quote(idx['name'])})")]
            notnull = {r["name"]: r["notnull"] for r in self._columns(table)}
            if cols and all(notnull.get(c) for c in cols):
                return cols

        if self._has_rowid(table):
            # Every ordinary SQLite table has one, and it round-trips through
            # an explicit INSERT, so it is a perfectly good identity.
            return ["rowid"]

        raise NoIdentity(
            f"{table} has no primary key, NOT NULL unique index, or rowid. "
            f"Re-run with --identity col1,col2 if you know a unique column set."
        )

    # -- tracking ----------------------------------------------------------

    def _image_expression(self, table: str, alias: str) -> str:
        """SQL that builds a JSON image of OLD./NEW. for every column."""
        parts = []
        for name in self._column_names(table):
            ref = f"{alias}.{self._quote(name)}"
            parts.append(
                f"'{name}', CASE WHEN typeof({ref}) = 'blob' "
                f"THEN json_object('{BLOB_KEY}', hex({ref})) ELSE {ref} END"
            )
        return "json_object(" + ", ".join(parts) + ")"

    def _identity_expression(self, identity: list[str], alias: str) -> str:
        parts = []
        for name in identity:
            ref = f"{alias}.{self._quote(name)}" if name != "rowid" else f"{alias}.rowid"
            parts.append(
                f"'{name}', CASE WHEN typeof({ref}) = 'blob' "
                f"THEN json_object('{BLOB_KEY}', hex({ref})) ELSE {ref} END"
            )
        return "json_object(" + ", ".join(parts) + ")"

    def track(self, table: str, identity: Optional[list[str]] = None) -> list[str]:
        self._require_init()
        columns = set(self._column_names(table))
        if identity:
            missing = [c for c in identity if c not in columns and c != "rowid"]
            if missing:
                raise NoIdentity(f"{table} has no column(s) {', '.join(missing)}")
            ident = list(identity)
        else:
            ident = self._detect_identity(table)

        self.untrack(table)
        for action, alias_new, alias_old in (
            ("INSERT", "NEW", None),
            ("UPDATE", "NEW", "OLD"),
            ("DELETE", None, "OLD"),
        ):
            source_alias = alias_new or alias_old
            before = self._image_expression(table, alias_old) if alias_old else "NULL"
            after = self._image_expression(table, alias_new) if alias_new else "NULL"
            ident_expr = self._identity_expression(ident, source_alias)
            self.conn.executescript(
                f"""
                CREATE TRIGGER {self._quote(f'ctrlz_capture_{action.lower()}_{table}')}
                AFTER {action} ON {self._quote(table)} FOR EACH ROW
                BEGIN
                    -- Claim an operation id if nothing has set one for us.
                    UPDATE ctrlz_current_op
                       SET op_id = lower(hex(randomblob(16))), source = 'external'
                     WHERE id = 1 AND op_id IS NULL;

                    INSERT OR IGNORE INTO ctrlz_operations
                        (op_id, label, source, actor, undo_of,
                         actor_user, actor_host, actor_app, ticket, channel,
                         risk, policy_outcome)
                    SELECT op_id, label, coalesce(source, 'external'), 'sqlite', undo_of,
                           actor_user, actor_host, actor_app, ticket, channel,
                           risk, policy_outcome
                      FROM ctrlz_current_op WHERE id = 1;

                    UPDATE ctrlz_operations SET row_count = row_count + 1
                     WHERE op_id = (SELECT op_id FROM ctrlz_current_op WHERE id = 1);

                    UPDATE ctrlz_operations SET capped = 1
                     WHERE op_id = (SELECT op_id FROM ctrlz_current_op WHERE id = 1)
                       AND row_count > (SELECT CAST(value AS INTEGER)
                                          FROM ctrlz_settings
                                         WHERE key = 'max_rows_per_operation');

                    INSERT INTO ctrlz_change_log
                        (op_id, table_name, action, identity, before, after)
                    SELECT c.op_id, '{table}', '{action[0]}',
                           {ident_expr}, {before}, {after}
                      FROM ctrlz_current_op c
                     WHERE c.id = 1
                       AND (SELECT capped FROM ctrlz_operations WHERE op_id = c.op_id) = 0;
                END;
                """
            )

        self.conn.execute(
            "INSERT INTO ctrlz_tracked (table_name, identity) VALUES (?, ?) "
            "ON CONFLICT (table_name) DO UPDATE SET identity = excluded.identity, "
            "tracked_at = datetime('now')",
            (table, json.dumps(ident)),
        )
        return ident

    def untrack(self, table: str) -> None:
        self._require_init()
        for action in ("insert", "update", "delete"):
            self.conn.execute(
                f"DROP TRIGGER IF EXISTS {self._quote(f'ctrlz_capture_{action}_{table}')}"
            )
        self.conn.execute("DELETE FROM ctrlz_tracked WHERE table_name = ?", (table,))

    def tracked(self) -> list[tuple[str, list[str]]]:
        self._require_init()
        rows = self._all("SELECT table_name, identity FROM ctrlz_tracked ORDER BY table_name")
        return [(r["table_name"], json.loads(r["identity"])) for r in rows]

    # -- executing ---------------------------------------------------------

    def execute(
        self,
        sql_text: str,
        label: Optional[str] = None,
        dry_run: bool = False,
        decide: Optional[Callable[[int], bool]] = None,
        session: Optional[dict[str, str]] = None,
    ) -> ExecutionResult:
        self._require_init()
        op_id = uuid.uuid4().hex
        warnings: list[str] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            values = _session_columns(session)
            self.conn.execute(
                "UPDATE ctrlz_current_op SET op_id = ?, label = ?, source = 'ctrlz', "
                "undo_of = NULL, actor_user = ?, actor_host = ?, actor_app = ?, "
                "ticket = ?, channel = ?, risk = ?, policy_outcome = ? WHERE id = 1",
                (op_id, label, *values),
            )
            rowcount = 0
            for statement in split_statements(sql_text):
                cur = self.conn.execute(statement)
                if cur.rowcount and cur.rowcount > 0:
                    rowcount += cur.rowcount
            captured = self._one(
                "SELECT row_count, capped FROM ctrlz_operations WHERE op_id = ?", (op_id,)
            )

            keep = True
            if dry_run:
                keep = False
            elif decide is not None:
                keep = bool(decide(rowcount))

            if keep:
                if captured is None and rowcount > 0:
                    warnings.append(
                        "Rows changed but nothing was captured -- the target table is "
                        "probably not tracked. This change is NOT undoable."
                    )
                elif captured is not None and captured["capped"]:
                    warnings.append(
                        "Operation exceeded the capture limit and is NOT undoable."
                    )
                self.conn.execute(_CLEAR_CURRENT_OP)
                self.conn.execute("COMMIT")
                return ExecutionResult(
                    op_id=op_id if captured is not None else None,
                    rowcount=rowcount,
                    committed=True,
                    warnings=warnings,
                )

            self.conn.execute("ROLLBACK")
            return ExecutionResult(
                op_id=None, rowcount=rowcount, committed=False, warnings=warnings
            )
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # -- history -----------------------------------------------------------

    def _row_to_operation(self, r: sqlite3.Row) -> Operation:
        tables = [
            x["table_name"]
            for x in self._all(
                "SELECT DISTINCT table_name FROM ctrlz_change_log WHERE op_id = ? "
                "ORDER BY table_name",
                (r["op_id"],),
            )
        ]
        return Operation(
            op_id=r["op_id"],
            label=r["label"],
            source=r["source"],
            actor=r["actor"],
            started_at=_parse_ts(r["started_at"]),
            row_count=r["row_count"],
            capped=bool(r["capped"]),
            undo_of=r["undo_of"],
            undone_at=_parse_ts(r["undone_at"]),
            undone_by=r["undone_by"],
            tables=tables,
            actor_user=_column(r, "actor_user"),
            actor_host=_column(r, "actor_host"),
            actor_app=_column(r, "actor_app"),
            ticket=_column(r, "ticket"),
            channel=_column(r, "channel"),
            risk=_column(r, "risk"),
            policy_outcome=_column(r, "policy_outcome"),
        )

    def operations(
        self, limit: int = 20, include_undone: bool = True, include_undos: bool = True
    ) -> list[Operation]:
        self._require_init()
        clauses = []
        if not include_undone:
            clauses.append("undone_at IS NULL")
        if not include_undos:
            clauses.append("undo_of IS NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._all(
            f"SELECT * FROM ctrlz_operations {where} "
            f"ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_operation(r) for r in rows]

    def operation(self, op_id: str) -> Operation:
        self._require_init()
        row = self._one("SELECT * FROM ctrlz_operations WHERE op_id = ?", (op_id,))
        if not row:
            raise UnknownOperation(f"no operation {op_id}")
        return self._row_to_operation(row)

    def changes(self, op_id: str) -> list[Change]:
        self._require_init()
        rows = self._all(
            "SELECT * FROM ctrlz_change_log WHERE op_id = ? ORDER BY seq", (op_id,)
        )
        return [
            Change(
                seq=r["seq"],
                op_id=r["op_id"],
                table_schema="",
                table_name=r["table_name"],
                action=r["action"],
                identity=_decode(json.loads(r["identity"])),
                before=_decode(json.loads(r["before"])) if r["before"] else None,
                after=_decode(json.loads(r["after"])) if r["after"] else None,
                captured_at=_parse_ts(r["captured_at"]),
            )
            for r in rows
        ]

    # -- assessment --------------------------------------------------------

    def _live_row(self, change: Change) -> Optional[dict[str, Any]]:
        table = self._quote(change.table_name)
        where = " AND ".join(
            f"{'rowid' if k == 'rowid' else self._quote(k)} IS ?" for k in change.identity
        )
        cols = self._column_names(change.table_name)
        select = ", ".join(self._quote(c) for c in cols)
        row = self._one(
            f"SELECT {select} FROM {table} WHERE {where}",
            tuple(change.identity[k] for k in change.identity),
        )
        return {c: row[c] for c in cols} if row is not None else None

    def assess(self, op_id: str) -> Undoability:
        op = self.operation(op_id)
        changes = self.changes(op_id)
        blockers: list[str] = []

        if op.capped:
            blockers.append(
                f"Operation exceeded the capture limit ({op.row_count} rows stored); "
                f"only part of it was recorded, so it cannot be reversed safely."
            )
        if op.already_undone:
            blockers.append(f"Already undone at {op.undone_at:%Y-%m-%d %H:%M:%S}.")
        if not changes and not op.capped:
            blockers.append("No captured changes -- nothing to undo.")

        freed = {
            (c.table_name, _key(c.identity)): c.after for c in changes if c.action == INSERT
        }

        verdicts: list[RowVerdict] = []
        for change in changes:
            try:
                current = self._live_row(change)
            except sqlite3.Error as exc:
                blockers.append(f"Cannot inspect {change.table_name}: {exc}")
                continue
            verdicts.append(
                RowVerdict(change=change, status=_verdict(change, current, freed), current=current)
            )
        return Undoability(operation=op, verdicts=verdicts, blockers=blockers)

    # -- undo --------------------------------------------------------------

    def undo(
        self, op_id: str, allow_conflicts: bool = False, label: Optional[str] = None
    ) -> UndoResult:
        assessment = self.assess(op_id)
        if assessment.blockers:
            raise NotUndoable("; ".join(assessment.blockers))
        conflicts = assessment.conflicts
        if conflicts and not allow_conflicts:
            raise UndoConflict(
                f"{len(conflicts)} of {len(assessment.verdicts)} rows changed since "
                f"capture. Re-run with --allow-conflicts to overwrite them, or "
                f"ctrlz preview to see the differences."
            )

        undo_op = uuid.uuid4().hex
        skipped = overridden = 0
        todo: list[RowVerdict] = []
        for verdict in assessment.verdicts:
            if verdict.status in (MISSING, OCCUPIED):
                skipped += 1
                continue
            if verdict.status == DRIFTED:
                overridden += 1
            todo.append(verdict)

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # Defers FK checks to COMMIT, which removes most ordering hazards;
            # the ordering below covers the rest.
            self.conn.execute("PRAGMA defer_foreign_keys = ON")
            self.conn.execute(
                "UPDATE ctrlz_current_op SET op_id = ?, label = ?, source = 'ctrlz', "
                "undo_of = ? WHERE id = 1",
                (undo_op, label or f"undo of {op_id[:8]}", op_id),
            )
            applied, failed, touched = self._apply_ordered(todo)
            if failed:
                detail = ", ".join(
                    f"{v.change.table_name} {v.change.identity}" for v in failed[:3]
                )
                raise NotUndoable(
                    f"{len(failed)} row(s) could not be restored without violating a "
                    f"constraint ({detail}). Nothing was changed."
                )
            skipped += len(todo) - applied
            self.conn.execute(
                "UPDATE ctrlz_operations SET undone_at = datetime('now'), undone_by = ? "
                "WHERE op_id = ?",
                (undo_op, op_id),
            )
            self.conn.execute(_CLEAR_CURRENT_OP)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        exists = self._one("SELECT 1 FROM ctrlz_operations WHERE op_id = ?", (undo_op,))
        return UndoResult(
            op_id=op_id,
            undo_op_id=undo_op if exists else None,
            applied=applied,
            skipped=skipped,
            conflicts_overridden=overridden,
            tables=sorted(touched),
        )

    def _dependency_rank(self, tables: set[str]) -> dict[str, int]:
        edges: list[tuple[str, str]] = []
        for table in tables:
            for fk in self._all(f"PRAGMA foreign_key_list({self._quote(table)})"):
                edges.append((fk["table"], table))  # (parent, child)
        return topological_rank(tables, edges)

    def _apply_ordered(
        self, todo: list[RowVerdict]
    ) -> tuple[int, list[RowVerdict], set[str]]:
        tables = {v.change.table_name for v in todo}
        rank = self._dependency_rank(tables)
        pending = order_verdicts(todo, rank, key=lambda v: v.change.table_name)
        applied = 0
        touched: set[str] = set()

        while pending:
            deferred: list[RowVerdict] = []
            progress = False
            for verdict in pending:
                savepoint = f"ctrlz_{int(time.time() * 1000) % 100000}_{applied}"
                self.conn.execute(f"SAVEPOINT {self._quote(savepoint)}")
                try:
                    count = self._apply_inverse(verdict.change, force=verdict.status == DRIFTED)
                except sqlite3.IntegrityError as exc:
                    if "FOREIGN KEY" not in str(exc).upper():
                        self.conn.execute(f"ROLLBACK TO {self._quote(savepoint)}")
                        raise
                    self.conn.execute(f"ROLLBACK TO {self._quote(savepoint)}")
                    self.conn.execute(f"RELEASE {self._quote(savepoint)}")
                    deferred.append(verdict)
                    continue
                self.conn.execute(f"RELEASE {self._quote(savepoint)}")
                progress = True
                if count:
                    applied += count
                    touched.add(verdict.change.table_name)
            if not progress:
                return applied, deferred, touched
            pending = deferred

        return applied, [], touched

    def _apply_inverse(self, change: Change, force: bool) -> int:
        table = self._quote(change.table_name)
        cols = self._column_names(change.table_name)
        ident_cols = list(change.identity.keys())
        ident_sql = " AND ".join(
            f"{'rowid' if k == 'rowid' else self._quote(k)} IS ?" for k in ident_cols
        )
        ident_values = [_encode_value(change.identity[k]) for k in ident_cols]

        if change.action == INSERT:
            guard, guard_values = self._guard(change.after, cols, force)
            cur = self.conn.execute(
                f"DELETE FROM {table} WHERE {ident_sql}{guard}",
                tuple(ident_values + guard_values),
            )
            return cur.rowcount

        if change.action == DELETE:
            image = change.before or {}
            insert_cols = [c for c in cols if c in image]
            names = ", ".join(self._quote(c) for c in insert_cols)
            if "rowid" in change.identity and "rowid" not in insert_cols:
                names = "rowid, " + names
                values = [_encode_value(change.identity["rowid"])]
            else:
                values = []
            values += [_encode_value(image.get(c)) for c in insert_cols]
            placeholders = ", ".join("?" for _ in values)
            cur = self.conn.execute(
                f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(values)
            )
            return cur.rowcount

        image = change.before or {}
        set_cols = [c for c in cols if c in image]
        if not set_cols:
            return 0
        assignments = ", ".join(f"{self._quote(c)} = ?" for c in set_cols)
        set_values = [_encode_value(image.get(c)) for c in set_cols]
        guard, guard_values = self._guard(change.after, cols, force)
        cur = self.conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {ident_sql}{guard}",
            tuple(set_values + ident_values + guard_values),
        )
        return cur.rowcount

    def _guard(
        self, expected: Optional[dict], cols: list[str], force: bool
    ) -> tuple[str, list[Any]]:
        """A WHERE fragment asserting the row still holds the values we wrote.

        This is what makes the apply atomic with the drift check: if anything
        changed between assessment and now, the statement simply matches no
        rows instead of overwriting a stranger's edit.
        """
        if force or not expected:
            return "", []
        usable = [c for c in cols if c in expected]
        if not usable:
            return "", []
        fragment = "".join(f" AND {self._quote(c)} IS ?" for c in usable)
        return fragment, [_encode_value(expected.get(c)) for c in usable]

    # -- settings ----------------------------------------------------------

    def get_setting(self, key: str) -> Optional[str]:
        self._require_init()
        row = self._one("SELECT value FROM ctrlz_settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._require_init()
        self.conn.execute(
            "INSERT INTO ctrlz_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- retention ---------------------------------------------------------

    def purge(self, older_than_seconds: Optional[int] = None) -> int:
        self._require_init()
        if older_than_seconds is None:
            cur = self.conn.execute("DELETE FROM ctrlz_operations")
        else:
            cur = self.conn.execute(
                "DELETE FROM ctrlz_operations "
                "WHERE started_at < datetime('now', ?)",
                (f"-{int(older_than_seconds)} seconds",),
            )
        deleted = cur.rowcount
        self.conn.execute(
            "DELETE FROM ctrlz_change_log WHERE op_id NOT IN (SELECT op_id FROM ctrlz_operations)"
        )
        return deleted


def split_statements(script: str) -> list[str]:
    """Split a script into statements.

    sqlite3 refuses more than one statement per execute(), and executescript()
    would commit the transaction we are holding open. ``complete_statement``
    is quote- and comment-aware, so semicolons inside literals are safe.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for char in script:
        buffer.append(char)
        if char == ";":
            candidate = "".join(buffer)
            if sqlite3.complete_statement(candidate):
                if candidate.strip(" \t\r\n;"):
                    statements.append(candidate.strip())
                buffer = []
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements or [script]


_CLEAR_CURRENT_OP = (
    "UPDATE ctrlz_current_op SET op_id = NULL, label = NULL, source = NULL, "
    "undo_of = NULL, actor_user = NULL, actor_host = NULL, actor_app = NULL, "
    "ticket = NULL, channel = NULL, risk = NULL, policy_outcome = NULL WHERE id = 1"
)

#: Order matches the UPDATE in execute(); keep the two together.
_SESSION_KEYS = (
    "ctrlz.actor_user",
    "ctrlz.actor_host",
    "ctrlz.actor_app",
    "ctrlz.ticket",
    "ctrlz.channel",
    "ctrlz.risk",
    "ctrlz.policy_outcome",
)


def _session_columns(session: Optional[dict[str, str]]) -> tuple:
    """Flatten the engine-neutral session settings into column values."""
    session = session or {}
    values = []
    for key in _SESSION_KEYS:
        value = session.get(key)
        value = None if value in (None, "") else value
        if key == "ctrlz.risk" and value is not None:
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = None
        values.append(value)
    return tuple(values)


def _column(row, name: str):
    """Read a column that may not exist yet on a partially upgraded store."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


# -- value encoding --------------------------------------------------------


def _decode(obj: Any) -> Any:
    """Turn stored JSON back into Python values, restoring BLOBs."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {BLOB_KEY} and isinstance(obj[BLOB_KEY], str):
            return bytes.fromhex(obj[BLOB_KEY])
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode(v) for v in obj]
    return obj


def _encode_value(value: Any) -> Any:
    return value


def _key(identity: dict[str, Any]) -> tuple:
    return tuple(sorted((k, _hashable(v)) for k, v in identity.items()))


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    return value


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _verdict(change: Change, current: Optional[dict], freed: Optional[dict] = None) -> str:
    if change.action == DELETE:
        if current is None:
            return CLEAN
        slot = (change.table_name, _key(change.identity))
        if freed and slot in freed and _same(freed[slot], current):
            return CLEAN
        return OCCUPIED
    if current is None:
        return MISSING
    return CLEAN if _same(change.after, current) else DRIFTED


def _same(expected: Optional[dict], current: Optional[dict]) -> bool:
    """Compare a stored image with a live row.

    Only the columns present in the stored image are compared, so a column
    added after capture does not make every historic row look drifted.
    """
    if expected is None or current is None:
        return expected is current
    for key, value in expected.items():
        if key not in current:
            return False
        if current[key] != value:
            return False
    return True
