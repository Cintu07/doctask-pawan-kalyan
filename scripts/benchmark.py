"""Measure the claims instead of repeating them.

METHOD, stated before the result.

Corpora are generated, so the right answer is known before the system sees the
documents. Each corpus is a master agreement, an amendment, a statement of work
and an invoice, with values drawn at random and a chosen number of
contradictions planted between them. The planted set is the ground truth.

Four things are measured on every corpus:

  conflict recall     of the contradictions planted, how many were found
  false conflicts     conflicts reported that were never planted
  citation validity   of the rows that state a value, how many carry a citation
                      whose quote verifies against the document it names
  cost                wall clock, and model calls

Reported as p50, p95 and worst, never as a bare mean, because the mean of a
distribution with a bad tail is a way of hiding the tail. Raw per-corpus results
are written to benchmarks/results.json so the numbers can be checked rather than
believed.

    python scripts/benchmark.py            # 40 corpora
    python scripts/benchmark.py --n 200    # more, if you want tighter tails
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analyst.extract import verify_claims  # noqa: E402
from analyst.service import Analyst  # noqa: E402

OUT = ROOT / "benchmarks"

MSA = """MASTER SERVICES AGREEMENT

This Agreement is made between {customer} and {vendor}, effective 1 March 2024.

2.2 Renewal. This Agreement renews automatically unless either party gives
{renewal} days notice prior to the renewal date.

3.1 Payment. Payment terms are net {payment_msa} days from receipt of a valid
invoice.

4.1 Termination. Either party may terminate upon {notice} days written notice.

5.1 Liability. The aggregate liability of either party shall not exceed
${cap_msa}.

6.1 Governing law. This Agreement is governed by the laws of {law}.
"""

AMENDMENT = """# Amendment No. 1

**Effective:** 1 September 2024

This Amendment modifies the Master Services Agreement.

1. Payment terms are net {payment_amd} days.

2. The aggregate liability shall not exceed ${cap_amd}.

3. Renewal notice is {renewal_amd} days prior to the renewal date.
"""

SOW = """STATEMENT OF WORK 004

Issued under the Master Services Agreement.

Day rate: ${rate_sow} per day.

