"""The control plane: one searchable place for many databases' history.

    ctrlz ship --to sqlite:///team-history.db
    ctrlz hub log

> 🟢 Every database keeps its own recording. This collects copies of those
> recordings into one place a team can search — who changed what, where, and
> when — without having to log into each database in turn.

Three rules shape everything here, and all three are about not letting a
convenience become a liability.

**It is a replica, never the source of truth.** Shipping crosses a transaction
boundary and can fail, be interrupted, or lag. The in-database change log stays
authoritative. If the hub and the database disagree, the database is right.

**Undo is never orchestrated from here.** The hub records where an operation
happened; reversing it means connecting to that database and running
``ctrlz undo`` against it. A hub that could undo would be a hub that could undo
*stale* data, and there is no way for it to know it was stale.

**Metadata by default, row values only on request.** Shipping before-and-after
images off the database they came from is a data-governance decision somebody
should make deliberately, not a default they discover later. Without
``--include-values`` the hub learns what changed and by how much, never what
the values were.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from .errors import ConfigError

#: Rows pulled from the source per round trip.
DEFAULT_BATCH = 1000


@dataclass
class ShipResult:
    """What one run of the shipper moved."""

    source_id: str
    source_name: str
    operations: int = 0
    changes: int = 0
    refreshed: int = 0
    watermark: int = 0
    batches: int = 1
    included_values: bool = False

    @property
    def moved_anything(self) -> bool:
        return bool(self.operations or self.changes or self.refreshed)


@dataclass
class HubOperation:
    """An operation as the hub remembers it."""

    source_id: str
    source_name: str
    op_id: str
    label: Optional[str]
    actor: str
    started_at: Optional[datetime]
    row_count: int
    tables: list[str] = field(default_factory=list)
    risk: Optional[int] = None
    policy_outcome: Optional[str] = None
    ticket: Optional[str] = None
    channel: Optional[str] = None
    undone_at: Optional[datetime] = None
    capped: bool = False

    @property
    def already_undone(self) -> bool:
        return self.undone_at is not None


# -- storage ---------------------------------------------------------------


class Hub:
    """A store of shipped history. SQLite or PostgreSQL.

    Deliberately not the same code path as an ``Engine``: an engine captures
    and reverses changes, a hub only files copies. Reusing the engine here
    would tempt somebody into calling ``undo`` on it.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        scheme = urlparse(dsn).scheme.lower()
        if scheme in ("postgres", "postgresql", "psql"):
            import psycopg2

            self.kind = "postgres"
            self.conn = psycopg2.connect(dsn)
            self.conn.autocommit = True
        elif scheme in ("sqlite", "", "file"):
            import sqlite3

            self.kind = "sqlite"
            self.conn = sqlite3.connect(_sqlite_path(dsn), isolation_level=None)
            self.conn.row_factory = sqlite3.Row
        else:
            raise ConfigError(
                f"the control plane supports sqlite and postgresql, not {scheme!r}"
            )
        self._create()

    # -- plumbing ----------------------------------------------------------

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _sql(self, query: str) -> str:
        """Translate the internal ``?`` placeholder style for the backend."""
        return query.replace("?", "%s") if self.kind == "postgres" else query

    def _exec(self, query: str, params: tuple = ()) -> None:
        with self._cursor() as cur:
            cur.execute(self._sql(query), params)

    def _all(self, query: str, params: tuple = ()) -> list[dict]:
        with self._cursor(dict_rows=True) as cur:
            cur.execute(self._sql(query), params)
            return [dict(r) for r in cur.fetchall()]

    def _one(self, query: str, params: tuple = ()) -> Optional[dict]:
        rows = self._all(query, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self):
        """Write a batch atomically, and at a sane speed.

        Without this every shipped row is its own committed transaction, which
        costs a disk flush each: 3000 rows took 10.7s one-at-a-time and 0.2s
        in a batch. It is also the more correct shape -- the rows and the
        watermark that says they were stored land together, so an interruption
        leaves the hub consistent rather than merely re-shippable.
        """
        if self.kind == "postgres":
            self.conn.autocommit = False
            try:
                yield
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                self.conn.autocommit = True
        else:
            self._exec("BEGIN")
            try:
                yield
                self._exec("COMMIT")
            except Exception:
                self._exec("ROLLBACK")
                raise

    def _cursor(self, dict_rows: bool = False):
        if self.kind == "postgres":
            import psycopg2.extras

            return self.conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None
            )
        return _SqliteCursor(self.conn)

    def _create(self) -> None:
        timestamp = "timestamptz" if self.kind == "postgres" else "TEXT"
        integer = "integer" if self.kind == "postgres" else "INTEGER"
        text = "text" if self.kind == "postgres" else "TEXT"
        # Kept as an integer on both backends: the shipper writes 0/1, and a
        # boolean column with an integer default is a Postgres type error.
        boolean = integer

        for statement in (
            f"""
            CREATE TABLE IF NOT EXISTS ctrlz_hub_sources (
                source_id  {text} PRIMARY KEY,
                name       {text} NOT NULL,
                engine     {text},
                dsn_hint   {text},
                first_seen   {timestamp},
                last_ship    {timestamp},
                last_refresh {timestamp},
                watermark    {integer} NOT NULL DEFAULT 0
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS ctrlz_hub_operations (
                source_id      {text} NOT NULL,
                op_id          {text} NOT NULL,
                label          {text},
                source         {text},
                actor          {text},
                actor_user     {text},
                actor_host     {text},
                actor_app      {text},
                ticket         {text},
                channel        {text},
                started_at     {timestamp},
                row_count      {integer} NOT NULL DEFAULT 0,
                capped         {boolean} NOT NULL DEFAULT 0,
                undo_of        {text},
                undone_at      {timestamp},
                risk           {integer},
                policy_outcome {text},
                tables         {text},
                shipped_at     {timestamp},
                PRIMARY KEY (source_id, op_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS ctrlz_hub_operation_tables (
                source_id  {text} NOT NULL,
                op_id      {text} NOT NULL,
                table_name {text} NOT NULL,
                inserts    {integer} NOT NULL DEFAULT 0,
                updates    {integer} NOT NULL DEFAULT 0,
                deletes    {integer} NOT NULL DEFAULT 0,
                PRIMARY KEY (source_id, op_id, table_name)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS ctrlz_hub_changes (
                source_id  {text} NOT NULL,
                seq        {integer} NOT NULL,
                op_id      {text} NOT NULL,
                table_name {text} NOT NULL,
                action     {text} NOT NULL,
                identity   {text},
                before_image {text},
                after_image  {text},
                PRIMARY KEY (source_id, seq)
            )
            """,
            "CREATE INDEX IF NOT EXISTS ctrlz_hub_ops_started "
            "ON ctrlz_hub_operations (started_at)",
        ):
            self._exec(statement)

    # -- shipping ----------------------------------------------------------

    def register(self, source_id: str, name: str, engine: str, dsn_hint: str) -> int:
        """Record the source and return its watermark."""
        row = self._one(
            "SELECT watermark FROM ctrlz_hub_sources WHERE source_id = ?", (source_id,)
        )
        if row is None:
            self._exec(
                "INSERT INTO ctrlz_hub_sources "
                "(source_id, name, engine, dsn_hint, first_seen, watermark) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (source_id, name, engine, dsn_hint, _now()),
            )
            return 0
        self._exec(
            "UPDATE ctrlz_hub_sources SET name = ?, engine = ?, dsn_hint = ? "
            "WHERE source_id = ?",
            (name, engine, dsn_hint, source_id),
        )
        return int(row["watermark"] or 0)

    def watermark(self, source_id: str) -> int:
        row = self._one(
            "SELECT watermark FROM ctrlz_hub_sources WHERE source_id = ?", (source_id,)
        )
        return int(row["watermark"] or 0) if row else 0

    def advance(self, source_id: str, watermark: int) -> None:
        self._exec(
            "UPDATE ctrlz_hub_sources SET watermark = ?, last_ship = ? "
            "WHERE source_id = ?",
            (watermark, _now(), source_id),
        )

    def put_operation(self, source_id: str, operation, tables: list[str]) -> None:
        """Upsert an operation. Re-shipping must update, not duplicate."""
        columns = (
            "source_id, op_id, label, source, actor, actor_user, actor_host, "
            "actor_app, ticket, channel, started_at, row_count, capped, undo_of, "
            "undone_at, risk, policy_outcome, tables, shipped_at"
        )
        values = (
            source_id,
            operation.op_id,
            operation.label,
            operation.source,
            operation.actor,
            operation.actor_user,
            operation.actor_host,
            operation.actor_app,
            operation.ticket,
            operation.channel,
            _stamp(operation.started_at),
            operation.row_count,
            1 if operation.capped else 0,
            operation.undo_of,
            _stamp(operation.undone_at),
            operation.risk,
            operation.policy_outcome,
            json.dumps(sorted(tables)),
            _now(),
        )
        placeholders = ", ".join("?" for _ in values)
        updates = ", ".join(
            f"{name} = EXCLUDED.{name}"
            for name in columns.split(", ")
            if name not in ("source_id", "op_id")
        )
        self._exec(
            f"INSERT INTO ctrlz_hub_operations ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (source_id, op_id) DO UPDATE SET {updates}",
            values,
        )

    def put_operation_tables(self, source_id: str, op_id: str, counts: dict) -> None:
        for table, actions in sorted(counts.items()):
            values = (
                source_id, op_id, table,
                actions.get("I", 0), actions.get("U", 0), actions.get("D", 0),
            )
            self._exec(
                "INSERT INTO ctrlz_hub_operation_tables "
                "(source_id, op_id, table_name, inserts, updates, deletes) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_id, op_id, table_name) DO UPDATE SET "
                "inserts = EXCLUDED.inserts, updates = EXCLUDED.updates, "
                "deletes = EXCLUDED.deletes",
                values,
            )

    def put_change(self, source_id: str, change, include_values: bool) -> None:
        values = (
            source_id,
            change.seq,
            change.op_id,
            change.qualified_name,
            change.action,
            json.dumps(change.identity, default=str) if include_values else None,
            json.dumps(change.before, default=str) if include_values else None,
            json.dumps(change.after, default=str) if include_values else None,
        )
        self._exec(
            "INSERT INTO ctrlz_hub_changes "
            "(source_id, seq, op_id, table_name, action, identity, before_image, "
            " after_image) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source_id, seq) DO NOTHING",
            values,
        )

    def refresh_marker(self, source_id: str):
        """How far the undo-refresh pass has got for this source."""
        row = self._one(
            "SELECT last_refresh FROM ctrlz_hub_sources WHERE source_id = ?",
            (source_id,),
        )
        return _parse(row["last_refresh"]) if row else None

    def advance_refresh(self, source_id: str, when) -> None:
        self._exec(
            "UPDATE ctrlz_hub_sources SET last_refresh = ? WHERE source_id = ?",
            (_stamp(when), source_id),
        )

    def mark_undone(self, source_id: str, op_id: str, undone_at) -> None:
        self._exec(
            "UPDATE ctrlz_hub_operations SET undone_at = ? "
            " WHERE source_id = ? AND op_id = ?",
            (_stamp(undone_at), source_id, op_id),
        )

    # -- reading -----------------------------------------------------------

    def sources(self) -> list[dict]:
        return self._all("SELECT * FROM ctrlz_hub_sources ORDER BY name")

    def operations(
        self,
        limit: int = 50,
        source: Optional[str] = None,
        actor: Optional[str] = None,
        table: Optional[str] = None,
        min_risk: Optional[int] = None,
    ) -> list[HubOperation]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if source:
            clauses.append("(s.name = ? OR o.source_id = ?)")
            params += [source, source]
        if actor:
            clauses.append("(o.actor_user = ? OR o.actor = ?)")
            params += [actor, actor]
        if table:
            clauses.append(
                "EXISTS (SELECT 1 FROM ctrlz_hub_operation_tables t "
                "WHERE t.source_id = o.source_id AND t.op_id = o.op_id "
                "AND t.table_name LIKE ?)"
            )
            params.append(f"%{table}%")
        if min_risk is not None:
            clauses.append("o.risk >= ?")
            params.append(min_risk)
        params.append(limit)

        rows = self._all(
            f"SELECT o.*, s.name AS source_name FROM ctrlz_hub_operations o "
            f"  LEFT JOIN ctrlz_hub_sources s ON s.source_id = o.source_id "
            f" WHERE {' AND '.join(clauses)} "
            f" ORDER BY o.started_at DESC LIMIT ?",
            tuple(params),
        )
        return [
            HubOperation(
                source_id=r["source_id"],
                source_name=r.get("source_name") or r["source_id"][:8],
                op_id=r["op_id"],
                label=r["label"],
                actor=r.get("actor_user") or r.get("actor") or "",
                started_at=_parse(r["started_at"]),
                row_count=r["row_count"] or 0,
                tables=json.loads(r["tables"]) if r.get("tables") else [],
                risk=r.get("risk"),
                policy_outcome=r.get("policy_outcome"),
                ticket=r.get("ticket"),
                channel=r.get("channel"),
                undone_at=_parse(r.get("undone_at")),
                capped=bool(r.get("capped")),
            )
            for r in rows
        ]

    def purge(self, older_than_seconds: Optional[int] = None) -> int:
        """Trim shipped history. The source database is untouched."""
        if older_than_seconds is None:
            deleted = len(self._all("SELECT op_id FROM ctrlz_hub_operations"))
            for table in (
                "ctrlz_hub_changes", "ctrlz_hub_operation_tables", "ctrlz_hub_operations"
            ):
                self._exec(f"DELETE FROM {table}")
            return deleted

        cutoff = _stamp(
            datetime.now(timezone.utc).timestamp() - older_than_seconds, epoch=True
        )
        stale = self._all(
            "SELECT source_id, op_id FROM ctrlz_hub_operations WHERE started_at < ?",
            (cutoff,),
        )
        for row in stale:
            for table in ("ctrlz_hub_changes", "ctrlz_hub_operation_tables"):
                self._exec(
                    f"DELETE FROM {table} WHERE source_id = ? AND op_id = ?",
                    (row["source_id"], row["op_id"]),
                )
            self._exec(
                "DELETE FROM ctrlz_hub_operations WHERE source_id = ? AND op_id = ?",
                (row["source_id"], row["op_id"]),
            )
        return len(stale)


