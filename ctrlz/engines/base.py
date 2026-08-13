"""The contract every storage engine implements.

The split is deliberate: an engine is responsible for *capture* and for
*applying* an inverse using the database's own type system. Everything above
that -- what counts as a conflict, what the user is promised, how it renders --
lives in engine-independent code.
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any, Iterable, Optional

from ..model import Change, ExecutionResult, Operation, Undoability, UndoResult


#: Marks a blocker that `--allow-conflicts` is allowed to override.
#:
#: Schema drift is the one blocker a caller can legitimately decide about:
#: they may know the column was added *after* the write, in which case there
#: was never a value to restore. Every other blocker -- a capped operation, an
#: already-undone one, a MySQL cascade -- means the capture is missing rows we
#: cannot reconstruct, and no flag should talk us past that.
SCHEMA_DRIFT_MARKER = "column(s) the capture never saw"


def overridable(blocker: str) -> bool:
    return SCHEMA_DRIFT_MARKER in blocker


class Engine(abc.ABC):
    """A database ctrlz can capture from and reverse changes in."""

    name: str = "base"
    #: Human-readable note about anything this engine cannot promise.
    caveats: tuple[str, ...] = ()

    # -- lifecycle ---------------------------------------------------------

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def is_initialized(self) -> bool: ...

    @abc.abstractmethod
    def initialize(self) -> None:
        """Create the metadata store. Must be safe to run repeatedly."""

    @abc.abstractmethod
    def uninstall(self) -> None:
        """Remove every trigger and the metadata store."""

    # -- tracking ----------------------------------------------------------

    @abc.abstractmethod
    def track(self, table: str, identity: Optional[list[str]] = None) -> list[str]:
        """Attach capture triggers. Returns the identity columns used."""

    @abc.abstractmethod
    def untrack(self, table: str) -> None: ...

    @abc.abstractmethod
    def tracked(self) -> list[tuple[str, list[str]]]: ...

    @abc.abstractmethod
    def tables(self) -> list[str]:
        """Every user table in the database, qualified."""

    # -- executing ---------------------------------------------------------

    @abc.abstractmethod
    def execute(
        self,
        sql: str,
        label: Optional[str] = None,
        dry_run: bool = False,
        decide: Optional[Any] = None,
    ) -> ExecutionResult:
        """Run a statement inside a labelled transaction.

        ``decide`` is called with the real affected-row count while the
        transaction is still open; returning False rolls it back. That is what
        makes the preview honest -- the number shown is the number the database
        actually touched, not an estimate.
        """

    # -- history -----------------------------------------------------------

    @abc.abstractmethod
    def operations(
        self, limit: int = 20, include_undone: bool = True, include_undos: bool = True
    ) -> list[Operation]: ...

    @abc.abstractmethod
    def operation(self, op_id: str) -> Operation: ...

    @abc.abstractmethod
    def changes(self, op_id: str) -> list[Change]: ...

    @abc.abstractmethod
    def changes_since(self, seq: int, limit: int = 1000) -> list[Change]:
        """Captured changes with a sequence above ``seq``, oldest first.

        A single indexed range scan. Undo never needs this -- it works one
        operation at a time -- but anything that follows the log incrementally
        does, and reconstructing it by walking every operation is quadratic in
        the size of the history.
        """

    @abc.abstractmethod
    def operations_undone_since(
        self, when: Optional[datetime] = None, limit: int = 1000
    ) -> list[tuple[str, datetime]]:
        """Operations undone after ``when``, oldest undo first.

        Undoing sets a column on a row that already exists, so a follower
        watching the change log's sequence cannot see it. This is the second
        thing a follower has to ask.
        """

    @abc.abstractmethod
    def assess(self, op_id: str) -> Undoability:
        """Compare every captured change against the live row."""

    @abc.abstractmethod
    def undo(
        self, op_id: str, allow_conflicts: bool = False, label: Optional[str] = None
    ) -> UndoResult: ...

    @abc.abstractmethod
    def purge(self, older_than_seconds: Optional[int] = None) -> int: ...

    # -- settings ----------------------------------------------------------

    @abc.abstractmethod
    def get_setting(self, key: str) -> Optional[str]:
        """Read a value from the metadata store, or None."""

    @abc.abstractmethod
    def set_setting(self, key: str, value: str) -> None:
        """Write a value to the metadata store."""

    def source_id(self) -> str:
        """A stable identity for this database, minted on first use.

        The control plane needs to tell two databases apart, and a DSN will
        not do: the same database is reached by different host names from
        different machines, and a copy restored from a backup would otherwise
        look like the original.
        """
        import uuid as _uuid

        existing = self.get_setting("source_id")
        if existing:
            return existing
        minted = _uuid.uuid4().hex
        self.set_setting("source_id", minted)
        return minted

    def describe_target(self) -> str:
        """Where this engine is connected, with any password removed.

        Printed by `init` and `doctor` so that "it worked" is never the only
        thing a user has to go on. If ctrlz opened something other than the
        database they meant, seeing the target says so immediately -- and no
        amount of validation upstream can be as convincing as that.

        Credential stripping lives here, in one place, because a second
        implementation of it is how a password reaches a log file.
        """
        from urllib.parse import urlparse

        target = getattr(self, "dsn", None) or getattr(self, "path", "")
        if not target:
            return ""
        parsed = urlparse(str(target))
        if not parsed.scheme:
            return str(target)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path}"

    # -- helpers -----------------------------------------------------------

    def schema_drift_blockers(
        self, changes: Iterable[Change], columns_of
    ) -> list[str]:
        """Refuse an undo whose captured images no longer cover the table.

        Engines that name every column in the trigger body -- SQLite and MySQL
        -- freeze that list when ``track`` runs. ``ALTER TABLE ... ADD COLUMN``
        does not rebuild the trigger, so from that moment the images are
        missing a column, and an undo restores everything *except* it.

        That is the worst failure this project can have, and it was reported by
        an external reviewer rather than caught here: preview said `clean`,
        undo said it had worked, and one column silently kept its wrong value.
        A partial restore presented as a complete one is worse than no undo at
        all, because the user stops looking.

        So the operation is blocked. It is deliberately blocked rather than
        best-effort repaired: we cannot tell whether the column was added
        before the write (the capture is genuinely incomplete) or after it (the
        row never had a value to restore), and guessing between "your data is
        fine" and "your data is not" is exactly the guess this tool exists to
        avoid making. `--allow-conflicts` proceeds for a caller who knows which
        case they are in.

        PostgreSQL never reaches here: it captures with ``to_jsonb(OLD)``,
        which serialises whatever columns exist when the trigger fires.
        """
        missing: dict[str, set] = {}
        for change in changes:
            image = change.before or change.after or {}
            if not image:
                continue
            try:
                live = set(columns_of(change.table_name))
            except Exception:  # noqa: BLE001 - the table may be gone; other checks cover that
                continue
            absent = live - set(image)
            if absent:
                missing.setdefault(change.table_name, set()).update(absent)

        blockers = []
        for table, columns in sorted(missing.items()):
            names = ", ".join(sorted(columns))
            blockers.append(
                f"{table} has column(s) the capture never saw ({names}) -- the "
                f"table was altered after it was tracked, so undoing would "
                f"restore every other column and silently leave these as they "
                f"are. Run `ctrlz track {table}` to rebuild the trigger for "
                f"future changes; use --allow-conflicts to reverse this one "
                f"anyway, knowing those columns keep their current values."
            )
        return blockers

    def resolve_op_id(self, ref: str) -> str:
        """Turn a user-supplied reference into a real operation id.

        Accepts ``last``, a unique id prefix, or a full id.
        """
        from ..errors import UnknownOperation

        ref = (ref or "").strip()
        if ref.lower() in ("last", "latest", "-1"):
            ops = [
                op
                for op in self.operations(limit=50, include_undone=False)
                if not op.is_undo
            ]
            if not ops:
                raise UnknownOperation("no undoable operation recorded yet")
            return ops[0].op_id
        if ref.lower() in ("last-undo", "undo"):
            ops = [op for op in self.operations(limit=50) if op.is_undo and not op.already_undone]
            if not ops:
                raise UnknownOperation("no undo to redo")
            return ops[0].op_id
        matches = [op.op_id for op in self.operations(limit=500) if op.op_id.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise UnknownOperation(f"{ref!r} is ambiguous ({len(matches)} operations match)")
        raise UnknownOperation(f"no operation matching {ref!r}")


def split_qualified(table: str, default_schema: str) -> tuple[str, str]:
    """Split ``schema.table`` into parts, applying a default schema."""
    table = table.strip().strip('"')
    if "." in table:
        schema, _, name = table.partition(".")
        return schema.strip('"'), name.strip('"')
    return default_schema, table


def rows_to_identity(row: dict[str, Any], columns: Iterable[str]) -> dict[str, Any]:
    return {c: row.get(c) for c in columns}
