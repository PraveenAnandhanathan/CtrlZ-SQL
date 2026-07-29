"""PostgreSQL engine.

Capture is done with row-level AFTER triggers that store ``to_jsonb(OLD)`` and
``to_jsonb(NEW)``. This is the whole trick, and it is why ctrlz does not ship a
SQL parser:

* the images are exact, whatever the statement looked like;
* they are written inside the same transaction as the change, so there is no
  race between "read the old values" and "write the new ones";
* changes made by triggers, rules and ``ON DELETE CASCADE`` show up as their
  own rows, so reversing them is automatic rather than a special case.

Applying the inverse uses ``jsonb_populate_record``, which hands the stored
image back to Postgres and lets *it* do the type conversion. No value is ever
rendered into SQL text by this module, so arrays, ranges, enums, composites,
``jsonb`` columns and user-defined types all round-trip without special code.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

import psycopg2
import psycopg2.extras
from psycopg2 import sql

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
from .base import Engine, split_qualified

TRIGGER_NAME = "ctrlz_capture"

SCHEMA_SQL = r"""
CREATE SCHEMA IF NOT EXISTS ctrlz;

CREATE TABLE IF NOT EXISTS ctrlz.settings (
    key   text PRIMARY KEY,
    value text NOT NULL
);

INSERT INTO ctrlz.settings (key, value) VALUES ('schema_version', '1')
    ON CONFLICT (key) DO NOTHING;
INSERT INTO ctrlz.settings (key, value) VALUES ('max_rows_per_operation', '100000')
    ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS ctrlz.tracked (
    table_schema text   NOT NULL,
    table_name   text   NOT NULL,
    identity     text[] NOT NULL,
    tracked_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_schema, table_name)
);

CREATE TABLE IF NOT EXISTS ctrlz.operations (
    op_id      uuid PRIMARY KEY,
    txid       bigint NOT NULL,
    label      text,
    source     text NOT NULL DEFAULT 'external',
    actor      text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    row_count  integer NOT NULL DEFAULT 0,
    capped     boolean NOT NULL DEFAULT false,
    undo_of    uuid,
    undone_at  timestamptz,
    undone_by  uuid
);

