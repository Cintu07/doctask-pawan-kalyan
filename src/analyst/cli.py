"""One command line for the whole system.

    analyst demo                          the whole story, offline, in one go
    analyst analyse <corpus> <folder>     read the pile, propose a register
    analyst review <run_id>               see what is waiting for a decision
    analyst decide <run_id> <item> ok|no  accept or reject one item
    analyst publish <run_id>              commit, once the gate is clear
    analyst watch <corpus> <folder>       keep watching for new documents
    analyst register <corpus>             write the current register to HTML
    analyst history <corpus>              what changed, when, and why
    analyst serve                         the HTTP API
    analyst mcp                           the MCP server

``watch`` is movement three: new documents keep arriving, and each arrival gets
a focused update rather than a rewrite. It never publishes on its own. Deciding
what a contradiction means is a person's job, so the watcher's output is a
proposal waiting for one.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import time

from .register import register_digest
from .service import Analyst, LeaseHeld

log = logging.getLogger("analyst")


def _analyst(args) -> Analyst:
    return Analyst(data_dir=args.data)


def cmd_analyse(args) -> int:
    print("reading the pile...", flush=True)
    analyst = _analyst(args)
    proposal = analyst.analyse(
        corpus_id=args.corpus,
        source_dir=args.folder,
        playbook=args.playbook,
        actor=args.actor,
        domain=args.domain,
    )
    print(f"run {proposal.run_id}  revision {proposal.revision}")
    print(f"{proposal.diff.summary()}\n")

    for row in proposal.state.rows:
        cites = "; ".join(p.cite() for p in row.citations) or "no source"
        print(f"  {row.label:26} {row.value[:44]:46} {cites[:58]}")

    if proposal.state.open_conflicts:
        print(f"\n{len(proposal.state.open_conflicts)} conflict(s) need a decision:")
        for conflict in proposal.state.open_conflicts:
            print(f"  {conflict.id}  {conflict.subject}")
            for claim in conflict.claims:
                print(f"      {claim.id}  {claim.rendered():16} {claim.provenance.cite()}")

    breaches = [f for f in proposal.state.findings if f.severity != "info"]
    if breaches:
        print(f"\n{len(breaches)} finding(s):")
        for finding in breaches:
            print(f"  [{finding.severity}] {finding.rule_id}: {finding.message}")
    else:
        print("\nno findings: every rule was checked and none was breached")

    for note in proposal.notes:
        print(f"  ! {note[:150]}")
    return 0


def cmd_review(args) -> int:
    analyst = _analyst(args)
    state = analyst.store.load_run(args.run_id)
    if state is None:
        print(f"unknown run {args.run_id}", file=sys.stderr)
        return 1
    print(f"run {state.run_id}  corpus {state.corpus_id}  stage {state.stage}")
    print(f"next checkpoint: {analyst.pending_stage(args.run_id)}\n")
    for conflict in state.conflicts:
        mark = "open" if conflict.is_open else conflict.decision.value
        print(f"  [{mark:8}] {conflict.id}  {conflict.subject}")
        print(f"             {conflict.explanation[:100]}")
        for claim in conflict.claims:
            chosen = " <- chosen" if claim.id == conflict.chosen_claim_id else ""
            print(f"      {claim.id}  {claim.rendered():16} {claim.provenance.cite()}{chosen}")
    cost = state.cost_report()
    print(f"\ncost: {cost['total_seconds']}s, {cost['model_calls']} model call(s)")
    return 0


def cmd_decide(args) -> int:
    analyst = _analyst(args)
    state = analyst.store.load_run(args.run_id)
    if state is None:
        print(f"unknown run {args.run_id}", file=sys.stderr)
        return 1
    verdict = "accepted" if args.verdict in ("ok", "accept", "accepted", "yes") else "rejected"
    try:
        result = analyst.decide(
            corpus_id=state.corpus_id,
            run_id=args.run_id,
            decisions={args.item: verdict},
            chosen={args.item: args.claim} if args.claim else {},
            notes={args.item: args.note} if args.note else {},
            actor=args.actor,
        )
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if result["unknown"]:
        print(f"no such item: {', '.join(result['unknown'])}", file=sys.stderr)
        return 1
    print(f"{args.item} {verdict}. {result['open_conflicts']} conflict(s) still open.")
    return 0


def cmd_publish(args) -> int:
    analyst = _analyst(args)
    state = analyst.store.load_run(args.run_id)
    if state is None:
        print(f"unknown run {args.run_id}", file=sys.stderr)
        return 1
    try:
        result = analyst.publish(
            corpus_id=state.corpus_id, run_id=args.run_id, actor=args.actor, force=args.force
        )
    except PermissionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"published revision {result['revision']}, digest {result['digest']}")
    print(f"changed: {', '.join(result['changed']) or 'nothing'}")
    print(f"unchanged and byte identical: {result['unchanged']} row(s)")
    return 0


def cmd_register(args) -> int:
    analyst = _analyst(args)
    html = analyst.register_html(args.corpus)
    out = pathlib.Path(args.out or f"{args.corpus}-register.html")
    out.write_text(html, encoding="utf-8")
    revision, rows = analyst.store.load_register(args.corpus)
    print(f"revision {revision}, {len(rows)} rows, digest {register_digest(rows)}")
    print(f"wrote {out}")
    return 0


def cmd_history(args) -> int:
    analyst = _analyst(args)
    for event in analyst.history(args.corpus, args.limit):
        detail = (event["detail"] or "")[:70]
        because = f"  because: {event['because']}" if event["because"] else ""
        print(f"{event['at']}  {event['actor']:10} {event['action']:22} {detail}{because}")
    return 0


def cmd_watch(args) -> int:
    """Poll a folder and propose a focused update whenever it changes.

    Polling rather than filesystem events on purpose: it is the same code on
    every platform, it survives a network share that does not emit events, and
    a missed event here means a stale register, which is the failure this whole
    system exists to prevent.
    """
    analyst = _analyst(args)
    folder = pathlib.Path(args.folder)
    print(f"watching {folder} for {args.corpus}, every {args.interval}s. ctrl-c to stop.")
    seen = analyst.store.known_hashes(args.corpus)

    while True:
        try:
            from .ingest import ingest_directory

            arrived, problems = ingest_directory(folder, known_hashes=seen)
            for problem in problems:
                print(f"  ! {problem}")

            if arrived:
                names = ", ".join(d.name for d in arrived)
                print(f"\n{time.strftime('%H:%M:%S')}  new: {names}")
                proposal = analyst.analyse(
                    corpus_id=args.corpus,
                    source_dir=folder,
                    playbook=args.playbook,
                    actor="watcher",
                    domain=getattr(args, "domain", None),
                )
                print(f"  {proposal.diff.summary()}")
                for change in proposal.diff.touched:
                    before = change.before.value if change.before else "-"
                    after = change.after.value if change.after else "-"
                    print(f"    {change.action:8} {change.subject:26} {before} -> {after}")
                if proposal.state.open_conflicts:
                    print(
                        f"  {len(proposal.state.open_conflicts)} conflict(s) waiting for a "
                        f"person: analyst review {proposal.run_id}"
                    )
                else:
                    print(f"  nothing contested. analyst publish {proposal.run_id}")
                seen |= {d.sha256 for d in arrived}

            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped")
            return 0
        except LeaseHeld as exc:
            print(f"  busy: {exc}")
            time.sleep(args.interval)


def cmd_domains(args) -> int:
    from .domain import available_domains, load_domain

    for name in available_domains():
        d = load_domain(name)
        print(f"{name}")
        print(f"  {d.description}")
        print(f"  {len(d.subjects)} subjects, {len(d.rules)} rules, "
              f"{len(d.kinds)} document kinds")
    return 0


def cmd_demo(args) -> int:
    import runpy

    script = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "demo.py"
    runpy.run_path(str(script), run_name="__main__")
    return 0


def cmd_serve(args) -> int:
    # Printed before the imports, not after: uvicorn and the graph stack take a
    # few seconds to load and a silent terminal reads as a hang.
    print(f"starting the analyst api on http://{args.host}:{args.port}")
    print(f"  review interface  http://{args.host}:{args.port}/ui")
    print(f"  api docs          http://{args.host}:{args.port}/docs")
    print("loading, this takes a few seconds. ctrl-c to stop.\n", flush=True)

    import uvicorn

    uvicorn.run("analyst.api:app", host=args.host, port=args.port, reload=False)
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import mcp

    mcp.run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="analyst", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default="data", help="where state lives")
    parser.add_argument("--actor", default="cli", help="who is doing this, for the audit trail")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyse", help="read the pile and propose a register")
    p.add_argument("corpus")
    p.add_argument("folder")
    p.add_argument("--playbook", default=None)
    p.add_argument("--domain", default=None,
                   help="which industry pack to use, e.g. vendor-contracts or land-title")
    p.set_defaults(func=cmd_analyse)

    p = sub.add_parser("review", help="see what is waiting for a decision")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("decide", help="accept or reject one item")
    p.add_argument("run_id")
    p.add_argument("item")
    p.add_argument("verdict", choices=["ok", "no", "accept", "reject", "accepted", "rejected", "yes"])
    p.add_argument("--claim", default=None, help="which claim wins, when accepting a conflict")
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("publish", help="commit the register")
    p.add_argument("run_id")
    p.add_argument("--force", action="store_true", help="override open conflicts, audited")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("register", help="write the current register to HTML")
    p.add_argument("corpus")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("history", help="what changed, when, and because of which source")
    p.add_argument("corpus")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("watch", help="keep watching a folder for new documents")
    p.add_argument("corpus")
    p.add_argument("folder")
    p.add_argument("--playbook", default=None)
    p.add_argument("--domain", default=None)
    p.add_argument("--interval", type=float, default=5.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("domains", help="list the industry packs available")
    p.set_defaults(func=cmd_domains)

    p = sub.add_parser("demo", help="the whole story, offline, in one go")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("serve", help="the HTTP API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mcp", help="the MCP server, over stdio")
    p.set_defaults(func=cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
