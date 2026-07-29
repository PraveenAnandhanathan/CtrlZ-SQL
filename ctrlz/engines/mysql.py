"""MySQL engine.

The same shape as the other two -- row-level ``AFTER`` triggers storing a JSON
image of ``OLD`` and ``NEW``, in the same transaction as the change -- which is
the point of building it: an abstraction that has only ever run on the database
it was designed against is a guess.

Three things genuinely differ, and each is a finding rather than a workaround
(see ``spec/tasks-phase3.md``):

* **Operation grouping is per connection, not per transaction.** MySQL gives a
  trigger no reliable way to tell one transaction from the next -- ``trx_id``
  from ``information_schema.innodb_trx`` is not distinct between successive
  transactions on the same session. Statements run through ctrlz are grouped
  exactly, because ctrlz sets the marker itself; writes from other clients are
  grouped per connection.
* **No transactional DDL.** Every ``CREATE TRIGGER`` commits, so ``track`` is
  not atomic across several tables the way it is on Postgres.
* **AUTO_INCREMENT needs no resync.** InnoDB advances the counter when a row is
  inserted with an explicit higher key, so restoring rows cannot strand it --
  the fix-up Postgres needs has no MySQL equivalent to write.

And one that is not a difference in convenience but a genuine gap in what MySQL
can be asked to record:

* **Foreign-key cascades do not fire triggers.** InnoDB performs
  ``ON DELETE CASCADE`` and ``ON UPDATE CASCADE`` itself, below the trigger
  layer, so the rows it removes are invisible to capture. Measured: deleting a
  parent with one child captured the parent and nothing else, while the same
  schema on PostgreSQL captured both.

  This is exactly the silent-data-loss shape the whole design exists to avoid,
  so the engine does not paper over it. Any operation that deletes from a table
  with cascading children -- or changes a referenced key with ``ON UPDATE
  CASCADE`` -- is reported as **not undoable**, because rows may have gone that
  we never saw. Refusing an undo we cannot complete is the entire trust
  contract; quietly restoring the parent and losing the children would be worse
  than having no undo at all.

Drift is compared by asking the server to build the live row's image with the
*same* JSON expression the trigger used, so both sides go through one
conversion. Comparing a decoded image against separately-read column values
would report every ``DECIMAL`` as drifted.
"""

from __future__ import annotations

import decimal
import json
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from ..errors import (
    NoIdentity,
    NotInitialized,
    NotTracked,
    NotUndoable,
    UndoConflict,
    UnknownOperation,
)
from ..migrations import CURRENT_VERSION, pending
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
from ..ordering import order_verdicts, topological_rank
from .base import Engine

#: Marker for a value JSON cannot hold. MySQL refuses binary strings in
#: JSON_OBJECT outright, so they are base64'd on the way in.
BINARY_KEY = "$b64"

#: Column types whose values cannot go into JSON as-is.
BINARY_TYPES = frozenset(
    {"blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary", "geometry"}
)

#: InnoDB foreign-key errors. Anything else is a genuine failure.
FK_ERRORS = frozenset({1451, 1452})

