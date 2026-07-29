"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from . import render
from .actor import CLI, Actor
from .api import DEFAULT_CONFIRM_OVER, Toolkit, connect
from .errors import CtrlzError, NoIdentity, PreflightBlocked
from .model import ACTION_NAMES
from .policy import find_policy_file, load_policy

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
    p.add_argument("--explain", action="store_true", help="show how the verdict was reached")

    p = sub.add_parser("policy", help="inspect the rulebook")
    p.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=("show", "test", "lint", "path"),
        help="show the effective rules, test a statement, validate the file, "
        "or print where it was loaded from",
    )
    p.add_argument("sql", nargs="?", help="statement to test (for: policy test)")
    p.add_argument("--file", help="policy file to use instead of the discovered one")

    p = sub.add_parser("purge", help="delete old history")
    p.add_argument("--older-than", help="e.g. 30m, 24h, 7d; omit to delete everything")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser(
        "gateway",
        help="run a checkpoint in front of the database that every client can use",
    )
    p.add_argument(
        "--listen", default="127.0.0.1:6543",
        help="address to accept client connections on (default 127.0.0.1:6543)",
    )
    p.add_argument(
        "--upstream",
        help="the real database to proxy to; defaults to --dsn / $CTRLZ_DSN",
    )
    p.add_argument(
        "--no-attribution", action="store_true",
        help="do not record the connecting user against changes",
    )

    p = sub.add_parser(
        "ship", help="copy this database's history to a shared control plane"
    )
    p.add_argument("--to", required=True, help="hub DSN (sqlite:///... or postgresql://...)")
    p.add_argument("--name", help="what to call this database in the hub")
    p.add_argument(
        "--include-values",
        action="store_true",
        help="also ship before/after row values (off by default: shipping data "
        "off the database it came from should be a deliberate choice)",
    )
    p.add_argument("--batch", type=int, default=1000, help="rows per run")

    p = sub.add_parser("hub", help="read the shared control plane")
    p.add_argument(
        "action", nargs="?", default="log", choices=("log", "sources", "purge")
    )
    p.add_argument("--at", required=True, help="hub DSN")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("--source", help="filter by source name or id")
    p.add_argument("--actor", help="filter by who made the change")
    p.add_argument("--table", help="filter by table touched")
    p.add_argument("--min-risk", type=int, help="only operations at or above this risk")
    p.add_argument("--older-than", help="for purge: e.g. 30d")
    p.add_argument("--yes", action="store_true")

    sub.add_parser("doctor", help="report what is and is not protected")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        with connect(args.dsn, actor=Actor.resolve(channel=CLI)) as toolkit:
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
            "decision": _decision_json(result.decision) if result.decision else None,
        }, indent=2, default=str))
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


#: Exit codes, so scripts and CI can act on a verdict without parsing text.
EXIT_ALLOW, EXIT_WARN, EXIT_BLOCK = 0, 1, 2


def _exit_code(decision) -> int:
    return {"allow": EXIT_ALLOW, "warn": EXIT_WARN, "block": EXIT_BLOCK}[decision.outcome]


def cmd_check(toolkit: Toolkit, args) -> int:
    decision = toolkit.check(args.sql)
    if args.json:
        print(json.dumps(_decision_json(decision), indent=2))
        return _exit_code(decision)

    print(render.format_decision(decision))
    if getattr(args, "explain", False):
        print()
        print(render.c(decision.explain(), "dim"))
    return _exit_code(decision)


def cmd_policy(toolkit: Toolkit, args) -> int:
    handlers = {
        "show": _policy_show,
        "test": _policy_test,
        "lint": _policy_lint,
        "path": _policy_path,
    }
    return handlers[args.action](toolkit, args)


def _policy_for(toolkit: Toolkit, args):
    return load_policy(args.file) if getattr(args, "file", None) else toolkit.policy