CREATE TABLE IF NOT EXISTS ctrlz.change_log (
    seq          bigserial PRIMARY KEY,
    op_id        uuid NOT NULL REFERENCES ctrlz.operations(op_id) ON DELETE CASCADE,
    table_schema text NOT NULL,
    table_name   text NOT NULL,
    action       char(1) NOT NULL,
    identity     jsonb NOT NULL,
    before       jsonb,
    after        jsonb,
    captured_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS change_log_op_idx ON ctrlz.change_log (op_id, seq);
CREATE INDEX IF NOT EXISTS operations_started_idx ON ctrlz.operations (started_at DESC);

-- gen_random_uuid() is built in from PG13; fall back for older servers.
CREATE OR REPLACE FUNCTION ctrlz.new_op_id() RETURNS uuid LANGUAGE plpgsql AS $$
BEGIN
    RETURN gen_random_uuid();
EXCEPTION WHEN undefined_function THEN
    RETURN md5(random()::text || clock_timestamp()::text || pg_backend_pid()::text)::uuid;
END $$;
"""

# Recreated on every initialize(), after migrations, so the function always
# matches the columns that actually exist.
CAPTURE_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION ctrlz.capture() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER AS $fn$
DECLARE
    v_op_id  uuid;
    v_before jsonb;
    v_after  jsonb;
    v_src    jsonb;
    v_ident  jsonb;
    v_count  integer;
    v_max    integer;
BEGIN
    -- One operation per transaction. set_config(..., true) is transaction
    -- scoped, so the id resets automatically at COMMIT or ROLLBACK.
    v_op_id := nullif(current_setting('ctrlz.op_id', true), '')::uuid;
    IF v_op_id IS NULL THEN
        v_op_id := ctrlz.new_op_id();
        PERFORM set_config('ctrlz.op_id', v_op_id::text, true);
    END IF;

    INSERT INTO ctrlz.operations (
        op_id, txid, label, source, actor, undo_of,
        actor_user, actor_host, actor_app, ticket, channel, risk, policy_outcome
    )
    VALUES (
        v_op_id,
        txid_current(),
        nullif(current_setting('ctrlz.label', true), ''),
        coalesce(nullif(current_setting('ctrlz.source', true), ''), 'external'),
        session_user,
        nullif(current_setting('ctrlz.undo_of', true), '')::uuid,
        -- Attribution. Absent for a change made by a client that never set it;
        -- NULL is the honest answer there, not a guess.
        nullif(current_setting('ctrlz.actor_user', true), ''),
        nullif(current_setting('ctrlz.actor_host', true), ''),
        nullif(current_setting('ctrlz.actor_app', true), ''),
        nullif(current_setting('ctrlz.ticket', true), ''),
        nullif(current_setting('ctrlz.channel', true), ''),
        nullif(current_setting('ctrlz.risk', true), '')::integer,
        nullif(current_setting('ctrlz.policy_outcome', true), '')
    )
    ON CONFLICT (op_id) DO NOTHING;

    SELECT value::integer INTO v_max FROM ctrlz.settings
     WHERE key = 'max_rows_per_operation';
    v_max := coalesce(v_max, 100000);

    UPDATE ctrlz.operations SET row_count = row_count + 1
     WHERE op_id = v_op_id
     RETURNING row_count INTO v_count;

    -- Past the cap we stop storing images and mark the operation, so it is
    -- reported as not-undoable instead of silently half-undoable.
    IF v_count > v_max THEN
        UPDATE ctrlz.operations SET capped = true, row_count = v_max
         WHERE op_id = v_op_id;
        RETURN coalesce(NEW, OLD);
    END IF;

    IF TG_OP <> 'DELETE' THEN v_after  := to_jsonb(NEW); END IF;
    IF TG_OP <> 'INSERT' THEN v_before := to_jsonb(OLD); END IF;
    v_src := coalesce(v_after, v_before);

    SELECT jsonb_object_agg(c, v_src -> c) INTO v_ident
      FROM unnest(string_to_array(TG_ARGV[0], ',')) AS c;

    INSERT INTO ctrlz.change_log
        (op_id, table_schema, table_name, action, identity, before, after)
    VALUES
        (v_op_id, TG_TABLE_SCHEMA, TG_TABLE_NAME, left(TG_OP, 1), v_ident, v_before, v_after);

    RETURN coalesce(NEW, OLD);
END $fn$;
"""


class PostgresEngine(Engine):
    name = "postgresql"
    caveats = (
        "TRUNCATE does not fire row triggers and cannot be captured.",
        "DDL is not captured; schema changes are outside the undo history.",
    )

    def __init__(self, dsn: str, default_schema: str = "public"):
        self.dsn = dsn
        self.default_schema = default_schema
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    # -- plumbing ----------------------------------------------------------

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _cur(self):
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def _one(self, query, params=None):
        with self._cur() as cur:
            cur.execute(query, params)
            return cur.fetchone()

    def _all(self, query, params=None):
        with self._cur() as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def _regclass(self, schema: str, table: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))

    def _require_init(self) -> None:
        if not self.is_initialized():
            raise NotInitialized("ctrlz is not installed in this database; run: ctrlz init")

    # -- lifecycle ---------------------------------------------------------

    def is_initialized(self) -> bool:
        row = self._one(
            "SELECT 1 AS ok FROM pg_namespace WHERE nspname = 'ctrlz'"
        )
        if not row:
            return False
        row = self._one(
            "SELECT 1 AS ok FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'ctrlz' AND c.relname = 'change_log'"
        )
        return bool(row)

    def initialize(self) -> None:
        """Create or upgrade the metadata store.

        The whole chain runs every time, fresh installs included, so the
        upgrade path is exercised constantly rather than only by its own test.
        """
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            for migration in pending(self.schema_version()):
                for statement in migration.postgres:
                    cur.execute(statement)
            # Last, so the trigger function always matches the columns that
            # now exist.
            cur.execute(CAPTURE_FUNCTION_SQL)
            cur.execute(
                "INSERT INTO ctrlz.settings (key, value) VALUES ('schema_version', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (str(CURRENT_VERSION),),
            )

    def schema_version(self) -> int:
        row = self._one("SELECT value FROM ctrlz.settings WHERE key = 'schema_version'")
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def uninstall(self) -> None:
        if not self.is_initialized():
            return
        for qualified, _identity in self.tracked():
            try:
                self.untrack(qualified)
            except Exception:
                pass
        with self.conn.cursor() as cur:
            cur.execute("DROP SCHEMA ctrlz CASCADE")

    # -- introspection -----------------------------------------------------

    def tables(self) -> list[str]:
        rows = self._all(
            """
            SELECT n.nspname AS schema, c.relname AS name
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind IN ('r', 'p')
               AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'ctrlz')
               AND n.nspname NOT LIKE 'pg_toast%%'
             ORDER BY 1, 2
            """
        )
        return [f"{r['schema']}.{r['name']}" for r in rows]

    def _columns(self, schema: str, table: str) -> list[dict[str, Any]]:
        rows = self._all(
            """
            SELECT a.attname AS name,
                   a.attgenerated AS generated,
                   a.attidentity  AS identity
              FROM pg_attribute a
             WHERE a.attrelid = format('%%I.%%I', %s, %s)::regclass
               AND a.attnum > 0
               AND NOT a.attisdropped
             ORDER BY a.attnum
            """,
            (schema, table),
        )
        if not rows:
            raise NotTracked(f"table {schema}.{table} has no columns or does not exist")
        return [dict(r) for r in rows]

    def _writable_columns(self, schema: str, table: str) -> list[str]:
        # Columns declared GENERATED ALWAYS AS (...) STORED are computed by the
        # server and rejected in an INSERT/UPDATE column list.
        return [c["name"] for c in self._columns(schema, table) if c["generated"] != "s"]

    def _updatable_columns(self, schema: str, table: str) -> list[str]:
        # GENERATED ALWAYS AS IDENTITY columns are writable in an INSERT (with
        # OVERRIDING SYSTEM VALUE) but rejected outright in an UPDATE. They can
        # never have changed under us for the same reason, so dropping them
        # from the SET list is safe as well as necessary.
        return [
            c["name"]
            for c in self._columns(schema, table)
            if c["generated"] != "s" and c["identity"] != "a"
        ]

    def _has_always_identity(self, schema: str, table: str) -> bool:
        return any(c["identity"] == "a" for c in self._columns(schema, table))

    def _detect_identity(self, schema: str, table: str) -> list[str]:
        rows = self._all(
            """
            SELECT a.attname AS name
              FROM pg_index i
              JOIN pg_attribute a
                ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
             WHERE i.indrelid = format('%%I.%%I', %s, %s)::regclass
               AND i.indisprimary
             ORDER BY array_position(i.indkey, a.attnum)
            """,
            (schema, table),
        )
        if rows:
            return [r["name"] for r in rows]

        # No primary key: a unique index over NOT NULL columns is just as good
        # an identity for our purposes.
        rows = self._all(
            """
            SELECT i.indexrelid::regclass::text AS idx,
                   array_agg(a.attname ORDER BY array_position(i.indkey, a.attnum)) AS cols,
                   bool_and(a.attnotnull) AS all_not_null
              FROM pg_index i
              JOIN pg_attribute a
                ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
             WHERE i.indrelid = format('%%I.%%I', %s, %s)::regclass
               AND i.indisunique AND i.indisvalid AND i.indpred IS NULL
             GROUP BY i.indexrelid
            """,
            (schema, table),
        )
        for r in rows:
            if r["all_not_null"]:
                return list(r["cols"])

        raise NoIdentity(
            f"{schema}.{table} has no primary key or NOT NULL unique index, so rows "
            f"cannot be identified for undo. Re-run with --identity col1,col2 if you "
            f"know a column set that is unique."
        )

    # -- tracking ----------------------------------------------------------

    def track(self, table: str, identity: Optional[list[str]] = None) -> list[str]:
        self._require_init()
        schema, name = split_qualified(table, self.default_schema)
        columns = {c["name"] for c in self._columns(schema, name)}

        if identity:
            missing = [c for c in identity if c not in columns]
            if missing:
                raise NoIdentity(
                    f"{schema}.{name} has no column(s) {', '.join(missing)}"
                )
            ident = list(identity)
        else:
            ident = self._detect_identity(schema, name)

        target = self._regclass(schema, name)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
                    sql.Identifier(TRIGGER_NAME), target
                )
            )
            cur.execute(
                sql.SQL(
                    "CREATE TRIGGER {} AFTER INSERT OR UPDATE OR DELETE ON {} "
                    "FOR EACH ROW EXECUTE FUNCTION ctrlz.capture({})"
                ).format(
                    sql.Identifier(TRIGGER_NAME), target, sql.Literal(",".join(ident))
                )
            )
            cur.execute(
                """
                INSERT INTO ctrlz.tracked (table_schema, table_name, identity)
                VALUES (%s, %s, %s)
                ON CONFLICT (table_schema, table_name)
                DO UPDATE SET identity = EXCLUDED.identity, tracked_at = now()
                """,
                (schema, name, ident),
            )
        return ident

    def untrack(self, table: str) -> None:
        self._require_init()
        schema, name = split_qualified(table, self.default_schema)
        with self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
                    sql.Identifier(TRIGGER_NAME), self._regclass(schema, name)
                )
            )
            cur.execute(
                "DELETE FROM ctrlz.tracked WHERE table_schema = %s AND table_name = %s",
                (schema, name),
            )

    def tracked(self) -> list[tuple[str, list[str]]]:
        self._require_init()
        rows = self._all(
            "SELECT table_schema, table_name, identity FROM ctrlz.tracked ORDER BY 1, 2"
        )
        return [(f"{r['table_schema']}.{r['table_name']}", list(r["identity"])) for r in rows]

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
        op_id = str(uuid.uuid4())
        warnings: list[str] = []
        self.conn.autocommit = False
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT set_config('ctrlz.op_id', %s, true)", (op_id,))
                cur.execute("SELECT set_config('ctrlz.label', %s, true)", (label or "",))
                cur.execute("SELECT set_config('ctrlz.source', 'ctrlz', true)")
                # Attribution and the policy verdict, carried into the capture
                # trigger as transaction-scoped settings. Plain strings only:
                # the engine layer must not import analysis or policy.
                for key, value in (session or {}).items():
                    cur.execute("SELECT set_config(%s, %s, true)", (key, str(value)))
                cur.execute(sql_text)
                rowcount = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
                cur.execute(
                    "SELECT row_count, capped FROM ctrlz.operations WHERE op_id = %s",
                    (op_id,),
                )
                captured = cur.fetchone()

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
                elif captured is not None and captured[1]:
                    warnings.append(
                        "Operation exceeded the capture limit and is NOT undoable."
                    )
                self.conn.commit()
                return ExecutionResult(
                    op_id=op_id if captured is not None else None,
                    rowcount=rowcount,
                    committed=True,
                    warnings=warnings,
                )

            self.conn.rollback()
            return ExecutionResult(
                op_id=None, rowcount=rowcount, committed=False, warnings=warnings
            )
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self.conn.autocommit = True

    # -- history -----------------------------------------------------------

    def _row_to_operation(self, r) -> Operation:
        return Operation(
            op_id=str(r["op_id"]),
            label=r["label"],
            source=r["source"],
            actor=r["actor"],
            started_at=r["started_at"],
            row_count=r["row_count"],
            capped=r["capped"],
            undo_of=str(r["undo_of"]) if r["undo_of"] else None,
            undone_at=r["undone_at"],
            undone_by=str(r["undone_by"]) if r["undone_by"] else None,
            tables=list(r.get("tables") or []),
            actor_user=r.get("actor_user"),
            actor_host=r.get("actor_host"),
            actor_app=r.get("actor_app"),
            ticket=r.get("ticket"),
            channel=r.get("channel"),
            risk=r.get("risk"),
            policy_outcome=r.get("policy_outcome"),
        )

    _OP_SELECT = """
        SELECT o.*,
               (SELECT array_agg(DISTINCT c.table_schema || '.' || c.table_name)
                  FROM ctrlz.change_log c WHERE c.op_id = o.op_id) AS tables
          FROM ctrlz.operations o
    """

    def operations(
        self, limit: int = 20, include_undone: bool = True, include_undos: bool = True
    ) -> list[Operation]:
        self._require_init()
        clauses = []
        if not include_undone:
            clauses.append("o.undone_at IS NULL")
        if not include_undos:
            clauses.append("o.undo_of IS NULL")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._all(
            f"{self._OP_SELECT} {where} ORDER BY o.started_at DESC, o.op_id LIMIT %s",
            (limit,),
        )
        return [self._row_to_operation(r) for r in rows]

    def operation(self, op_id: str) -> Operation:
        self._require_init()
        row = self._one(f"{self._OP_SELECT} WHERE o.op_id = %s", (op_id,))
        if not row:
            raise UnknownOperation(f"no operation {op_id}")
        return self._row_to_operation(row)

    def changes(self, op_id: str) -> list[Change]:
        self._require_init()
        rows = self._all(
            "SELECT * FROM ctrlz.change_log WHERE op_id = %s ORDER BY seq", (op_id,)
        )
        return [
            Change(
                seq=r["seq"],
                op_id=str(r["op_id"]),
                table_schema=r["table_schema"],
                table_name=r["table_name"],
                action=r["action"],
                identity=r["identity"],
                before=r["before"],
                after=r["after"],
                captured_at=r["captured_at"],
            )
            for r in rows
        ]

    # -- assessment --------------------------------------------------------

    def _live_rows(
        self, schema: str, table: str, changes: list[Change]
    ) -> dict[int, Optional[dict]]:
        """Fetch the current image of every row a change refers to.

        One query per table. The identity values are handed back to Postgres as
        jsonb and re-typed by ``jsonb_populate_record``, so this works for
        composite keys of any type without building SQL literals.
        """
        identity_cols = list(changes[0].identity.keys())
        target = self._regclass(schema, table)
        payload = psycopg2.extras.Json(
            [{"seq": c.seq, "ident": c.identity} for c in changes]
        )
        match = sql.SQL(" AND ").join(
            sql.SQL("t.{col} IS NOT DISTINCT FROM p.{col}").format(col=sql.Identifier(col))
            for col in identity_cols
        )
        query = sql.SQL(
            """
            WITH want AS (
                SELECT (e ->> 'seq')::bigint AS seq, e -> 'ident' AS ident
                  FROM jsonb_array_elements(%s::jsonb) e
            )
            SELECT w.seq AS seq, to_jsonb(t) AS current
              FROM want w
              CROSS JOIN LATERAL jsonb_populate_record(NULL::{target}, w.ident) p
              LEFT JOIN {target} t ON {match}
            """
        ).format(target=target, match=match)
        with self._cur() as cur:
            cur.execute(query, (payload,))
            return {r["seq"]: r["current"] for r in cur.fetchall()}

    def _dependency_rank(self, tables: set[tuple[str, str]]) -> dict[tuple[str, str], int]:
        """Rank tables so that parents sort before children.

        Undoing a DELETE has to insert parents first; undoing an INSERT has to
        delete children first. Both fall out of this one ordering.
        """
        if not tables:
            return {}
        rows = self._all(
            """
            SELECT srcns.nspname AS child_schema, src.relname AS child_name,
                   tgtns.nspname AS parent_schema, tgt.relname AS parent_name
              FROM pg_constraint c
              JOIN pg_class src        ON src.oid = c.conrelid
              JOIN pg_namespace srcns  ON srcns.oid = src.relnamespace
              JOIN pg_class tgt        ON tgt.oid = c.confrelid
              JOIN pg_namespace tgtns  ON tgtns.oid = tgt.relnamespace
             WHERE c.contype = 'f'
            """
        )
        edges = [
            ((r["parent_schema"], r["parent_name"]), (r["child_schema"], r["child_name"]))
            for r in rows
        ]
        return topological_rank(tables, edges)

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

        by_table: dict[tuple[str, str], list[Change]] = {}
        for c in changes:
            by_table.setdefault((c.table_schema, c.table_name), []).append(c)

        # Identities this same operation inserted. Undoing it will free them,
        # so a DELETE we want to reverse into one of them is not a conflict --
        # it is just a row this operation replaced.
        freed = {
            (c.table_schema, c.table_name, _key(c.identity)): c.after
            for c in changes
            if c.action == INSERT
        }

        verdicts: list[RowVerdict] = []
        for (schema, table), group in by_table.items():
            try:
                live = self._live_rows(schema, table, group)
            except psycopg2.Error as exc:
                self.conn.rollback()
                blockers.append(f"Cannot inspect {schema}.{table}: {exc}".strip())
                continue
            for c in group:
                current = live.get(c.seq)
                verdicts.append(
                    RowVerdict(change=c, status=_verdict(c, current, freed), current=current)
                )

        verdicts.sort(key=lambda v: v.change.seq)
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

        undo_op = str(uuid.uuid4())
        applied = skipped = overridden = 0
        touched: set[tuple[str, str]] = set()
        reinserted: set[tuple[str, str]] = set()

        self.conn.autocommit = False
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT set_config('ctrlz.op_id', %s, true)", (undo_op,))
                cur.execute("SELECT set_config('ctrlz.undo_of', %s, true)", (op_id,))
                cur.execute("SELECT set_config('ctrlz.source', 'ctrlz', true)")
                cur.execute(
                    "SELECT set_config('ctrlz.label', %s, true)",
                    (label or f"undo of {op_id[:8]}",),
                )
                # Helps for constraints that are declared DEFERRABLE; the
                # ordering below is what makes the rest work.
                cur.execute("SET CONSTRAINTS ALL DEFERRED")

                todo: list[RowVerdict] = []
                for verdict in assessment.verdicts:
                    if verdict.status == MISSING:
                        # Row is already gone: reverting or deleting it is a
                        # no-op, not a failure.
                        skipped += 1
                        continue
                    if verdict.status == OCCUPIED:
                        # Never destroy whatever took the identity back.
                        skipped += 1
                        continue
                    if verdict.status == DRIFTED:
                        overridden += 1
                    todo.append(verdict)

                applied, failed, touched, reinserted = self._apply_ordered(cur, todo)
                if failed:
                    detail = ", ".join(
                        f"{v.change.qualified_name} {v.change.identity}" for v in failed[:3]
                    )
                    raise NotUndoable(
                        f"{len(failed)} row(s) could not be restored without violating a "
                        f"constraint ({detail}). Nothing was changed."
                    )
                skipped += len(todo) - applied

                cur.execute(
                    "UPDATE ctrlz.operations SET undone_at = clock_timestamp(), undone_by = %s "
                    "WHERE op_id = %s",
                    (undo_op, op_id),
                )
            fixed = self._resync_sequences(reinserted)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self.conn.autocommit = True

        exists = self._one("SELECT 1 AS ok FROM ctrlz.operations WHERE op_id = %s", (undo_op,))
        return UndoResult(
            op_id=op_id,
            undo_op_id=undo_op if exists else None,
            applied=applied,
            skipped=skipped,
            conflicts_overridden=overridden,
            tables=sorted(f"{s}.{t}" for s, t in touched),
            sequences_fixed=fixed,
        )

    def _apply_ordered(
        self, cur, todo: list[RowVerdict]
    ) -> tuple[int, list[RowVerdict], set[tuple[str, str]], set[tuple[str, str]]]:
        """Apply inverses in an order that keeps foreign keys satisfied.

        Three phases, because the safe order differs by action:

        1. Remove rows the operation inserted -- children before parents.
        2. Restore rows the operation deleted -- parents before children.
        3. Revert updates, once every row they might reference exists again.

        Rows that still fail on a constraint (self-references, circular FKs)
        go into a retry queue and the whole set is re-attempted while progress
        is being made. Each row runs inside a savepoint, so a failed attempt
        costs nothing.
        """
        tables = {(v.change.table_schema, v.change.table_name) for v in todo}
        rank = self._dependency_rank(tables)
        pending = order_verdicts(todo, rank)
        applied = 0
        touched: set[tuple[str, str]] = set()
        reinserted: set[tuple[str, str]] = set()

        while pending:
            deferred: list[RowVerdict] = []
            progress = False
            for verdict in pending:
                change = verdict.change
                cur.execute("SAVEPOINT ctrlz_row")
                try:
                    count = self._apply_inverse(
                        cur, change, force=verdict.status == DRIFTED
                    )
                except psycopg2.errors.ForeignKeyViolation:
                    cur.execute("ROLLBACK TO SAVEPOINT ctrlz_row")
                    deferred.append(verdict)
                    continue
                cur.execute("RELEASE SAVEPOINT ctrlz_row")
                progress = True
                if count:
                    applied += count
                    touched.add((change.table_schema, change.table_name))
                    if change.action == DELETE:
                        reinserted.add((change.table_schema, change.table_name))
            if not progress:
                return applied, deferred, touched, reinserted
            pending = deferred

        return applied, [], touched, reinserted

    def _apply_inverse(self, cur, change: Change, force: bool) -> int:
        target = self._regclass(change.table_schema, change.table_name)
        identity_cols = list(change.identity.keys())

        if change.action == INSERT:
            # Undo an INSERT by deleting the row, but only if it still looks
            # exactly like the row we inserted.
            match = sql.SQL(" AND ").join(
                sql.SQL("t.{col} IS NOT DISTINCT FROM j.{col}").format(col=sql.Identifier(c))
                for c in identity_cols
            )
            query = sql.SQL(
                "DELETE FROM {target} t "
                "USING jsonb_populate_record(NULL::{target}, %(image)s::jsonb) j "
                "WHERE {match} AND (%(force)s::boolean OR to_jsonb(t) = %(image)s::jsonb)"
            ).format(target=target, match=match)
            cur.execute(
                query,
                {"image": psycopg2.extras.Json(change.after), "force": force},
            )
            return cur.rowcount

        if change.action == DELETE:
            cols = self._writable_columns(change.table_schema, change.table_name)
            col_list = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
            overriding = (
                sql.SQL("OVERRIDING SYSTEM VALUE")
                if self._has_always_identity(change.table_schema, change.table_name)
                else sql.SQL("")
            )
            query = sql.SQL(
                "INSERT INTO {target} ({cols}) {overriding} "
                "SELECT {cols} FROM jsonb_populate_record(NULL::{target}, %(image)s::jsonb)"
            ).format(target=target, cols=col_list, overriding=overriding)
            cur.execute(query, {"image": psycopg2.extras.Json(change.before)})
            return cur.rowcount

        # UPDATE: put the old values back, keyed on the row still holding the
        # values we wrote.
        cols = self._updatable_columns(change.table_schema, change.table_name)
        if not cols:
            return 0
        col_list = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
        match = sql.SQL(" AND ").join(
            sql.SQL("t.{col} IS NOT DISTINCT FROM j.{col}").format(col=sql.Identifier(c))
            for c in identity_cols
        )
        source = sql.SQL(
            "(SELECT {cols} FROM jsonb_populate_record(NULL::{target}, %(before)s::jsonb))"
        ).format(cols=col_list, target=target)
        assignment = (
            sql.SQL("{col} = {source}").format(col=sql.Identifier(cols[0]), source=source)
            if len(cols) == 1
            else sql.SQL("({cols}) = {source}").format(cols=col_list, source=source)
        )
        query = sql.SQL(
            "UPDATE {target} t SET {assignment} "
            "FROM jsonb_populate_record(NULL::{target}, %(after)s::jsonb) j "
            "WHERE {match} AND (%(force)s::boolean OR to_jsonb(t) = %(after)s::jsonb)"
        ).format(target=target, assignment=assignment, match=match)
        cur.execute(
            query,
            {
                "before": psycopg2.extras.Json(change.before),
                "after": psycopg2.extras.Json(change.after),
                "force": force,
            },
        )
        return cur.rowcount

    def _resync_sequences(self, tables: set[tuple[str, str]]) -> list[str]:
        """Push identity/serial sequences past any primary keys we restored.

        Re-inserting a row with an explicit key does not advance the sequence
        that generated it, so without this the next INSERT collides.
        """
        fixed: list[str] = []
        for schema, table in sorted(tables):
            qualified = f"{schema}.{table}"
            rows = self._all(
                """
                SELECT a.attname AS name,
                       pg_get_serial_sequence(format('%%I.%%I', %s, %s), a.attname) AS seq
                  FROM pg_attribute a
                 WHERE a.attrelid = format('%%I.%%I', %s, %s)::regclass
                   AND a.attnum > 0 AND NOT a.attisdropped
                """,
                (schema, table, schema, table),
            )
            for r in rows:
                if not r["seq"]:
                    continue
                with self._cur() as cur:
                    cur.execute(
                        sql.SQL("SELECT max({col})::bigint AS m FROM {target}").format(
                            col=sql.Identifier(r["name"]),
                            target=self._regclass(schema, table),
                        )
                    )
                    top = cur.fetchone()["m"]
                    if top is None:
                        continue
                    cur.execute(
                        "SELECT last_value FROM pg_sequences "
                        "WHERE schemaname || '.' || sequencename = %s",
                        (r["seq"].replace('"', ""),),
                    )
                    seq_row = cur.fetchone()
                    current = seq_row["last_value"] if seq_row else None
                    if current is not None and current >= top:
                        continue
                    cur.execute("SELECT setval(%s, %s, true)", (r["seq"], top))
                    fixed.append(f"{qualified}.{r['name']}")
        return fixed

    # -- settings ----------------------------------------------------------

    def get_setting(self, key: str) -> Optional[str]:
        self._require_init()
        row = self._one("SELECT value FROM ctrlz.settings WHERE key = %s", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._require_init()
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ctrlz.settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )

    # -- retention ---------------------------------------------------------

    def purge(self, older_than_seconds: Optional[int] = None) -> int:
        self._require_init()
        with self.conn.cursor() as cur:
            if older_than_seconds is None:
                cur.execute("DELETE FROM ctrlz.operations")
            else:
                cur.execute(
                    "DELETE FROM ctrlz.operations "
                    "WHERE started_at < now() - make_interval(secs => %s)",
                    (older_than_seconds,),
                )
            return cur.rowcount


def _key(identity: dict[str, Any]) -> tuple:
    return tuple(sorted((k, _hashable(v)) for k, v in identity.items()))


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    return value


def _verdict(
    change: Change, current: Optional[dict], freed: Optional[dict] = None
) -> str:
    """Compare a captured change against the live row."""
    if change.action == DELETE:
        # We want to put the row back, so the identity has to be free.
        if current is None:
            return CLEAN
        slot = (change.table_schema, change.table_name, _key(change.identity))
        if freed and slot in freed and freed[slot] == current:
            # This operation deleted a row and inserted another over the same
            # identity. Undoing removes the newcomer first, so the slot is free
            # by the time we get here.
            return CLEAN
        return OCCUPIED
    if current is None:
        return MISSING
    return CLEAN if current == change.after else DRIFTED
