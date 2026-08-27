"""Pulling claims out of documents.

Two extractors, in this order.

**Rules first.** A pattern that finds "payment terms are net forty five (45)
days" is deterministic, cites its own span for free, costs nothing, and can be
tested without a key. Most of what a register needs is of that shape, so most of
it is done this way on purpose.

**A model second**, only for what the rules could not find. Every model claim
must come back with a quote, and :func:`verify_claims` throws away any claim
whose quote does not literally appear in the document it names.

That last step is the whole "it never bluffs" requirement. A model that invents
a clause does not get a low confidence score, it gets deleted, and the register
says "not stated" instead. Saying nothing is always available and always honest.

Nothing here knows what industry it is working in. The subjects, the patterns
and the document kinds all come from a domain pack, so pointing the system at
land titles instead of vendor contracts is a json file, not a rewrite.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable

from .domain import Domain, ExtractionRule, Subject, caster, load_domain, set_active
from .ingest import locator_for
from .models import Claim, Provenance, SourceDocument

log = logging.getLogger(__name__)

_ACTIVE: Domain | None = None


def active_domain() -> Domain:
    """The domain in force, loaded once."""
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = load_domain()
        set_active(_ACTIVE)
    return _ACTIVE


def use_domain(domain: Domain | str) -> Domain:
    """Switch the system to another industry."""
    global _ACTIVE
    _ACTIVE = domain if isinstance(domain, Domain) else load_domain(domain)
    set_active(_ACTIVE)
    return _ACTIVE


def _d(domain: Domain | None) -> Domain:
    return domain or active_domain()


# ------------------------------------------------------------------ helpers


def _sentence_span(text: str, start: int, end: int, pad: int = 220) -> tuple[int, int]:
    """Widen a match to its surrounding sentence, so the quote reads sensibly.

    A citation of "45" is useless to a reviewer. A citation of the sentence that
    contains it can be checked at a glance.
    """
    left = max(0, start - pad)
    right = min(len(text), end + pad)
    before = text.rfind(".", left, start)
    after = text.find(".", end, right)
    lo = before + 1 if before != -1 else left
    hi = after + 1 if after != -1 else right
    while lo < len(text) and text[lo] in " \n\t":
        lo += 1
    return lo, hi


def _claim(
    subject: str,
    value: object,
    unit: str,
    document: SourceDocument,
    match: re.Match[str],
    extractor: str,
) -> Claim:
    start, end = _sentence_span(document.text, match.start(), match.end())
    return Claim(
        id=f"clm_{uuid.uuid4().hex[:12]}",
        subject=subject,
        value=value,  # type: ignore[arg-type]
        unit=unit,
        provenance=Provenance(
            document_id=document.id,
            document_name=document.name,
            locator=locator_for(document.text, match.start()),
            start=start,
            end=end,
            quote=document.text[start:end],
        ),
        extractor=extractor,
    )


def apply_rule(rule: ExtractionRule, document: SourceDocument) -> list[Claim]:
    """Run one subject's patterns against one document.

    The first pattern that yields a value wins, and only its first match is
    taken. A contract restating its own payment terms in the definitions and
    again in the payment clause is not two claims, and treating it as two would
    manufacture a conflict inside a single document.
    """
    cast = caster(rule.cast)
    for pattern in rule.patterns:
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:  # pragma: no cover - load_domain already checked
            log.error("bad pattern for %s: %s", rule.subject, exc)
            continue
        for match in compiled.finditer(document.text):
            value = cast(match.group("v"))
            if value is None:
                continue
            return [_claim(rule.subject, value, rule.unit, document, match, f"rule:{rule.subject}")]
    return []


def extract_with_rules(document: SourceDocument, domain: Domain | None = None) -> list[Claim]:
    """Every claim the deterministic rules can find in one document.

    Only subjects this kind of document may speak to are attempted, so an
    invoice cannot contribute a liability cap and a statement of work cannot
    contribute an invoice total.
    """
    claims: list[Claim] = []
    for rule in _d(domain).rules_for(document.kind):
        claims.extend(apply_rule(rule, document))
    return claims


def subjects_for(kind: str, domain: Domain | None = None) -> list[Subject]:
    """The terms a document of this kind is allowed to speak to."""
    return _d(domain).subjects_for(kind)


def missing_subjects(claims: Iterable[Claim], domain: Domain | None = None) -> list[Subject]:
    """Subjects no rule could answer, which a model may be asked about."""
    found = {c.subject for c in claims if not c.is_unknown}
    return [s for s in _d(domain).subjects if s.key not in found]


# ------------------------------------------------------------------ the gate


def verify_claims(
    claims: Iterable[Claim], documents: dict[str, SourceDocument]
) -> tuple[list[Claim], list[str]]:
    """Keep only claims whose citation actually says what they claim.

    This is where a hallucinated clause dies. Returns the surviving claims and a
    rejection reason for each one dropped, because a model inventing citations
    is something the operator needs to see, not something to swallow quietly.
    """
    kept: list[Claim] = []
    rejected: list[str] = []

    for claim in claims:
        document = documents.get(claim.provenance.document_id)
        if document is None:
            rejected.append(
                f"{claim.subject}: cites document {claim.provenance.document_id} "
                f"which is not in this corpus ({claim.extractor})"
            )
            continue
        if not claim.provenance.verify(document.text):
            rejected.append(
                f"{claim.subject}: quoted text was not found at the cited span in "
                f"{document.name} ({claim.extractor}), claim discarded"
            )
            continue
        kept.append(claim)

    return kept, rejected
