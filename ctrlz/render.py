"""Terminal output.

The preview is the most important screen in the toolkit: it is where the user
decides whether to trust the undo. It shows what changed, what drifted, and
what will be skipped -- before anything is applied.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .model import (
    ACTION_NAMES,
    CLEAN,
    DELETE,
    DRIFTED,
    INSERT,
    MISSING,
    OCCUPIED,
    UPDATE,
    Operation,
    RowVerdict,
    Undoability,
)

_COLORS = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
}


def use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CTRLZ_FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def c(text: str, color: str) -> str:
    if not use_color():
        return text
    return f"{_COLORS.get(color, '')}{text}{_COLORS['reset']}"


def short(op_id: str) -> str:
    return op_id[:8]


def ago(when: Optional[datetime]) -> str:
    if when is None:
        return "-"
    now = datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def truncate(value: Any, width: int = 40) -> str:
    text = "NULL" if value is None else str(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= width else text[: width - 1] + "…"


def table(rows: list[list[str]], headers: list[str]) -> str:
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["  ".join(c(h.ljust(widths[i]), "dim") for i, h in enumerate(headers))]
    for row in rows:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(out)


# -- history ---------------------------------------------------------------


def format_operations(ops: Iterable[Operation]) -> str:
    ops = list(ops)
    if not ops:
        return c("No operations recorded yet.", "dim")

    rows = []
    for op in ops:
        if op.already_undone:
            mark, colour = "undone", "dim"
        elif op.capped:
            mark, colour = "TOO BIG", "red"
        elif op.is_undo:
            mark, colour = "undo", "blue"
        else:
            mark, colour = "undoable", "green"
        rows.append(
            [
                c(short(op.op_id), "bold"),
                ago(op.started_at),
                c(mark, colour),
                str(op.row_count),
                ", ".join(op.tables) or "-",
                truncate(op.label or "(unlabelled)", 44),
            ]
        )
    return table(rows, ["ID", "WHEN", "STATE", "ROWS", "TABLES", "LABEL"])


# -- preview ---------------------------------------------------------------


def format_preview(assessment: Undoability, max_rows: int = 20) -> str:
    op = assessment.operation
    counts = assessment.counts
    lines: list[str] = []

    lines.append(
        f"{c('Operation', 'dim')} {c(short(op.op_id), 'bold')}  "
        f"{op.label or '(unlabelled)'}"
    )
    lines.append(
        c(
            f"  {op.row_count} row(s) across {', '.join(op.tables) or 'no tables'} "
            f"by {op.actor} {ago(op.started_at)} via {op.source}",
            "dim",
        )
    )
    lines.append("")

    by_action: dict[str, int] = {}
    for verdict in assessment.verdicts:
        by_action[verdict.change.action] = by_action.get(verdict.change.action, 0) + 1
    summary = ", ".join(
        f"{count} {ACTION_NAMES[action].lower()}" for action, count in sorted(by_action.items())
    )
    if summary:
        lines.append(f"Undoing reverses: {summary}")
        lines.append("")

    shown = 0
    for verdict in assessment.verdicts:
        if shown >= max_rows:
            break
        lines.extend(format_row(verdict))
        shown += 1
    if len(assessment.verdicts) > shown:
        lines.append(c(f"  … and {len(assessment.verdicts) - shown} more row(s)", "dim"))
    if assessment.verdicts:
        lines.append("")

    lines.append(format_verdict_banner(assessment, counts))
    return "\n".join(lines)


def format_row(verdict: RowVerdict) -> list[str]:
    change = verdict.change
    ident = ", ".join(f"{k}={truncate(v, 24)}" for k, v in change.identity.items())
    status_colour = {
        CLEAN: "green",
        DRIFTED: "yellow",
        MISSING: "dim",
        OCCUPIED: "red",
    }[verdict.status]

    # The inverse of the original action is what the user is about to run.
    inverse = {INSERT: "DELETE", DELETE: "INSERT", UPDATE: "UPDATE"}[change.action]
    head = (
        f"  {c(inverse.ljust(6), status_colour)} {change.qualified_name} "
        f"[{ident}] {c(verdict.status, status_colour)}"
    )
    lines = [head]

    if change.action == UPDATE:
        for column, old, new in diff_columns(change.before, change.after):
            lines.append(
                f"      {column}: {c(truncate(new), 'red')} → {c(truncate(old), 'green')}"
            )
    elif change.action == DELETE:
        preview = ", ".join(
            f"{k}={truncate(v, 20)}" for k, v in list((change.before or {}).items())[:5]
        )
        lines.append(f"      restores: {preview}")
    else:
        preview = ", ".join(
            f"{k}={truncate(v, 20)}" for k, v in list((change.after or {}).items())[:5]
        )
        lines.append(f"      removes: {preview}")

    if verdict.status == DRIFTED:
        for column, expected, actual in diff_columns(change.after, verdict.current):
            lines.append(
                c(
                    f"      ! {column} is now {truncate(actual)} "
                    f"(we wrote {truncate(expected)}) -- someone else changed this",
                    "yellow",
                )
            )
    elif verdict.status == OCCUPIED:
        lines.append(
            c("      ! that identity is taken by a newer row; will be skipped", "red")
        )
    return lines


def diff_columns(
    left: Optional[dict], right: Optional[dict]
) -> list[tuple[str, Any, Any]]:
    """Columns whose value differs between two row images."""
    left = left or {}
    right = right or {}
    out = []
    for key in left:
        if key in right and left[key] != right[key]:
            out.append((key, left[key], right[key]))
    return out


def format_verdict_banner(assessment: Undoability, counts: dict[str, int]) -> str:
    if assessment.blockers:
        body = "\n".join(f"  - {b}" for b in assessment.blockers)
        return c("CANNOT UNDO", "red") + "\n" + body

    parts = [f"{counts[CLEAN]} clean"]
    if counts[DRIFTED]:
        parts.append(c(f"{counts[DRIFTED]} drifted", "yellow"))
    if counts[MISSING]:
        parts.append(c(f"{counts[MISSING]} already gone", "dim"))
    if counts[OCCUPIED]:
        parts.append(c(f"{counts[OCCUPIED]} blocked", "red"))
    body = ", ".join(parts)

    if assessment.conflicts:
        return (
            c("UNDO WITH CONFLICTS", "yellow")
            + f"  ({body})\n"
            + c(
                "  Rows above marked drifted were changed by someone else since. "
                "Undo will overwrite them only with --allow-conflicts; occupied "
                "identities are always skipped.",
                "dim",
            )
        )
    return c("UNDOABLE", "green") + f"  ({body})"


def format_undo_result(result) -> str:
    lines = [
        c("Undone.", "green")
        + f" {result.applied} row(s) restored across {', '.join(result.tables) or 'no tables'}."
    ]
    if result.skipped:
        lines.append(c(f"  {result.skipped} row(s) skipped (already gone or blocked).", "dim"))
    if result.conflicts_overridden:
        lines.append(
            c(f"  {result.conflicts_overridden} conflicting row(s) overwritten.", "yellow")
        )
    if result.sequences_fixed:
        lines.append(c(f"  sequences resynced: {', '.join(result.sequences_fixed)}", "dim"))
    if result.undo_op_id:
        lines.append(
            c(f"  This undo is itself operation {short(result.undo_op_id)} "
              f"-- run `ctrlz redo` to reverse it.", "dim")
        )
    return "\n".join(lines)