#: Referential actions InnoDB performs *itself*, without firing a trigger.
#: See the module docstring: this is the one place MySQL is genuinely less
#: capable than PostgreSQL, and it is handled by refusing rather than hoping.
INVISIBLE_ACTIONS = frozenset({"CASCADE", "SET NULL"})

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS ctrlz_settings (
        `key`   varchar(64) NOT NULL PRIMARY KEY,
        `value` text NOT NULL
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ctrlz_tracked (
        table_name varchar(190) NOT NULL PRIMARY KEY,
        identity   text NOT NULL,
        tracked_at datetime NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ctrlz_operations (
        op_id      char(32) NOT NULL PRIMARY KEY,
        label      text,
        source     varchar(32) NOT NULL DEFAULT 'external',
        actor      varchar(190) NOT NULL DEFAULT '',
        started_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        row_count  int NOT NULL DEFAULT 0,
        capped     tinyint NOT NULL DEFAULT 0,
        undo_of    char(32),
        undone_at  datetime(6),
        undone_by  char(32),
        INDEX ctrlz_operations_started (started_at)
    ) ENGINE=InnoDB
    """,
    """
    CREATE TABLE IF NOT EXISTS ctrlz_change_log (
        seq         bigint NOT NULL AUTO_INCREMENT PRIMARY KEY,
        op_id       char(32) NOT NULL,
        table_name  varchar(190) NOT NULL,
        action      char(1) NOT NULL,
        identity    json NOT NULL,
        `before`    json,
        `after`     json,
        captured_at datetime(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        INDEX ctrlz_change_log_op (op_id, seq)
    ) ENGINE=InnoDB
    """,
    "INSERT IGNORE INTO ctrlz_settings (`key`, `value`) VALUES ('schema_version', '1')",
    "INSERT IGNORE INTO ctrlz_settings (`key`, `value`) "
    "VALUES ('max_rows_per_operation', '100000')",
)


class MySQLEngine(Engine):
    name = "mysql"
    caveats = (
        "Foreign-key cascades do not fire triggers in InnoDB, so rows removed "
        "by ON DELETE CASCADE are never captured. Operations that could have "
        "cascaded are reported as not undoable.",
        "Writes made outside ctrlz are grouped per connection, not per "
        "transaction: MySQL gives a trigger no reliable transaction marker.",
        "DDL is not captured; schema changes are outside the undo history, and "
        "MySQL commits implicitly on every DDL statement.",
        "TRUNCATE does not fire row triggers and cannot be captured.",
    )

    def __init__(self, dsn: str):
        import pymysql
        from pymysql.constants import CLIENT

        from urllib.parse import unquote, urlparse

        parsed = urlparse(dsn)
        self.database = unquote(parsed.path.lstrip("/"))
        self.default_schema = self.database
        self.conn = pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=unquote(parsed.username or "root"),
            password=unquote(parsed.password or ""),
            database=self.database,
            autocommit=True,
            charset="utf8mb4",
            # Lets a caller run "a; b" as one operation, the way psql does.
            client_flag=CLIENT.MULTI_STATEMENTS,
        )

    # -- plumbing ----------------------------------------------------------

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _all(self, query: str, params: tuple = ()) -> list[dict]:
        import pymysql.cursors

        with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(query, params)
            return list(cur.fetchall())

    def _one(self, query: str, params: tuple = ()) -> Optional[dict]:
        rows = self._all(query, params)
        return rows[0] if rows else None

    def _exec(self, query: str, params: tuple = ()) -> int:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return cur.rowcount

    def _require_init(self) -> None:
        if not self.is_initialized():
            raise NotInitialized("ctrlz is not installed in this database; run: ctrlz init")

    @staticmethod
    def _quote(name: str) -> str:
        return "`" + str(name).replace("`", "``") + "`"

    # -- lifecycle ---------------------------------------------------------

    def is_initialized(self) -> bool:
        return bool(
            self._one(
                "SELECT 1 AS ok FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = 'ctrlz_change_log'",
                (self.database,),
            )
        )

    def initialize(self) -> None:
        fresh = not self.is_initialized()
        for statement in SCHEMA_STATEMENTS:
            self._exec(statement)

        for migration in pending(self.schema_version()):
            for table, column, column_type in migration.sqlite_columns:
                if table == "ctrlz_current_op":
                    continue  # SQLite-only staging table
                if not self._has_column(table, column):
                    self._exec(
                        f"ALTER TABLE {self._quote(table)} "
                        f"ADD COLUMN {self._quote(column)} "
                        f"{'int' if column_type == 'INTEGER' else 'text'}"
                    )

        self._exec(
            "INSERT INTO ctrlz_settings (`key`, `value`) VALUES ('schema_version', %s) "
            "ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)",
            (str(CURRENT_VERSION),),
        )

        if not fresh:
            # Trigger bodies name the columns they write, so an upgraded
            # database needs them rebuilt. A table tracked earlier may have
            # been dropped since; that is a stale bookkeeping row, not a
            # reason to refuse the upgrade.
            for table, identity in self.tracked():
                try:
                    self.track(table, identity)
                except (NotTracked, NoIdentity):
                    self.untrack(table)

    def schema_version(self) -> int:
        row = self._one("SELECT `value` FROM ctrlz_settings WHERE `key` = 'schema_version'")
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError, KeyError):
            return 0

    def _has_column(self, table: str, column: str) -> bool:
        return bool(
            self._one(
                "SELECT 1 AS ok FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
                (self.database, table, column),
            )
        )

    def uninstall(self) -> None:
        if not self.is_initialized():
            return
        for table, _identity in self.tracked():
            try:
                self.untrack(table)
            except Exception:
                pass
        for table in (
            "ctrlz_change_log", "ctrlz_operations", "ctrlz_tracked", "ctrlz_settings"
        ):
            self._exec(f"DROP TABLE IF EXISTS {self._quote(table)}")

    # -- introspection -----------------------------------------------------

    def tables(self) -> list[str]:
        rows = self._all(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
            "AND table_name NOT LIKE 'ctrlz\\_%%' ORDER BY table_name",
            (self.database,),
        )
        return [r["name"] for r in rows]

    def _columns(self, table: str) -> list[dict]:
        rows = self._all(
            "SELECT column_name AS name, data_type AS type, extra AS extra, "
            "       is_nullable AS nullable "
            "  FROM information_schema.columns "
            " WHERE table_schema = %s AND table_name = %s "
            " ORDER BY ordinal_position",
            (self.database, table),
        )
        if not rows:
            raise NotTracked(f"table {table} does not exist")
        return rows

    def _column_names(self, table: str) -> list[str]:
        return [c["name"] for c in self._columns(table)]

    def _writable_columns(self, table: str) -> list[str]:
        # Generated columns are computed by the server and rejected in an
        # INSERT or UPDATE column list.
        return [
            c["name"] for c in self._columns(table)
            if "GENERATED" not in (c["extra"] or "").upper()
        ]

    def _detect_identity(self, table: str) -> list[str]:
        rows = self._all(
            "SELECT column_name AS name FROM information_schema.statistics "
            " WHERE table_schema = %s AND table_name = %s AND index_name = 'PRIMARY' "
            " ORDER BY seq_in_index",
            (self.database, table),
        )
        if rows:
            return [r["name"] for r in rows]

        candidates = self._all(
            "SELECT index_name AS idx, column_name AS name, seq_in_index AS pos "
            "  FROM information_schema.statistics "
            " WHERE table_schema = %s AND table_name = %s AND non_unique = 0 "
            " ORDER BY index_name, seq_in_index",
            (self.database, table),
        )
        nullable = {
            c["name"]: (c["nullable"] or "").upper() == "YES" for c in self._columns(table)
        }
        grouped: dict[str, list[str]] = {}
        for row in candidates:
            grouped.setdefault(row["idx"], []).append(row["name"])
        for columns in grouped.values():
            if columns and not any(nullable.get(c, True) for c in columns):
                return columns

        raise NoIdentity(
            f"{table} has no primary key or NOT NULL unique index, so rows cannot "
            f"be identified for undo. Re-run with --identity col1,col2 if you know "
            f"a unique column set."
        )

    # -- image expressions -------------------------------------------------

    def _image_expression(self, table: str, alias: str, columns=None) -> str:
        """JSON_OBJECT over the given columns, base64'ing anything binary.

        MySQL refuses a binary string inside JSON_OBJECT outright, so blob and
        geometry columns are wrapped in a marker object and decoded on the way
        back out.
        """
        parts = []
        for column in self._columns(table):
            name = column["name"]
            if columns is not None and name not in columns:
                continue
            reference = f"{alias}.{self._quote(name)}" if alias else self._quote(name)
            if (column["type"] or "").lower() in BINARY_TYPES:
                value = (
                    f"CASE WHEN {reference} IS NULL THEN NULL "
                    f"ELSE JSON_OBJECT('{BINARY_KEY}', TO_BASE64({reference})) END"
                )
            else:
                value = reference
            parts.append(f"'{name}', {value}")
        return "JSON_OBJECT(" + ", ".join(parts) + ")"

    # -- tracking ----------------------------------------------------------

    def track(self, table: str, identity: Optional[list[str]] = None) -> list[str]:
        self._require_init()
        table = table.rpartition(".")[2]
        columns = set(self._column_names(table))

        if identity:
            missing = [c for c in identity if c not in columns]
            if missing:
                raise NoIdentity(f"{table} has no column(s) {', '.join(missing)}")
            ident = list(identity)
        else:
            ident = self._detect_identity(table)

        self.untrack(table)
        for action, new_alias, old_alias in (
            ("INSERT", "NEW", None), ("UPDATE", "NEW", "OLD"), ("DELETE", None, "OLD")
        ):
            source = new_alias or old_alias
            before = self._image_expression(table, old_alias) if old_alias else "NULL"
            after = self._image_expression(table, new_alias) if new_alias else "NULL"
            ident_expr = self._image_expression(table, source, columns=set(ident))
            self._exec(self._trigger_sql(table, action, ident_expr, before, after))

        self._exec(
            "INSERT INTO ctrlz_tracked (table_name, identity) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE identity = VALUES(identity), "
            "tracked_at = CURRENT_TIMESTAMP",
            (table, json.dumps(ident)),
        )
        return ident

    def _trigger_sql(
        self, table: str, action: str, ident_expr: str, before: str, after: str
    ) -> str:
        """The capture trigger.

        ``@ctrlz_op_id`` is a session variable. ctrlz sets it around every
        statement it runs, so those group exactly. A client that never sets it
        gets one generated here, which is why external writes group per
        connection -- see the module docstring.
        """
        name = self._quote(f"ctrlz_capture_{action[0].lower()}_{table}")
        return f"""
        CREATE TRIGGER {name}
        AFTER {action} ON {self._quote(table)} FOR EACH ROW
        BEGIN
            IF @ctrlz_op_id IS NULL THEN
                SET @ctrlz_op_id = REPLACE(UUID(), '-', '');
                SET @ctrlz_source = 'external';
                SET @ctrlz_label = NULL;
                SET @ctrlz_undo_of = NULL;
            END IF;

            INSERT IGNORE INTO ctrlz_operations
                (op_id, label, source, actor, undo_of,
                 actor_user, actor_host, actor_app, ticket, channel,
                 risk, policy_outcome)
            VALUES
                (@ctrlz_op_id, @ctrlz_label,
                 COALESCE(@ctrlz_source, 'external'), CURRENT_USER(), @ctrlz_undo_of,
                 @ctrlz_actor_user, @ctrlz_actor_host, @ctrlz_actor_app,
                 @ctrlz_ticket, @ctrlz_channel, @ctrlz_risk, @ctrlz_policy_outcome);

            UPDATE ctrlz_operations SET row_count = row_count + 1
             WHERE op_id = @ctrlz_op_id;

            UPDATE ctrlz_operations SET capped = 1
             WHERE op_id = @ctrlz_op_id
               AND row_count > (SELECT CAST(`value` AS UNSIGNED) FROM ctrlz_settings
                                 WHERE `key` = 'max_rows_per_operation');

            INSERT INTO ctrlz_change_log
                (op_id, table_name, action, identity, `before`, `after`)
            SELECT @ctrlz_op_id, '{table}', '{action[0]}',
                   {ident_expr}, {before}, {after}
              FROM DUAL
             WHERE (SELECT capped FROM ctrlz_operations WHERE op_id = @ctrlz_op_id) = 0;
        END
        """

    def untrack(self, table: str) -> None:
        self._require_init()
        table = table.rpartition(".")[2]
        for action in ("i", "u", "d"):
            self._exec(
                f"DROP TRIGGER IF EXISTS {self._quote(f'ctrlz_capture_{action}_{table}')}"
            )
        self._exec("DELETE FROM ctrlz_tracked WHERE table_name = %s", (table,))

    def tracked(self) -> list[tuple[str, list[str]]]:
        self._require_init()
        rows = self._all(
            "SELECT table_name AS name, identity FROM ctrlz_tracked ORDER BY table_name"
        )
        return [(r["name"], json.loads(r["identity"])) for r in rows]

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

        self.conn.begin()
        try:
            self._set_session(op_id, label, "ctrlz", session=session)

            rowcount = 0
            with self.conn.cursor() as cur:
                cur.execute(sql_text)
                while True:
                    if cur.rowcount and cur.rowcount > 0:
                        rowcount += cur.rowcount
                    if not cur.nextset():
                        break

            captured = self._one(
                "SELECT row_count, capped FROM ctrlz_operations WHERE op_id = %s",
                (op_id,),
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
                self.conn.commit()
                self._clear_session()
                return ExecutionResult(
                    op_id=op_id if captured is not None else None,
                    rowcount=rowcount,
                    committed=True,
                    warnings=warnings,
                )

            self.conn.rollback()
            self._clear_session()
            return ExecutionResult(
                op_id=None, rowcount=rowcount, committed=False, warnings=warnings
            )
        except Exception:
            self.conn.rollback()
            self._clear_session()
            raise

    def _set_session(
        self,
        op_id: str,
        label: Optional[str],
        source: str,
        undo_of: Optional[str] = None,
        session: Optional[dict[str, str]] = None,
    ) -> None:
        settings = session or {}
        risk = settings.get("ctrlz.risk")
        try:
            risk_value: Optional[int] = int(risk) if risk not in (None, "") else None
        except (TypeError, ValueError):
            risk_value = None

        self._exec(
            "SET @ctrlz_op_id = %s, @ctrlz_label = %s, @ctrlz_source = %s, "
            "    @ctrlz_undo_of = %s, @ctrlz_actor_user = %s, @ctrlz_actor_host = %s, "
            "    @ctrlz_actor_app = %s, @ctrlz_ticket = %s, @ctrlz_channel = %s, "
            "    @ctrlz_risk = %s, @ctrlz_policy_outcome = %s",
            (
                op_id, label, source, undo_of,
                settings.get("ctrlz.actor_user") or None,
                settings.get("ctrlz.actor_host") or None,
                settings.get("ctrlz.actor_app") or None,
                settings.get("ctrlz.ticket") or None,
                settings.get("ctrlz.channel") or None,
                risk_value,
                settings.get("ctrlz.policy_outcome") or None,
            ),
        )

    def _clear_session(self) -> None:
        self._exec(
            "SET @ctrlz_op_id = NULL, @ctrlz_label = NULL, @ctrlz_source = NULL, "
            "    @ctrlz_undo_of = NULL, @ctrlz_actor_user = NULL, "
            "    @ctrlz_actor_host = NULL, @ctrlz_actor_app = NULL, "
            "    @ctrlz_ticket = NULL, @ctrlz_channel = NULL, @ctrlz_risk = NULL, "
            "    @ctrlz_policy_outcome = NULL"
        )

    # -- history -----------------------------------------------------------

    def _row_to_operation(self, r: dict) -> Operation:
        tables = [
            x["table_name"]
            for x in self._all(
                "SELECT DISTINCT table_name FROM ctrlz_change_log WHERE op_id = %s "
                "ORDER BY table_name",
                (r["op_id"],),
            )
        ]
        return Operation(
            op_id=r["op_id"],
            label=r["label"],
            source=r["source"],
            actor=r["actor"],
            started_at=_as_datetime(r["started_at"]),
            row_count=r["row_count"],
            capped=bool(r["capped"]),
            undo_of=r["undo_of"],
            undone_at=_as_datetime(r["undone_at"]),
            undone_by=r["undone_by"],
            tables=tables,
            actor_user=r.get("actor_user"),
            actor_host=r.get("actor_host"),
            actor_app=r.get("actor_app"),
            ticket=r.get("ticket"),
            channel=r.get("channel"),
            risk=r.get("risk"),
            policy_outcome=r.get("policy_outcome"),
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
            f"ORDER BY started_at DESC, op_id DESC LIMIT %s",
            (limit,),
        )
        return [self._row_to_operation(r) for r in rows]

    def operation(self, op_id: str) -> Operation:
        self._require_init()
        row = self._one("SELECT * FROM ctrlz_operations WHERE op_id = %s", (op_id,))
        if not row:
            raise UnknownOperation(f"no operation {op_id}")
        return self._row_to_operation(row)

    def changes(self, op_id: str) -> list[Change]:
        self._require_init()
        rows = self._all(
            "SELECT * FROM ctrlz_change_log WHERE op_id = %s ORDER BY seq", (op_id,)
        )
        return [
            Change(
                seq=r["seq"],
                op_id=r["op_id"],
                table_schema="",
                table_name=r["table_name"],
                action=r["action"],
                identity=_decode(r["identity"]),
                before=_decode(r["before"]),
                after=_decode(r["after"]),
                captured_at=_as_datetime(r["captured_at"]),
            )
            for r in rows
        ]

    # -- assessment --------------------------------------------------------

    def _live_image(self, change: Change) -> Optional[dict]:
        """The live row, built with the same expression the trigger used.

        Comparing a decoded image against separately-read column values would
        report every DECIMAL and DATETIME as drifted, because the two paths
        convert differently. One path, one answer.
        """
        table = change.table_name
        image = self._image_expression(table, "")
        where = " AND ".join(f"{self._quote(k)} <=> %s" for k in change.identity)
        rows = self._all(
            f"SELECT {image} AS image FROM {self._quote(table)} WHERE {where}",
            tuple(_bind(v) for v in change.identity.values()),
        )
        return _decode(rows[0]["image"]) if rows else None

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
        blockers.extend(self._cascade_blockers(changes))

        freed = {
            (c.table_name, _key(c.identity)): c.after for c in changes if c.action == INSERT
        }

        verdicts: list[RowVerdict] = []
        for change in changes:
            try:
                current = self._live_image(change)
            except Exception as exc:  # noqa: BLE001
                blockers.append(f"Cannot inspect {change.table_name}: {exc}")
                continue
            verdicts.append(
                RowVerdict(change=change, status=_verdict(change, current, freed),
                           current=current)
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

        self.conn.begin()
        try:
            self._set_session(
                undo_op, label or f"undo of {op_id[:8]}", "ctrlz", undo_of=op_id
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
            self._exec(
                "UPDATE ctrlz_operations SET undone_at = CURRENT_TIMESTAMP(6), "
                "undone_by = %s WHERE op_id = %s",
                (undo_op, op_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._clear_session()

        exists = self._one("SELECT 1 AS ok FROM ctrlz_operations WHERE op_id = %s", (undo_op,))
        return UndoResult(
            op_id=op_id,
            undo_op_id=undo_op if exists else None,
            applied=applied,
            skipped=skipped,
            conflicts_overridden=overridden,
            tables=sorted(touched),
            # InnoDB advances AUTO_INCREMENT when a row is inserted with an
            # explicit higher key, so restoring rows cannot strand the counter.
            sequences_fixed=[],
        )

    def cascade_risks(self) -> dict[str, list[dict]]:
        """Tracked tables whose children InnoDB would remove behind our back.

        Keyed by parent table name, lower-cased. Reported by ``ctrlz doctor``
        so the gap is visible before somebody relies on it, not after.
        """
        rows = self._all(
            "SELECT k.table_name AS child, k.referenced_table_name AS parent, "
            "       k.referenced_column_name AS parent_column, "
            "       r.delete_rule AS delete_rule, r.update_rule AS update_rule, "
            "       k.constraint_name AS name "
            "  FROM information_schema.key_column_usage k "
            "  JOIN information_schema.referential_constraints r "
            "    ON r.constraint_schema = k.constraint_schema "
            "   AND r.constraint_name = k.constraint_name "
            " WHERE k.table_schema = %s AND k.referenced_table_name IS NOT NULL",
            (self.database,),
        )
        risks: dict[str, list[dict]] = {}
        for row in rows:
            if (
                (row["delete_rule"] or "").upper() in INVISIBLE_ACTIONS
                or (row["update_rule"] or "").upper() in INVISIBLE_ACTIONS
            ):
                risks.setdefault((row["parent"] or "").lower(), []).append(row)
        return risks

    def _cascade_blockers(self, changes: list[Change]) -> list[str]:
        """Refuse operations whose cascaded rows we could not have seen."""
        risks = self.cascade_risks()
        if not risks:
            return []

        blockers: list[str] = []
        seen: set[tuple[str, str]] = set()
        for change in changes:
            for entry in risks.get(change.table_name.lower(), []):
                rule = None
                if change.action == DELETE and (
                    entry["delete_rule"] or ""
                ).upper() in INVISIBLE_ACTIONS:
                    rule = f"ON DELETE {entry['delete_rule']}"
                elif change.action == UPDATE and (
                    entry["update_rule"] or ""
                ).upper() in INVISIBLE_ACTIONS:
                    # Only if the referenced key actually changed; an update to
                    # unrelated columns cascades nothing.
                    column = entry["parent_column"]
                    before = (change.before or {}).get(column)
                    after = (change.after or {}).get(column)
                    if before != after:
                        rule = f"ON UPDATE {entry['update_rule']}"
                if rule is None:
                    continue
                key = (change.table_name, entry["child"])
                if key in seen:
                    continue
                seen.add(key)
                blockers.append(
                    f"{change.table_name} has a {rule} foreign key from "
                    f"{entry['child']}, and InnoDB performs cascades without firing "
                    f"triggers -- rows removed from {entry['child']} were never "
                    f"captured, so this operation cannot be reversed completely."
                )
        return blockers

    def _dependency_rank(self, tables: set[str]) -> dict[str, int]:
        rows = self._all(
            "SELECT k.table_name AS child, k.referenced_table_name AS parent "
            "  FROM information_schema.key_column_usage k "
            " WHERE k.table_schema = %s AND k.referenced_table_name IS NOT NULL",
            (self.database,),
        )
        edges = [(r["parent"], r["child"]) for r in rows]
        return topological_rank(tables, edges)

    def _apply_ordered(
        self, todo: list[RowVerdict]
    ) -> tuple[int, list[RowVerdict], set[str]]:
        import pymysql

        tables = {v.change.table_name for v in todo}
        rank = self._dependency_rank(tables)
        pending_rows = order_verdicts(todo, rank, key=lambda v: v.change.table_name)
        applied = 0
        touched: set[str] = set()
        round_number = 0

        while pending_rows:
            deferred: list[RowVerdict] = []
            progress = False
            round_number += 1
            for index, verdict in enumerate(pending_rows):
                savepoint = self._quote(f"ctrlz_{round_number}_{index}")
                self._exec(f"SAVEPOINT {savepoint}")
                try:
                    count = self._apply_inverse(
                        verdict.change, force=verdict.status == DRIFTED
                    )
                except pymysql.err.IntegrityError as exc:
                    if exc.args and exc.args[0] in FK_ERRORS:
                        self._exec(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        deferred.append(verdict)
                        continue
                    raise
                self._exec(f"RELEASE SAVEPOINT {savepoint}")
                progress = True
                if count:
                    applied += count
                    touched.add(verdict.change.table_name)
            if not progress:
                return applied, deferred, touched
            pending_rows = deferred

        return applied, [], touched

    def _apply_inverse(self, change: Change, force: bool) -> int:
        table = self._quote(change.table_name)
        columns = self._column_names(change.table_name)
        ident_sql = " AND ".join(f"{self._quote(k)} <=> %s" for k in change.identity)
        ident_values = [_bind(v) for v in change.identity.values()]

        if change.action == INSERT:
            guard, guard_values = self._guard(change, force)
            return self._exec(
                f"DELETE FROM {table} WHERE {ident_sql}{guard}",
                tuple(ident_values + guard_values),
            )

        if change.action == DELETE:
            image = change.before or {}
            writable = [c for c in self._writable_columns(change.table_name) if c in image]
            names = ", ".join(self._quote(c) for c in writable)
            placeholders = ", ".join("%s" for _ in writable)
            values = [_bind(image.get(c)) for c in writable]
            return self._exec(
                f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(values)
            )

        image = change.before or {}
        writable = [c for c in self._writable_columns(change.table_name) if c in image]
        if not writable:
            return 0
        assignments = ", ".join(f"{self._quote(c)} = %s" for c in writable)
        set_values = [_bind(image.get(c)) for c in writable]
        guard, guard_values = self._guard(change, force)
        return self._exec(
            f"UPDATE {table} SET {assignments} WHERE {ident_sql}{guard}",
            tuple(set_values + ident_values + guard_values),
        )

    def _stored_image(self, seq: int, column: str = "after") -> Optional[str]:
        """The image exactly as the server wrote it, as JSON text.

        Read in a separate statement on purpose. MySQL refuses to let a trigger
        touch a table the invoking statement is already using (error 1442), and
        the capture trigger writes to ctrlz_change_log -- so a guard that read
        the log inline would make every tracked write fail. Fetching it first
        and binding it as a parameter sidesteps the restriction entirely.
        """
        row = self._one(
            f"SELECT `{column}` AS image FROM ctrlz_change_log WHERE seq = %s", (seq,)
        )
        if not row or row["image"] is None:
            return None
        image = row["image"]
        return image.decode("utf-8") if isinstance(image, (bytes, bytearray)) else image

    def _guard(self, change: Change, force: bool) -> tuple[str, list[Any]]:
        """A WHERE fragment asserting the row still holds the values we wrote.

        This is what makes the apply atomic with the drift check: a change
        landing between assessment and now matches no rows instead of
        overwriting somebody else's edit.

        The comparison is image against *stored* image, not column against
        re-serialised value. Rebuilding the image in Python would have to
        re-encode JSON and DECIMAL exactly as MySQL does, and it does not --
        comparing a `json` column to a re-serialised string is simply false, so
        every undo of a row with a JSON column silently matched nothing.
        """
        if force or change.after is None:
            return "", []
        stored = self._stored_image(change.seq, "after")
        if stored is None:
            return "", []
        image = self._image_expression(change.table_name, "")
        return f" AND {image} = CAST(%s AS JSON)", [stored]

    # -- settings ----------------------------------------------------------

    def get_setting(self, key: str) -> Optional[str]:
        self._require_init()
        row = self._one("SELECT `value` FROM ctrlz_settings WHERE `key` = %s", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._require_init()
        self._exec(
            "INSERT INTO ctrlz_settings (`key`, `value`) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)",
            (key, value),
        )

    # -- retention ---------------------------------------------------------

    def purge(self, older_than_seconds: Optional[int] = None) -> int:
        self._require_init()
        if older_than_seconds is None:
            deleted = self._exec("DELETE FROM ctrlz_operations")
        else:
            deleted = self._exec(
                "DELETE FROM ctrlz_operations "
                "WHERE started_at < DATE_SUB(NOW(), INTERVAL %s SECOND)",
                (int(older_than_seconds),),
            )
        self._exec(
            "DELETE FROM ctrlz_change_log "
            "WHERE op_id NOT IN (SELECT op_id FROM ctrlz_operations)"
        )
        return deleted


# -- value encoding --------------------------------------------------------


def _decode(raw: Any) -> Any:
    """Decode a stored JSON image, preserving decimal precision.

    ``parse_float=Decimal`` matters: a DECIMAL column round-tripping through a
    JSON float would silently lose precision on restore, and money columns are
    exactly what people undo.
    """
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        raw = json.loads(raw, parse_float=decimal.Decimal)
    return _revive(raw)


def _revive(obj: Any) -> Any:
    if isinstance(obj, dict):
        if set(obj.keys()) == {BINARY_KEY} and isinstance(obj[BINARY_KEY], str):
            import base64

            return base64.b64decode(obj[BINARY_KEY])
        return {k: _revive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_revive(v) for v in obj]
    return obj


def _bind(value: Any) -> Any:
    """Values go back to MySQL as they came out; JSON structures re-encode."""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _as_datetime(value: Any) -> Optional[datetime]:
    return value if isinstance(value, datetime) else None


def _key(identity: dict[str, Any]) -> tuple:
    return tuple(sorted((k, _hashable(v)) for k, v in identity.items()))


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return value


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
    if expected is None or current is None:
        return expected is current
    for key, value in expected.items():
        if key not in current or current[key] != value:
            return False
    return True