def _policy_show(toolkit: Toolkit, args) -> int:
    policy = _policy_for(toolkit, args)
    if args.json:
        print(json.dumps({
            "source": policy.source,
            "risk_threshold": policy.risk_threshold,
            "block_on_risk": policy.block_on_risk,
            "rules": [
                {"name": r.name, "action": r.action, "risk": r.risk, "message": r.message}
                for r in policy.rules
            ],
        }, indent=2))
        return 0

    print(render.c(f"loaded from {policy.source}", "dim"))
    print(
        f"risk threshold: {policy.risk_threshold}   "
        f"block on risk: {'yes' if policy.block_on_risk else 'no'}"
    )
    if not policy.block_on_risk:
        print(render.c(
            "  a high score warns but does not refuse; set block_on_risk to change that",
            "dim",
        ))
    print()
    rows = [
        [
            render.c(rule.name, "bold"),
            render.c(rule.action, {"block": "red", "warn": "yellow"}.get(rule.action, "dim")),
            str(rule.risk),
            render.truncate(rule.message, 66),
        ]
        for rule in policy.rules
    ]
    print(render.table(rows, ["RULE", "ACTION", "RISK", "MESSAGE"]))
    return 0


def _policy_test(toolkit: Toolkit, args) -> int:
    if not args.sql:
        print(f"{PROG}: policy test needs a statement", file=sys.stderr)
        return 1
    from .policy import PolicyEngine

    engine = PolicyEngine(_policy_for(toolkit, args))
    decision = engine.evaluate_sql(args.sql, context=toolkit._context())
    if args.json:
        print(json.dumps(_decision_json(decision), indent=2))
        return _exit_code(decision)
    print(render.format_decision(decision))
    print()
    print(render.c(decision.explain(), "dim"))
    return _exit_code(decision)


def _policy_lint(toolkit: Toolkit, args) -> int:
    """Validate a policy file. Loading it *is* the validation -- the loader
    rejects anything it cannot make sense of."""
    policy = _policy_for(toolkit, args)
    print(
        render.c("Policy is valid.", "green")
        + f" {len(policy.rules)} rule(s) from {policy.source}."
    )
    return 0


def _policy_path(toolkit: Toolkit, args) -> int:
    found = find_policy_file()
    if args.json:
        print(json.dumps({"path": str(found) if found else None,
                          "source": toolkit.policy.source}, indent=2))
        return 0
    if found:
        print(found)
    else:
        print(render.c("No ctrlz.policy.yaml found; using the built-in defaults.", "dim"))
        print(render.c("Create one to write your own rules: ctrlz policy show > "
                       "ctrlz.policy.yaml", "dim"))
    return 0


def cmd_purge(toolkit: Toolkit, args) -> int:
    what = f"older than {args.older_than}" if args.older_than else "ALL history"
    if not args.yes and not _confirm(f"Delete {what}?"):
        print("Cancelled.")
        return 1
    deleted = toolkit.purge(args.older_than)
    print(render.c(f"Purged {deleted} operation(s).", "green"))
    return 0


def cmd_ship(toolkit: Toolkit, args) -> int:
    from .hub import Hub, ship

    hub = Hub(args.to)
    try:
        result = ship(
            toolkit,
            hub,
            name=args.name,
            include_values=args.include_values,
            batch=args.batch,
        )
    finally:
        hub.close()

    if args.json:
        print(json.dumps(result.__dict__, indent=2, default=str))
        return 0

    print(
        render.c("Shipped.", "green")
        + f" {result.operations} operation(s), {result.changes} change(s) "
        f"from {result.source_name}."
    )
    if result.refreshed:
        print(render.c(f"  {result.refreshed} operation(s) marked undone.", "dim"))
    if not result.included_values:
        print(render.c(
            "  Metadata only. Row values stayed in the source database; pass "
            "--include-values to ship them too.", "dim"))
    print(render.c(f"  Watermark now {result.watermark}. Safe to run again.", "dim"))
    return 0


