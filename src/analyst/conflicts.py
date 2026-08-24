"""Finding where the pile disagrees with itself.

The brief is explicit: when a new source contradicts what the deliverable says,
the conflict is surfaced, not silently resolved. That is also the only honest
behaviour. Which document wins is a commercial judgement, and a parser has no
business making it.

So this module detects and explains. It will tell a reviewer that the amendment
is dated after the master agreement and therefore probably governs, because that
is useful. It will not act on that belief.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal
from itertools import combinations

from .models import Claim, Conflict, Decision, SourceDocument

# How far apart two money figures must be before we call it a disagreement.
# Exact equality would flag 45000 against 45000.00, which is noise.
MONEY_TOLERANCE = Decimal("0.01")

# A document kind's authority when two of them disagree. Higher wins, but only
# as an explanation offered to the reviewer, never as an automatic resolution.
PRECEDENCE = {"amendment": 3, "sow": 2, "msa": 2, "invoice": 1, "policy": 0, "unknown": 0}


def _same_value(a: Claim, b: Claim) -> bool:
    if a.value is None or b.value is None:
        return a.value is b.value
    if isinstance(a.value, Decimal) and isinstance(b.value, Decimal):
        return abs(a.value - b.value) <= MONEY_TOLERANCE
    return str(a.value).strip().lower() == str(b.value).strip().lower()


def _explain(a: Claim, b: Claim, documents: dict[str, SourceDocument]) -> tuple[str, str]:
    """Classify a disagreement and say what a reviewer should think about."""
    doc_a = documents.get(a.provenance.document_id)
    doc_b = documents.get(b.provenance.document_id)
    kind_a = doc_a.kind if doc_a else "unknown"
    kind_b = doc_b.kind if doc_b else "unknown"

    if {kind_a, kind_b} == {"amendment", "msa"}:
        amendment = a if kind_a == "amendment" else b
        return (
            "supersession_unclear",
            f"An amendment states a different value from the master agreement. "
            f"{amendment.provenance.document_name} would normally govern, but this "
            f"system does not apply precedence on its own. Confirm which controls.",
        )

    if "invoice" in (kind_a, kind_b):
        invoice = a if kind_a == "invoice" else b
        contract = b if kind_a == "invoice" else a
        return (
            "value_mismatch",
            f"An invoice states {invoice.rendered()} where the contract states "
            f"{contract.rendered()}. This is a billing exception worth checking "
            f"before payment.",
        )

    higher = max((kind_a, kind_b), key=lambda k: PRECEDENCE.get(k, 0))
    return (
        "value_mismatch",
        f"Two {higher} documents state different values for the same term. "
        f"Neither supersedes the other on its face.",
    )


# Subjects where documents are *expected* to differ, so a difference is not a
# contradiction. Each document in a file has its own effective date; reporting
# that as a dispute is noise, and noise is how a findings list stops being read.
PER_DOCUMENT_SUBJECTS = frozenset({"effective_date", "invoice_total"})


def carry_forward_decisions(
    fresh: list[Conflict], previous: list[Conflict]
) -> tuple[list[Conflict], int]:
    """Keep decisions a human already made, where the argument has not changed.

    Without this, every update re-opens every settled question. A reviewer who
    chose the amendment's payment terms last month should not be asked again
    because an unrelated document arrived.

    A decision is only carried when the *positions* are identical. If a new
    document adds a third value, the question is genuinely different and goes
    back to the human.
    """
    def fingerprint(conflict: Conflict) -> tuple[str, tuple[str, ...]]:
        return conflict.subject, tuple(sorted(c.digest() for c in conflict.claims))

    settled = {
        fingerprint(c): c for c in previous if c.decision is not Decision.PENDING
    }

    carried = 0
    for conflict in fresh:
        earlier = settled.get(fingerprint(conflict))
        if earlier is None:
            continue
        conflict.decision = earlier.decision
        conflict.decided_by = earlier.decided_by
        conflict.note = earlier.note
        if earlier.chosen_claim_id:
            # Claim ids are regenerated each run, so match on the value instead.
            winner = next(
                (
                    c for c in conflict.claims
                    if any(
                        c.digest() == old.digest()
                        for old in earlier.claims
                        if old.id == earlier.chosen_claim_id
                    )
                ),
                None,
            )
            if winner is not None:
                conflict.chosen_claim_id = winner.id
        carried += 1

    return fresh, carried


def detect_conflicts(
    claims: list[Claim], documents: dict[str, SourceDocument]
) -> list[Conflict]:
    """Every pair of claims about the same subject that do not agree.

    Claims from the same document are not compared: a contract restating its own
    payment terms in the definitions and again in the payment clause is not a
    contradiction, and flagging it would train reviewers to ignore the list.
    """
    by_subject: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        if not claim.is_unknown:
            by_subject[claim.subject].append(claim)

    conflicts: list[Conflict] = []
    for subject, group in sorted(by_subject.items()):
        if subject in PER_DOCUMENT_SUBJECTS:
            continue
        seen: list[tuple[Claim, Claim]] = []
        for a, b in combinations(group, 2):
            if a.provenance.document_id == b.provenance.document_id:
                continue
            if _same_value(a, b):
                continue
            seen.append((a, b))

        if not seen:
            continue

        # Collapse the pairs into one conflict per subject, listing every
        # distinct position. Three documents disagreeing is one argument to
        # settle, not three.
        involved: list[Claim] = []
        for a, b in seen:
            for claim in (a, b):
                if not any(_same_value(claim, existing) for existing in involved):
                    involved.append(claim)

        kind, explanation = _explain(seen[0][0], seen[0][1], documents)
        involved.sort(key=lambda c: c.provenance.document_name)
        conflicts.append(
            Conflict(
                id=f"cfl_{uuid.uuid4().hex[:12]}",
                subject=subject,
                claims=involved,
                kind=kind,
                explanation=explanation,
            )
        )

    return conflicts


def agreed_claim(
    subject: str, claims: list[Claim], conflicts: list[Conflict]
) -> tuple[Claim | None, str | None]:
    """The claim the register should show for a subject, and any conflict id.

    Where a conflict is open, the register shows the disagreement rather than
    picking a side. Where a human has settled it, their choice is used and
    attributed to them.
    """
    relevant = [c for c in claims if c.subject == subject and not c.is_unknown]
    if not relevant:
        return None, None

    conflict = next((c for c in conflicts if c.subject == subject), None)
    if conflict is None:
        return relevant[0], None

    if conflict.chosen_claim_id:
        chosen = next((c for c in relevant if c.id == conflict.chosen_claim_id), None)
        if chosen is not None:
            return chosen, conflict.id

    return None, conflict.id
