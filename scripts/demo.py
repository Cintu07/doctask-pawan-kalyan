"""The whole system, end to end, with no key and no network.

Six acts:

1. read the pile and propose a register
2. show that a hostile document was quarantined, not obeyed
3. a person decides each conflict, item by item, rejecting one
4. publish, and see it refuse until the gate is clear
5. a new amendment arrives, and the update touches only what it should
6. prove the untouched rows are byte identical, and show what it cost

Run: python scripts/demo.py
"""

from __future__ import annotations

import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analyst.register import register_digest  # noqa: E402
from analyst.service import Analyst  # noqa: E402

CORPUS = ROOT / "corpus" / "northwind"
PLAYBOOK = ROOT / "playbooks" / "vendor-contract.json"
DATA = ROOT / "data" / "demo"

NEW_AMENDMENT = """# Amendment No. 2 to the Master Services Agreement

**Parties:** Northwind Analytics Ltd and Calder & Voss LLP
**Effective:** 1 February 2025

## 1. Payment

Payment terms are net 15 days from receipt of a valid invoice.

## 2. Liability

The aggregate liability of either party under this Agreement shall not exceed
$2,500,000.

All other terms of the Agreement remain unchanged.
"""


def rule(title: str) -> None:
    print(f"\n{'=' * 76}\n{title}\n{'=' * 76}")


def show_rows(rows) -> None:
    for row in rows:
        cites = "; ".join(p.cite() for p in row.citations) or "no source"
        print(f"  {row.label:26} {row.value[:44]:46} {cites[:60]}")


def main() -> int:
    shutil.rmtree(DATA, ignore_errors=True)
    workspace = DATA / "corpus"
    shutil.copytree(CORPUS, workspace)
    analyst = Analyst(data_dir=DATA)

    # ------------------------------------------------------------- act one
    rule("1. READ THE PILE")
    proposal = analyst.analyse(
        corpus_id="northwind", source_dir=workspace, playbook=PLAYBOOK, actor="demo"
    )
    print(f"  run {proposal.run_id}, revision {proposal.revision}")
    print(f"  {proposal.diff.summary()}\n")
    show_rows(proposal.state.rows)

    print("\n  stages, and what each one decided:")
    for stage in proposal.state.stages:
        print(f"    {stage.name:10} {stage.status:10} {stage.seconds:6.3f}s  {stage.detail}")

    # ------------------------------------------------------------- act two
    rule("2. A DOCUMENT THAT TRIED TO GIVE ORDERS")
    quarantine = [n for n in proposal.notes if "quarantined" in n]
    attempts = [n for n in proposal.notes if "addressed to the system" in n]
    print(f"  {len(attempts)} instruction-like passage(s) found")
    for note in quarantine:
        print(f"  {note}")
    payment_claims = [c for c in proposal.state.claims if c.subject == "payment_terms_days"]
    print(f"\n  payment terms claims that survived: {len(payment_claims)}")
    for claim in payment_claims:
        print(f"    {claim.rendered():10} from {claim.provenance.document_name}")
    print("  the vendor note asked for 90 days. it is not in that list.")

    # ----------------------------------------------------------- act three
    rule("3. A PERSON DECIDES, ITEM BY ITEM")
    conflicts = proposal.state.conflicts
    for conflict in conflicts:
        print(f"  {conflict.subject}")
        print(f"    {conflict.explanation[:110]}")
        for claim in conflict.claims:
            print(f"      {claim.id}  {claim.rendered():14} {claim.provenance.cite()}")

    # Accept every conflict in favour of the amendment, except the day rate,
    # which is a genuine billing exception the reviewer wants left open.
    decisions, chosen = {}, {}
    for conflict in conflicts:
        if conflict.subject == "rate_day":
            decisions[conflict.id] = "rejected"
            continue
        winner = next(
            (c for c in conflict.claims if "amendment" in c.provenance.document_name),
            conflict.claims[-1],
        )
        decisions[conflict.id] = "accepted"
        chosen[conflict.id] = winner.id

    result = analyst.decide(
        corpus_id="northwind", run_id=proposal.run_id,
        decisions=decisions, chosen=chosen, actor="pawan",
    )
    print(f"\n  applied {len(result['applied'])} decision(s)")
    print(f"  open conflicts remaining: {result['open_conflicts']}")

    # ------------------------------------------------------------ act four
    rule("4. THE GATE")
    try:
        analyst.publish(corpus_id="northwind", run_id=proposal.run_id, actor="pawan")
        print("  published")
    except PermissionError as exc:
        print(f"  refused: {exc}")
        print("\n  the reviewer settles the last one, then publishes")
        rate = next(c for c in conflicts if c.subject == "rate_day")
        analyst.decide(
            corpus_id="northwind", run_id=proposal.run_id,
            decisions={rate.id: "accepted"},
            chosen={rate.id: rate.claims[0].id},
            notes={rate.id: "SOW rate governs, invoice to be re-issued"},
            actor="pawan",
        )
        published = analyst.publish(
            corpus_id="northwind", run_id=proposal.run_id, actor="pawan"
        )
        print(f"  published revision {published['revision']}, digest {published['digest']}")

    revision_one, rows_one = analyst.store.load_register("northwind")
    digest_one = register_digest(rows_one)

    # ------------------------------------------------------------ act five
    rule("5. A NEW DOCUMENT ARRIVES")
    (workspace / "06-amendment-02.md").write_text(NEW_AMENDMENT, encoding="utf-8")
    print("  dropped 06-amendment-02.md into the watched folder")
    print("  it changes payment terms to 15 days and the liability cap to $2,500,000")

    update = analyst.analyse(
        corpus_id="northwind", source_dir=workspace, playbook=PLAYBOOK, actor="demo"
    )
    print(f"\n  {update.diff.summary()}")
    for change in update.diff.touched:
        because = ", ".join(change.because) or "an existing document"
        print(f"    {change.action:8} {change.subject:26} {change.before.value if change.before else '-'} "
              f"-> {change.after.value if change.after else '-'}")
        print(f"             because: {because}")

    print("\n  rows deliberately left alone:")
    for change in update.diff.kept:
        print(f"    kept     {change.subject}")

    # ------------------------------------------------------------- act six
    rule("6. PROOF, AND WHAT IT COST")
    identical = all(
        c.before is not None and c.after is not None
        and c.before.render_html() == c.after.render_html()
        for c in update.diff.kept
    )
    print(f"  untouched rows byte identical: {identical} ({len(update.diff.kept)} row(s))")
    print(f"  register digest before update: {digest_one}")
    print(f"  rows that moved: {[c.subject for c in update.diff.touched]}")

    cost = update.state.cost_report()
    print(f"\n  this update took {cost['total_seconds']}s and made {cost['model_calls']} model call(s)")
    for stage in cost["stages"]:
        print(f"    {stage['name']:10} {stage['status']:10} {stage['seconds']:6.3f}s")

    rule("AUDIT TRAIL")
    for event in analyst.history("northwind", limit=12):
        detail = (event["detail"] or "")[:60]
        print(f"  {event['at']}  {event['actor']:8} {event['action']:20} {detail}")

    print(f"\nartefacts in {DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