def cmd_hub(toolkit: Toolkit, args) -> int:
    from .hub import Hub

    hub = Hub(args.at)
    try:
        if args.action == "sources":
            return _hub_sources(hub, args)
        if args.action == "purge":
            return _hub_purge(hub, args)
        return _hub_log(hub, args)
    finally:
        hub.close()


def _hub_log(hub, args) -> int:
    operations = hub.operations(
        limit=args.limit,
        source=args.source,
        actor=args.actor,
        table=args.table,
        min_risk=args.min_risk,
    )
    if args.json:
        print(json.dumps([op.__dict__ for op in operations], indent=2, default=str))
        return 0
    if not operations:
        print(render.c("No shipped operations match.", "dim"))
        return 0

    rows = [
        [
            render.c(render.short(op.op_id), "bold"),
            render.truncate(op.source_name, 16),
            render.ago(op.started_at),
            render.c("undone", "dim") if op.already_undone else "",
            str(op.row_count),
            "-" if op.risk is None else str(op.risk),
            render.truncate(op.actor, 14),
            render.truncate(", ".join(op.tables) or "-", 26),
            render.truncate(op.label or "(unlabelled)", 30),
        ]
        for op in operations
    ]
    print(render.table(
        rows,
        ["ID", "SOURCE", "WHEN", "STATE", "ROWS", "RISK", "ACTOR", "TABLES", "LABEL"],
    ))
    print()
    print(render.c(
        "This is a replica. To reverse any of these, connect to the source "
        "database\nand run `ctrlz undo <id>` there -- the hub never applies "
        "changes itself.", "dim"))
    return 0


def _hub_sources(hub, args) -> int:
    sources = hub.sources()
    if args.json:
        print(json.dumps(sources, indent=2, default=str))
        return 0
    if not sources:
        print(render.c("No databases have shipped to this hub yet.", "dim"))
        return 0
    rows = [
        [
            render.c(s["name"], "bold"),
            s.get("engine") or "-",
            s.get("dsn_hint") or "-",
            str(s.get("watermark") or 0),
            render.ago(_parse_stamp(s.get("last_ship"))),
        ]
        for s in sources
    ]
    print(render.table(rows, ["NAME", "ENGINE", "WHERE", "WATERMARK", "LAST SHIP"]))
    return 0


def _hub_purge(hub, args) -> int:
    from .api import parse_duration

    what = f"older than {args.older_than}" if args.older_than else "ALL shipped history"
    if not args.yes and not _confirm(f"Delete {what} from the hub?"):
        print("Cancelled.")
        return 1
    seconds = parse_duration(args.older_than) if args.older_than else None
    deleted = hub.purge(seconds)
    print(render.c(f"Purged {deleted} operation(s) from the hub.", "green"))
    print(render.c("  The source databases were not touched.", "dim"))
    return 0


def _parse_stamp(value):
    from .hub import _parse

    return _parse(value)


def cmd_gateway(toolkit: Toolkit, args) -> int:
    import asyncio

    from .gateway import Gateway, Upstream

    upstream_dsn = args.upstream or args.dsn
    if not upstream_dsn:
        print(f"{PROG}: give --upstream or set CTRLZ_DSN", file=sys.stderr)
        return 1

    tracked = tuple(name for name, _ in toolkit.tracked())
    host, _, port = args.listen.rpartition(":")
    host = host or "127.0.0.1"

    gateway = Gateway(
        Upstream.from_dsn(upstream_dsn),
        policy=toolkit.policy,
        tracked=tracked,
        environment=toolkit.environment,
        attribute=not args.no_attribution,
    )

    print(
        f"{render.c('ctrlz gateway', 'green')} listening on {host}:{port} "
        f"-> {gateway.upstream.host}:{gateway.upstream.port}"
    )
    print(render.c(
        f"  {len(tracked)} tracked table(s); {len(toolkit.policy.rules)} rule(s) "
        f"from {toolkit.policy.source}", "dim"))
    print(render.c(
        "  Point your client at this address. Capture and undo do not depend on "
        "the gateway -- stopping it loses the checkpoint, never the recorder.",
        "dim",
    ))
    print(render.c(
        "  Client connections are plaintext: the gateway must read SQL, so it "
        "cannot pass TLS through. Bind it to localhost or a trusted network.",
        "yellow",
    ))

    async def serve() -> None:
        await gateway.start(host, int(port or 6543))
        await gateway.serve_forever()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    finally:
        print(
            f"\nStopped. {gateway.stats.connections} connection(s), "
            f"{gateway.stats.statements} statement(s) checked, "
            f"{gateway.stats.refused} refused."
        )
    return 0


