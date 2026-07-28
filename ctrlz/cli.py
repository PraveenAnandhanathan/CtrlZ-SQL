"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from . import render
from .api import DEFAULT_CONFIRM_OVER, Toolkit, connect
from .errors import CtrlzError, NoIdentity, PreflightBlocked
from .model import ACTION_NAMES
from .preflight import inspect as preflight_inspect

PROG = "ctrlz"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Undo for SQL. Captures row images as changes happen, "
        "then reverses them with conflict detection.",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("CTRLZ_DSN"),
        help="database URL (postgresql://... or sqlite:///file.db); "
        "defaults to $CTRLZ_DSN",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="install the capture machinery")

    p = sub.add_parser("uninstall", help="remove all triggers and history")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    p = sub.add_parser("track", help="start capturing changes to a table")
    p.add_argument("table", nargs="*", help="table name, optionally schema-qualified")
    p.add_argument("--all", action="store_true", help="track every user table")
    p.add_argument("--identity", help="comma-separated columns identifying a row")

    p = sub.add_parser("untrack", help="stop capturing changes to a table")
    p.add_argument("table", nargs="+")

    sub.add_parser("tracked", help="list tracked tables")

    p = sub.add_parser("run", help="execute a statement behind the guardrails")
    p.add_argument("sql")
    p.add_argument("--label", help="what this change is for; shown in the history")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="execute, report the real affected-row count, then roll back",
    )
    p.add_argument(
        "--force", action="store_true", help="run even if a guardrail objects"
    )
    p.add_argument(
        "--confirm-over",
        type=int,
        default=DEFAULT_CONFIRM_OVER,
        help=f"ask before committing more than N rows (default {DEFAULT_CONFIRM_OVER}, "
        f"-1 to never ask)",
    )
    p.add_argument("--yes", action="store_true", help="answer yes to the row-count prompt")

    p = sub.add_parser("log", help="show recent operations")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--all", action="store_true", help="include undone operations and undos")

    p = sub.add_parser("preview", help="show what an undo would do")
    p.add_argument("operation", nargs="?", default="last", help="operation id or 'last'")
    p.add_argument("--rows", type=int, default=20, help="how many rows to show")

    p = sub.add_parser("undo", help="reverse an operation")
    p.add_argument("operation", nargs="?", default="last")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument(
        "--allow-conflicts",
        action="store_true",
        help="overwrite rows that changed since capture",
    )

    p = sub.add_parser("redo", help="undo the last undo")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("check", help="run the guardrails over a statement without executing")
    p.add_argument("sql")

    p = sub.add_parser("purge", help="delete old history")
    p.add_argument("--older-than", help="e.g. 30m, 24h, 7d; omit to delete everything")
    p.add_argument("--yes", action="store_true")

    sub.add_parser("doctor", help="report what is and is not protected")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        with connect(args.dsn) as toolkit:
            return dispatch(toolkit, args)
    except PreflightBlocked as exc:
        print(render.c("Blocked by a guardrail:", "red"), file=sys.stderr)
        for line in str(exc).split("; "):
            print(f"  - {line}", file=sys.stderr)
        print(
            render.c("  Re-run with --force if you really mean it.", "dim"),
            file=sys.stderr,
        )
        return 2
    except CtrlzError as exc:
        print(render.c(f"{PROG}: {exc}", "red"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def dispatch(toolkit: Toolkit, args) -> int:
    handler = globals()[f"cmd_{args.command.replace('-', '_')}"]
    return handler(toolkit, args)


# -- commands ---------------------------------------------------------------


def cmd_init(toolkit: Toolkit, args) -> int:
    toolkit.init()
    _emit(args, {"initialized": True}, lambda: print(
        render.c("ctrlz installed.", "green")
        + "\nNext: ctrlz track --all   (or ctrlz track schema.table)"
    ))
    return 0


def cmd_uninstall(toolkit: Toolkit, args) -> int:
    if not args.yes and not _confirm("Remove all ctrlz triggers and undo history?"):
        print("Cancelled.")
        return 1
    toolkit.uninstall()
    print(render.c("ctrlz removed.", "green"))
    return 0


def cmd_track(toolkit: Toolkit, args) -> int:
    if args.all:
        results = toolkit.track_all()
        ok = {t: v for t, v in results.items() if not isinstance(v, Exception)}
        bad = {t: v for t, v in results.items() if isinstance(v, Exception)}
        if args.json:
            print(json.dumps({
                "tracked": {t: v for t, v in ok.items()},
                "skipped": {t: str(v) for t, v in bad.items()},
            }, indent=2))
            return 0
        for tbl, ident in sorted(ok.items()):
            print(f"{render.c('tracked', 'green')} {tbl} (identity: {', '.join(ident)})")
        for tbl, err in sorted(bad.items()):
            print(f"{render.c('skipped', 'yellow')} {tbl}: {err}")
        return 0

    if not args.table:
        print(f"{PROG}: give a table name or --all", file=sys.stderr)
        return 1

    identity = args.identity.split(",") if args.identity else None
    for table in args.table:
        try:
            ident = toolkit.track(table, [c.strip() for c in identity] if identity else None)
        except NoIdentity as exc:
            print(f"{render.c('skipped', 'yellow')} {table}: {exc}")
            continue
        print(f"{render.c('tracked', 'green')} {table} (identity: {', '.join(ident)})")
    return 0


def cmd_untrack(toolkit: Toolkit, args) -> int:
    for table in args.table:
        toolkit.untrack(table)
        print(f"{render.c('untracked', 'dim')} {table}")
    return 0


def cmd_tracked(toolkit: Toolkit, args) -> int:
    rows = toolkit.tracked()
    if args.json:
        print(json.dumps([{"table": t, "identity": i} for t, i in rows], indent=2))
        return 0
    if not rows:
        print(render.c("Nothing is tracked. Run: ctrlz track --all", "dim"))
        return 0
    print(render.table([[t, ", ".join(i)] for t, i in rows], ["TABLE", "IDENTITY"]))
    return 0


def cmd_run(toolkit: Toolkit, args) -> int:
    def confirm(rowcount: int, sql: str) -> bool:
        if args.yes:
            return True
        print(
            render.c(
                f"This statement affects {rowcount} row(s), over the "
                f"--confirm-over threshold of {args.confirm_over}.",
                "yellow",
            )
        )
        return _confirm("Commit it?")

    result = toolkit.run(
        args.sql,
        label=args.label,
        dry_run=args.dry_run,
        force=args.force,
        confirm_over=args.confirm_over,
        confirm=confirm,
    )

    if args.json:
        print(json.dumps({
            "op_id": result.op_id,
            "rowcount": result.rowcount,
            "committed": result.committed,
            "warnings": result.warnings,
        }, indent=2))
        return 0

    for warning in result.warnings:
        print(render.c(f"warning: {warning}", "yellow"), file=sys.stderr)

    if not result.committed:
        reason = "dry run" if args.dry_run else "not confirmed"
        print(
            render.c(f"Rolled back ({reason}).", "blue")
            + f" It would have affected {result.rowcount} row(s)."
        )
        return 0

    message = f"{result.rowcount} row(s) affected."
    if result.op_id:
        message += (
            f" Undo with: {PROG} undo {render.short(result.op_id)}"
        )
    print(render.c("Committed.", "green") + " " + message)
    return 0


def cmd_log(toolkit: Toolkit, args) -> int:
    ops = toolkit.log(
        limit=args.limit,
        include_undone=args.all,
        include_undos=args.all,
    )
    if args.json:
        print(json.dumps([_op_json(op) for op in ops], indent=2, default=str))
        return 0
    print(render.format_operations(ops))
    return 0


def cmd_preview(toolkit: Toolkit, args) -> int:
    assessment = toolkit.preview(args.operation)
    if args.json:
        print(json.dumps(_assessment_json(assessment), indent=2, default=str))
        return 0
    print(render.format_preview(assessment, max_rows=args.rows))
    return 0 if assessment.status != "blocked" else 1


def cmd_undo(toolkit: Toolkit, args) -> int:
    assessment = toolkit.preview(args.operation)
    if not args.json:
        print(render.format_preview(assessment, max_rows=args.rows if hasattr(args, "rows") else 20))
        print()
    if assessment.status == "blocked":
        return 1
    if not args.yes and not _confirm("Apply this undo?"):
        print("Cancelled.")
        return 1

    result = toolkit.undo(args.operation, allow_conflicts=args.allow_conflicts)
    if args.json:
        print(json.dumps(result.__dict__, indent=2, default=str))
        return 0
    print(render.format_undo_result(result))
    return 0


def cmd_redo(toolkit: Toolkit, args) -> int:
    if not args.yes and not _confirm("Redo the last undo?"):
        print("Cancelled.")
        return 1
    result = toolkit.redo()
    if args.json:
        print(json.dumps(result.__dict__, indent=2, default=str))
        return 0
    print(render.format_undo_result(result))
    return 0


def cmd_check(toolkit: Toolkit, args) -> int:
    tracked = {name for name, _ in toolkit.tracked()}
    checks = preflight_inspect(args.sql, tracked=tracked)
    if args.json:
        print(json.dumps({
            "keyword": checks.keyword,
            "blockers": checks.blockers,
            "warnings": checks.warnings,
        }, indent=2))
        return 2 if checks.blocked else 0
    for blocker in checks.blockers:
        print(render.c(f"blocked: {blocker}", "red"))
    for warning in checks.warnings:
        print(render.c(f"warning: {warning}", "yellow"))
    if not checks.blockers and not checks.warnings:
        print(render.c("Looks fine.", "green"))
    return 2 if checks.blocked else 0


def cmd_purge(toolkit: Toolkit, args) -> int:
    what = f"older than {args.older_than}" if args.older_than else "ALL history"
    if not args.yes and not _confirm(f"Delete {what}?"):
        print("Cancelled.")
        return 1
    deleted = toolkit.purge(args.older_than)
    print(render.c(f"Purged {deleted} operation(s).", "green"))
    return 0


def cmd_doctor(toolkit: Toolkit, args) -> int:
    info = toolkit.doctor()
    if args.json:
        print(json.dumps(info, indent=2, default=str))
        return 0

    print(f"engine:      {info['engine']}")
    print(f"initialized: {'yes' if info['initialized'] else 'no'}")
    if not info["initialized"]:
        print(render.c("\nRun: ctrlz init", "yellow"))
        return 1

    tracked = info.get("tracked") or []
    untracked = info.get("untracked") or []
    print(f"operations:  {info.get('operations', 0)} in history")
    print(f"\n{render.c('Protected', 'green')} ({len(tracked)} table(s))")
    for name, ident in tracked:
        print(f"  {name}  identity: {', '.join(ident)}")
    if untracked:
        print(f"\n{render.c('NOT protected', 'yellow')} ({len(untracked)} table(s))")
        for name in untracked:
            print(f"  {name}")
        print(render.c("  Changes to these tables cannot be undone.", "dim"))
    if info["caveats"]:
        print(f"\n{render.c('Known limits', 'dim')}")
        for caveat in info["caveats"]:
            print(f"  - {caveat}")
    return 0


# -- helpers ----------------------------------------------------------------


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _emit(args, payload: dict, human) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        human()


def _op_json(op) -> dict:
    return {
        "op_id": op.op_id,
        "label": op.label,
        "source": op.source,
        "actor": op.actor,
        "started_at": op.started_at,
        "row_count": op.row_count,
        "capped": op.capped,
        "undo_of": op.undo_of,
        "undone_at": op.undone_at,
        "tables": op.tables,
    }


def _assessment_json(assessment) -> dict:
    return {
        "operation": _op_json(assessment.operation),
        "status": assessment.status,
        "blockers": assessment.blockers,
        "counts": assessment.counts,
        "rows": [
            {
                "seq": v.change.seq,
                "table": v.change.qualified_name,
                "action": ACTION_NAMES[v.change.action],
                "inverse": {"I": "DELETE", "D": "INSERT", "U": "UPDATE"}[v.change.action],
                "identity": v.change.identity,
                "before": v.change.before,
                "after": v.change.after,
                "current": v.current,
                "status": v.status,
            }
            for v in assessment.verdicts
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
