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

    # -- helpers -----------------------------------------------------------

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
