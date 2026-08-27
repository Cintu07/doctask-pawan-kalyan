"""Run the analyst over 510 real commercial contracts.

The generated benchmark proves the logic against data whose answer I chose. This
one asks a different and less comfortable question: what happens on contracts
written by lawyers who had never heard of this system.

The corpus is CUAD, the Contract Understanding Atticus Dataset: 510 real
commercial agreements filed with the SEC, annotated by attorneys, released
CC BY 4.0 by The Atticus Project. Licences, supply agreements, distribution,
hosting, development, maintenance, agency. Median length 33,000 characters, the
longest 338,000.

    python scripts/realworld.py --fetch     # download the corpus, about 18 MB
    python scripts/realworld.py             # run it

WHAT IS MEASURED

  survival          documents read without an exception
  classification    what kind each document was judged to be, and how often
                    that judgement was "unknown"
  extraction        of the subjects a document of that kind may speak to, how
                    many were found. This is the number that will be worst, and
                    it should be: the rules were written against one drafting
                    style and real contracts do not share it.
  citation validity of the claims extracted, how many carry a quote that
                    verifies against the document at the cited span. This is the
                    one that must be 100%. Extraction can be weak. A citation
                    that does not check out is a lie.
  false quarantine  real contracts wrongly judged to be addressing the system.
                    Legal prose contains "shall act as agent" and similar, and a
                    guard that fires on those would be unusable in practice.

Figures are p50, p95 and worst. Raw per document results go to
benchmarks/realworld.json.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import re
import statistics
import sys
import time
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analyst.extract import active_domain, extract_with_rules, subjects_for, verify_claims  # noqa: E402
from analyst.ingest import UnsupportedDocument, ingest_file  # noqa: E402
from analyst.sanitize import scan_text  # noqa: E402

CORPUS = ROOT / "corpus" / "real-contracts"
OUT = ROOT / "benchmarks"
SOURCE = "https://raw.githubusercontent.com/TheAtticusProject/cuad/main/data.zip"


def fetch() -> int:
    """Download CUAD and write each contract out as plain text."""
    CORPUS.mkdir(parents=True, exist_ok=True)
    print(f"downloading CUAD from {SOURCE}")
    with urllib.request.urlopen(SOURCE, timeout=300) as response:
        blob = response.read()
    print(f"  {len(blob) / 1_048_576:.1f} MB")

    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        data = json.loads(z.read("CUADv1.json"))["data"]

    for record in data:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", record["title"]).strip("-")[:90]
        (CORPUS / f"{slug}.txt").write_text(
            record["paragraphs"][0]["context"], encoding="utf-8"
        )
    print(f"  wrote {len(data)} contracts to {CORPUS.relative_to(ROOT)}")
    print("  CUAD is CC BY 4.0, The Atticus Project. Not committed to this repo.")
    return 0


def spread(values: list[float]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 4),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 4),
        "worst": round(ordered[-1], 4),
        "mean": round(statistics.fmean(ordered), 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="download the corpus first")
    parser.add_argument("--limit", type=int, default=0, help="stop after N documents")
    args = parser.parse_args()

    if args.fetch:
        return fetch()

    if not CORPUS.is_dir() or not any(CORPUS.glob("*.txt")):
        print(f"no corpus at {CORPUS}. run: python scripts/realworld.py --fetch")
        return 1

    files = sorted(CORPUS.glob("*.txt"))
    if args.limit:
        files = files[: args.limit]

    domain = active_domain()
    print(f"reading {len(files)} real contracts against the "
          f"{domain.name} pack\n")

    rows: list[dict] = []
    crashes: list[str] = []

    for index, path in enumerate(files, 1):
        started = time.perf_counter()
        try:
            document = ingest_file(path)
        except UnsupportedDocument as exc:
            crashes.append(f"{path.name}: refused, {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - the question is whether it crashes
            crashes.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue

        claims = extract_with_rules(document)
        kept, rejected = verify_claims(claims, {document.id: document})
        flags = scan_text(document.text)
        elapsed = time.perf_counter() - started

        available = subjects_for(document.kind)
        rows.append({
            "name": path.name,
            "chars": len(document.text),
            "kind": document.kind,
            "subjects_available": len(available),
            "claims": len(claims),
            "verified": len(kept),
            "rejected": len(rejected),
            "found_subjects": sorted({c.subject for c in kept}),
            "guard_flags": len(flags),
            "guard_reasons": sorted({why for _, why, _ in flags})[:3],
            "guard_excerpts": [excerpt[:90] for _, _, excerpt in flags[:2]],
            "seconds": round(elapsed, 4),
        })

        if index % 100 == 0:
            print(f"  {index}/{len(files)}")

    total_claims = sum(r["claims"] for r in rows)
    total_verified = sum(r["verified"] for r in rows)
    kinds: dict[str, int] = {}
    subject_hits: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        for s in r["found_subjects"]:
            subject_hits[s] = subject_hits.get(s, 0) + 1

    coverage = [
        r["verified"] / r["subjects_available"]
        for r in rows if r["subjects_available"]
    ]
    quarantined = [r for r in rows if r["guard_flags"]]

    summary = {
        "corpus": "CUAD v1, The Atticus Project, CC BY 4.0",
        "documents": len(files),
        "read_without_error": len(rows),
        "crashes": crashes,
        "classification": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "citation_validity": {
            "claims_extracted": total_claims,
            "claims_verifying": total_verified,
            "rate": round(total_verified / total_claims, 4) if total_claims else None,
        },
        "subject_coverage_per_document": spread(coverage),
        "subjects_found_in_n_documents": dict(
            sorted(subject_hits.items(), key=lambda kv: -kv[1])
        ),
        "documents_flagged_by_the_guard": len(quarantined),
        "seconds_per_document": spread([r["seconds"] for r in rows]),
        "chars_per_document": spread([float(r["chars"]) for r in rows]),
    }

    OUT.mkdir(exist_ok=True)
    (OUT / "realworld.json").write_text(
        json.dumps({"summary": summary, "documents": rows}, indent=2), encoding="utf-8"
    )

    print(f"\n{'=' * 74}\nREAL CONTRACTS, {len(files)} of them\n{'=' * 74}")
    print(f"  read without error   {len(rows)}/{len(files)}"
          + (f", {len(crashes)} refused or failed" if crashes else ""))
    cv = summary["citation_validity"]
    print(f"  citation validity    {cv['rate']:.1%}  "
          f"({cv['claims_verifying']}/{cv['claims_extracted']} claims)")
    cov = summary["subject_coverage_per_document"]
    print(f"  subject coverage     p50 {cov['p50']:.1%}  p95 {cov['p95']:.1%}  "
          f"best {cov['worst']:.1%}")
    print(f"  guard flagged        {len(quarantined)} documents")
    sec = summary["seconds_per_document"]
    print(f"  seconds per document p50 {sec['p50']}  p95 {sec['p95']}  worst {sec['worst']}")

    print("\n  classified as:")
    for kind, n in summary["classification"].items():
        print(f"    {kind:22} {n:>4}  ({n / len(rows):.0%})")

    print("\n  subjects found, out of 510 documents:")
    for subject, n in summary["subjects_found_in_n_documents"].items():
        print(f"    {subject:28} {n:>4}  ({n / len(rows):.0%})")

    if quarantined:
        print(f"\n  documents the guard flagged ({len(quarantined)}), first few:")
        for r in quarantined[:5]:
            print(f"    {r['name'][:56]:58} {r['guard_reasons']}")

    if crashes:
        print(f"\n  refused or failed ({len(crashes)}), first few:")
        for c in crashes[:5]:
            print(f"    {c[:110]}")

    print(f"\n  raw: {(OUT / 'realworld.json').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