# -- the shipper -----------------------------------------------------------


def ship(
    toolkit,
    hub: Hub,
    name: Optional[str] = None,
    include_values: bool = False,
    batch: int = DEFAULT_BATCH,
    max_batches: int = 0,
) -> ShipResult:
    """Copy this database's history into the hub, catching up fully.

    Idempotent and resumable: progress is a watermark over the change log's
    monotonic sequence, advanced only after a batch is stored, and every write
    is an upsert. An interrupted run resumes from the last committed
    watermark rather than repeating or skipping.

    Batches are looped until the source is drained, because the alternative is
    a shipper that silently falls further behind the more it is used -- a
    scheduled run that moves at most one batch never catches up with a
    database producing more than a batch between runs. ``max_batches`` caps
    the work for a caller that wants a bounded run; 0 means drain.
    """
    total = None
    for round_number in range(1, (max_batches or 1_000_000) + 1):
        moved = _ship_once(toolkit, hub, name, include_values, batch)
        if total is None:
            total = moved
        else:
            total.operations += moved.operations
            total.changes += moved.changes
            total.refreshed += moved.refreshed
            total.watermark = moved.watermark
            total.batches = round_number
        if not moved.moved_anything:
            break
    return total


def _ship_once(
    toolkit,
    hub: Hub,
    name: Optional[str],
    include_values: bool,
    batch: int,
) -> ShipResult:
    """One batch. Committed on its own so an interruption costs at most one."""
    engine = toolkit.engine
    source_id = engine.source_id()
    source_name = name or _default_name(engine)
    watermark = hub.register(
        source_id, source_name, engine.name, _dsn_hint(engine)
    )

    result = ShipResult(
        source_id=source_id,
        source_name=source_name,
        watermark=watermark,
        included_values=include_values,
    )

    # 1. New changes since the watermark, and the operations they belong to.
    #    One indexed range scan. The first version of this walked every
    #    operation in the history on every run, which measured 0.1s at a
    #    thousand operations and 12s at two thousand -- a shipper whose cost
    #    grows with everything that ever happened cannot be run on a schedule,
    #    which is the only way anybody would run it.
    new_changes = engine.changes_since(watermark, batch)
    touched: dict[str, dict] = {}
    highest = watermark
    for change in new_changes:
        highest = max(highest, change.seq)
        counts = touched.setdefault(change.op_id, {})
        counts.setdefault(change.qualified_name, {})
        counts[change.qualified_name][change.action] = (
            counts[change.qualified_name].get(change.action, 0) + 1
        )

    # Read the source before opening the hub transaction, so a slow source
    # never holds a write transaction open on a store other databases share.
    operations = []
    for op_id, counts in touched.items():
        try:
            operations.append((engine.operation(op_id), counts))
        except Exception:  # noqa: BLE001 - purged at the source between reads
            continue

    if new_changes or operations:
        with hub.transaction():
            for change in new_changes:
                hub.put_change(source_id, change, include_values)
                result.changes += 1
            for operation, counts in operations:
                hub.put_operation(source_id, operation, sorted(counts))
                hub.put_operation_tables(source_id, operation.op_id, counts)
                result.operations += 1
            if highest > watermark:
                hub.advance(source_id, highest)
                result.watermark = highest

    # 2. Operations undone since we last looked. The watermark cannot see this:
    #    undoing sets a column on a row that was already copied, so it needs a
    #    second question with its own marker.
    since = hub.refresh_marker(source_id)
    undone = engine.operations_undone_since(since, limit=batch)
    if undone:
        latest = since
        with hub.transaction():
            for op_id, undone_at in undone:
                hub.mark_undone(source_id, op_id, undone_at)
                result.refreshed += 1
                if undone_at is not None and (latest is None or undone_at > latest):
                    latest = undone_at
            if latest is not None and latest != since:
                hub.advance_refresh(source_id, latest)

    return result


