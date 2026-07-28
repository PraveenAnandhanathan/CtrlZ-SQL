"""Value types shared by every engine.

Everything in here is engine-independent on purpose: the capture layer differs
wildly between databases, but once a change has been reduced to a before-image
and an after-image the rest of the toolkit can treat all engines alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

INSERT = "I"
UPDATE = "U"
DELETE = "D"

ACTION_NAMES = {INSERT: "INSERT", UPDATE: "UPDATE", DELETE: "DELETE"}

# Per-row drift verdicts.
CLEAN = "clean"          # the row still looks exactly like we left it
DRIFTED = "drifted"      # somebody changed it after us; undo would clobber them
MISSING = "missing"      # the row we would revert or delete is already gone
OCCUPIED = "occupied"    # the identity we want to re-insert into is taken


@dataclass(frozen=True)
class Change:
    """One row-level change, as captured by the database itself."""

    seq: int
    op_id: str
    table_schema: str
    table_name: str
    action: str
    identity: dict[str, Any]
    before: Optional[dict[str, Any]]
    after: Optional[dict[str, Any]]
    captured_at: Optional[datetime] = None

    @property
    def qualified_name(self) -> str:
        return f"{self.table_schema}.{self.table_name}" if self.table_schema else self.table_name


@dataclass
class Operation:
    """A group of changes that will be undone together.

    On Postgres one operation == one database transaction, which is the only
    grouping that is actually meaningful: it is what the database committed
    atomically, so it is what we can reverse atomically.
    """

    op_id: str
    label: Optional[str]
    source: str
    actor: str
    started_at: Optional[datetime]
    row_count: int
    capped: bool = False
    undo_of: Optional[str] = None
    undone_at: Optional[datetime] = None
    undone_by: Optional[str] = None
    tables: list[str] = field(default_factory=list)

    @property
    def is_undo(self) -> bool:
        return self.undo_of is not None

    @property
    def already_undone(self) -> bool:
        return self.undone_at is not None


@dataclass(frozen=True)
class RowVerdict:
    """What we found when we compared a captured change to the live row."""

    change: Change
    status: str
    current: Optional[dict[str, Any]] = None

    @property
    def blocks_undo(self) -> bool:
        # A missing row is not a blocker: undoing an INSERT whose row is
        # already gone, or reverting an UPDATE on a deleted row, is a no-op we
        # can safely skip. Drift and occupied identities are real conflicts.
        return self.status in (DRIFTED, OCCUPIED)


@dataclass
class Undoability:
    """The trust contract, computed before the user is offered an Undo button.

    The whole point of the toolkit is that this is never a guess. If we cannot
    promise a clean reversal we say so up front rather than at apply time.
    """

    operation: Operation
    verdicts: list[RowVerdict]
    blockers: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out = {CLEAN: 0, DRIFTED: 0, MISSING: 0, OCCUPIED: 0}
        for v in self.verdicts:
            out[v.status] = out.get(v.status, 0) + 1
        return out

    @property
    def conflicts(self) -> list[RowVerdict]:
        return [v for v in self.verdicts if v.blocks_undo]

    @property
    def status(self) -> str:
        if self.blockers:
            return "blocked"
        if self.conflicts:
            return "conflicts"
        return "undoable"


@dataclass
class UndoResult:
    """What actually happened when the inverse was applied."""

    op_id: str
    undo_op_id: Optional[str]
    applied: int
    skipped: int
    conflicts_overridden: int
    tables: list[str] = field(default_factory=list)
    sequences_fixed: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    """Result of running a statement through the toolkit's guardrails."""

    op_id: Optional[str]
    rowcount: int
    committed: bool
    warnings: list[str] = field(default_factory=list)