def cmd_doctor(toolkit: Toolkit, args) -> int:
    info = toolkit.doctor()
    if args.json:
        print(json.dumps(info, indent=2, default=str))
        return 0

    print(f"engine:      {info['engine']}")
    print(f"initialized: {'yes' if info['initialized'] else 'no'}")
    print(f"actor:       {info['actor']}  (environment: {info['environment']})")
    print(
        f"policy:      {info['policy_rules']} rule(s) from {info['policy_source']}; "
        f"block on risk: {'yes' if info['block_on_risk'] else 'no'}"
    )
    if not info["initialized"]:
        print(render.c("\nRun: ctrlz init", "yellow"))
        return 1

    tracked = info.get("tracked") or []
    untracked = info.get("untracked") or []
    print(f"schema:      v{info.get('schema_version', '?')}")
    print(f"operations:  {info.get('operations', 0)} in history")
    print(f"\n{render.c('Protected', 'green')} ({len(tracked)} table(s))")
    for name, ident in tracked:
        print(f"  {name}  identity: {', '.join(ident)}")
    if untracked:
        print(f"\n{render.c('NOT protected', 'yellow')} ({len(untracked)} table(s))")
        for name in untracked:
            print(f"  {name}")
        print(render.c("  Changes to these tables cannot be undone.", "dim"))
    cascade_risks = info.get("cascade_risks") or {}
    if cascade_risks:
        print(f"\n{render.c('Cascade blind spot', 'red')} ({len(cascade_risks)} table(s))")
        for parent, children in sorted(cascade_risks.items()):
            print(f"  {parent}  ->  {', '.join(children)}")
        print(render.c(
            "  This database performs foreign-key cascades without firing triggers, "
            "so rows\n  removed from those children are never captured. Deletes from "
            "these tables are\n  reported as NOT undoable rather than partially "
            "restored.", "dim"))

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


def _decision_json(decision) -> dict:
    analysis = decision.analysis
    return {
        "outcome": decision.outcome,
        "risk": decision.risk,
        "risk_threshold": decision.risk_threshold,
        "block_on_risk": decision.block_on_risk,
        "decided_by": decision.decided_by.name if decision.decided_by else None,
        "scored_by": decision.scored_by.name if decision.scored_by else None,
        "matched": [
            {"rule": m.name, "action": m.rule.action, "risk": m.rule.risk,
             "message": m.message}
            for m in decision.matched
        ],
        "analysis": {
            "statement": analysis.statement,
            "kind": analysis.kind,
            "written_tables": list(analysis.written_tables),
            "read_tables": list(analysis.read_tables),
            "has_filter": analysis.has_filter,
            "confidence": analysis.confidence,
            "backend": analysis.backend,
            "notes": list(analysis.notes),
        },
    }


def _op_json(op) -> dict:
    return {
        "op_id": op.op_id,
        "label": op.label,
        "source": op.source,
        "actor": op.actor,
        "actor_user": op.actor_user,
        "actor_host": op.actor_host,
        "ticket": op.ticket,
        "channel": op.channel,
        "risk": op.risk,
        "policy_outcome": op.policy_outcome,
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