# -- helpers ---------------------------------------------------------------


class _SqliteCursor:
    """Context-manager wrapper so both backends read the same way."""

    def __init__(self, conn):
        self._conn = conn
        self._cursor = None

    def __enter__(self):
        self._cursor = self._conn.cursor()
        return self._cursor

    def __exit__(self, *exc):
        if self._cursor is not None:
            self._cursor.close()
        return False


def _sqlite_path(dsn: str) -> str:
    if not dsn.startswith("sqlite:"):
        return dsn
    rest = dsn.split(":", 1)[1]
    rest = rest[2:] if rest.startswith("//") else rest
    path = rest[1:] if rest.startswith("/") else rest
    return path or ":memory:"


def _default_name(engine) -> str:
    return getattr(engine, "database", None) or getattr(engine, "path", None) or engine.name


def _dsn_hint(engine) -> str:
    """Where to go to undo something. Never a password.

    The hub tells you which database an operation happened in; it does not
    hold the means to connect to it.
    """
    dsn = getattr(engine, "dsn", None) or getattr(engine, "path", "")
    if not dsn:
        return ""
    parsed = urlparse(str(dsn))
    if not parsed.scheme:
        return str(dsn)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp(value, epoch: bool = False) -> Optional[str]:
    if value is None:
        return None
    if epoch:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.isoformat()
    return str(value)


def _parse(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