Total contract value: ${value}.
"""

INVOICE = """<html><body>
<h1>Invoice {number}</h1>
<p>Rate: ${rate_inv} per day</p>
<p>Payment terms are net {payment_inv} days.</p>
<p>Total amount due: ${total}</p>
</body></html>"""


def build_corpus(folder: pathlib.Path, rng: random.Random, planted: set[str]) -> dict:
    """Write one corpus and return the ground truth for it."""
    folder.mkdir(parents=True, exist_ok=True)

    payment = rng.choice([15, 30, 45, 60])
    cap = rng.choice([250_000, 500_000, 1_000_000])
    renewal = rng.choice([30, 60, 90])
    rate = rng.choice([900, 1_200, 1_450])

    # A planted subject gets a genuinely different value in a second document.
    # Anything not planted is stated identically everywhere, so a conflict
    # reported on it is a false positive by construction.
    def other(value: int, options: list[int]) -> int:
        return rng.choice([o for o in options if o != value])

    payment_amd = other(payment, [15, 30, 45, 60]) if "payment_terms_days" in planted else payment
    cap_amd = other(cap, [250_000, 500_000, 1_000_000]) if "liability_cap" in planted else cap
    renewal_amd = other(renewal, [30, 60, 90]) if "auto_renewal_notice_days" in planted else renewal
    rate_inv = other(rate, [900, 1_200, 1_450]) if "rate_day" in planted else rate

    values = {
        "customer": f"Client {rng.randint(100, 999)} Ltd",
        "vendor": f"Vendor {rng.randint(100, 999)} Inc",
        "payment_msa": payment,
        "payment_amd": payment_amd,
        "payment_inv": payment,
        "cap_msa": f"{cap:,}",
        "cap_amd": f"{cap_amd:,}",
        "renewal": renewal,
        "renewal_amd": renewal_amd,
        "notice": rng.choice([30, 60, 90]),
        "law": rng.choice(["Delaware", "England and Wales", "New York"]),
        "rate_sow": f"{rate:,}",
        "rate_inv": f"{rate_inv:,}",
        "value": f"{rng.randint(100, 900) * 1000:,}",
        "number": f"NW-2024-{rng.randint(100, 999)}",
        "total": f"{rng.randint(10, 90) * 1000:,}",
    }

    (folder / "01-msa.txt").write_text(MSA.format(**values), encoding="utf-8")
    (folder / "02-amendment-01.md").write_text(AMENDMENT.format(**values), encoding="utf-8")
    (folder / "03-sow-004.txt").write_text(SOW.format(**values), encoding="utf-8")
    (folder / "04-invoice.html").write_text(INVOICE.format(**values), encoding="utf-8")

    return {"planted": sorted(planted), "values": values}


SUBJECTS = ["payment_terms_days", "liability_cap", "auto_renewal_notice_days", "rate_day"]


def run_one(index: int, rng: random.Random, work: pathlib.Path, playbook: pathlib.Path) -> dict:
    how_many = rng.randint(0, len(SUBJECTS))
    planted = set(rng.sample(SUBJECTS, how_many))

    folder = work / f"corpus-{index:03d}"
    truth = build_corpus(folder, rng, planted)

    analyst = Analyst(data_dir=work / f"db-{index:03d}")
    started = time.perf_counter()
    proposal = analyst.analyse(corpus_id=f"c{index}", source_dir=folder, playbook=playbook)
    elapsed = time.perf_counter() - started

    found = {c.subject for c in proposal.state.conflicts}
    documents = {d.id: d for d in proposal.state.documents}

    # A row that states a value must carry at least one citation that actually
    # verifies. This is the "it never bluffs" claim, measured.
    stated = [r for r in proposal.state.rows if r.value != "not stated"]
    cited = 0
    for row in stated:
        claims = [c for c in proposal.state.claims if c.subject == row.subject]
        kept, _ = verify_claims(claims, documents)
        if kept:
            cited += 1

    return {
        "corpus": index,
        "planted": truth["planted"],
        "found": sorted(found),
        "recalled": sorted(planted & found),
        "missed": sorted(planted - found),
        "false_positives": sorted(found - planted),
        "rows_stated": len(stated),
        "rows_cited": cited,
        "seconds": round(elapsed, 4),
        "model_calls": proposal.state.cost_report()["model_calls"],
    }


def spread(values: list[float]) -> dict:
    """p50, p95 and worst. Never a bare mean."""
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 4),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 4),
        "worst": round(ordered[-1], 4),
        "mean": round(statistics.fmean(ordered), 4),
        "stdev": round(statistics.stdev(ordered), 4) if len(ordered) > 1 else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=40, help="how many corpora to generate")
    parser.add_argument("--seed", type=int, default=20260826)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    playbook = ROOT / "playbooks" / "vendor-contract.json"
    work = pathlib.Path(tempfile.mkdtemp(prefix="bench-"))

    print(f"generating and analysing {args.n} corpora, seed {args.seed}\n")
    rows = []
    for i in range(args.n):
        rows.append(run_one(i, rng, work, playbook))
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{args.n}")

    planted_total = sum(len(r["planted"]) for r in rows)
    recalled_total = sum(len(r["recalled"]) for r in rows)
    fp_total = sum(len(r["false_positives"]) for r in rows)
    stated_total = sum(r["rows_stated"] for r in rows)
    cited_total = sum(r["rows_cited"] for r in rows)

    per_corpus_recall = [
        len(r["recalled"]) / len(r["planted"]) for r in rows if r["planted"]
    ]

    summary = {
        "method": "generated corpora with known planted contradictions; see the "
                  "module docstring for the full method",
        "corpora": args.n,
        "seed": args.seed,
        "conflict_recall": {
            "planted": planted_total,
            "found": recalled_total,
            "rate": round(recalled_total / planted_total, 4) if planted_total else None,
            "per_corpus": spread(per_corpus_recall),
        },
        "false_conflicts": {
            "total": fp_total,
            "corpora_with_any": sum(1 for r in rows if r["false_positives"]),
        },
        "citation_validity": {
            "rows_stating_a_value": stated_total,
            "rows_with_a_verifying_citation": cited_total,
            "rate": round(cited_total / stated_total, 4) if stated_total else None,
        },
        "seconds_per_corpus": spread([r["seconds"] for r in rows]),
        "model_calls_total": sum(r["model_calls"] for r in rows),
    }

    OUT.mkdir(exist_ok=True)
    (OUT / "results.json").write_text(
        json.dumps({"summary": summary, "runs": rows}, indent=2), encoding="utf-8"
    )

    print(f"\n{'=' * 70}\nRESULT over {args.n} corpora\n{'=' * 70}")
    cr = summary["conflict_recall"]
    print(f"  conflict recall     {cr['rate']:.1%}  ({cr['found']}/{cr['planted']} planted)")
    print(f"    per corpus        p50 {cr['per_corpus'].get('p50')}  "
          f"worst {cr['per_corpus'].get('worst')}")
    print(f"  false conflicts     {fp_total} across {args.n} corpora "
          f"({summary['false_conflicts']['corpora_with_any']} corpora affected)")
    cv = summary["citation_validity"]
    print(f"  citation validity   {cv['rate']:.1%}  "
          f"({cv['rows_with_a_verifying_citation']}/{cv['rows_stating_a_value']} stated rows)")
    sec = summary["seconds_per_corpus"]
    print(f"  seconds per corpus  p50 {sec['p50']}  p95 {sec['p95']}  worst {sec['worst']}")
    print(f"  model calls         {summary['model_calls_total']}")

    worst = sorted(rows, key=lambda r: len(r["missed"]), reverse=True)[:3]
    if any(w["missed"] for w in worst):
        print("\n  worst corpora, so the tail is visible rather than averaged away:")
        for w in worst:
            if w["missed"]:
                print(f"    corpus {w['corpus']:>3}  missed {', '.join(w['missed'])}")

    print(f"\n  raw results: {(OUT / 'results.json').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
