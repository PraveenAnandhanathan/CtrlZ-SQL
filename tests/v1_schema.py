"""The v0.1 metadata store, frozen.

This is not a copy of the current schema with bits removed -- it is the exact
DDL that ctrlz 0.1 shipped, lifted from the commit that introduced it. The
migration test builds a genuine v1 database with it, captures real history
through v1 triggers, and then upgrades. Anything less would be testing the
migration against our memory of v1 rather than against v1.

Do not "fix" or modernise anything in this file. It is a historical artefact
and its value is that it does not change.
"""

from __future__ import annotations

# -- PostgreSQL -------------------------------------------------------------

V1_POSTGRES_SCHEMA = r"""
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

    INSERT INTO ctrlz.operations (op_id, txid, label, source, actor, undo_of)
    VALUES (
        v_op_id,
        txid_current(),
        nullif(current_setting('ctrlz.label', true), ''),
        coalesce(nullif(current_setting('ctrlz.source', true), ''), 'external'),
        session_user,
        nullif(current_setting('ctrlz.undo_of', true), '')::uuid
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


# -- SQLite -----------------------------------------------------------------

V1_SQLITE_SCHEMA = """
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
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    op_id   TEXT,
    label   TEXT,
    source  TEXT,
    undo_of TEXT
);
INSERT OR IGNORE INTO ctrlz_current_op (id, op_id) VALUES (1, NULL);
"""


def v1_sqlite_trigger(table: str, action: str, identity_expr: str,
                      before: str, after: str) -> str:
    """The v0.1 SQLite capture trigger, verbatim."""
    return f'''
        CREATE TRIGGER "ctrlz_capture_{action.lower()}_{table}"
        AFTER {action} ON "{table}" FOR EACH ROW
        BEGIN
            UPDATE ctrlz_current_op
               SET op_id = lower(hex(randomblob(16))), source = 'external'
             WHERE id = 1 AND op_id IS NULL;

            INSERT OR IGNORE INTO ctrlz_operations
                (op_id, label, source, actor, undo_of)
            SELECT op_id, label, coalesce(source, 'external'), 'sqlite', undo_of
              FROM ctrlz_current_op WHERE id = 1;

            UPDATE ctrlz_operations SET row_count = row_count + 1
             WHERE op_id = (SELECT op_id FROM ctrlz_current_op WHERE id = 1);

            INSERT INTO ctrlz_change_log
                (op_id, table_name, action, identity, before, after)
            SELECT c.op_id, '{table}', '{action[0]}',
                   {identity_expr}, {before}, {after}
              FROM ctrlz_current_op c
             WHERE c.id = 1
               AND (SELECT capped FROM ctrlz_operations WHERE op_id = c.op_id) = 0;
        END;
    '''
