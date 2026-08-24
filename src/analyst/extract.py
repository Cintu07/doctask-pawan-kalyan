"""Pulling claims out of documents.

Two extractors, in this order.

**Rules first.** A regex that finds "payment terms are net forty five (45) days"
is deterministic, cites its own span for free, costs nothing, and can be tested
without a key. Most of what a commercial register needs is of that shape, so
most of it is done this way on purpose.

**A model second**, only for what rules could not find. Every model claim must
come back with a quote, and :func:`verify_claims` throws away any claim whose
quote does not literally appear in the document it names.

That last step is the whole "it never bluffs" requirement. A model that invents
a clause does not get a low confidence score, it gets deleted, and the register
says "not stated" instead. Saying nothing is always available and always honest.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable

from .ingest import locator_for
from .models import Claim, Provenance, SourceDocument

log = logging.getLogger(__name__)

# Spelled-out numbers appear constantly in contracts, usually alongside digits
# ("forty five (45) days"). We read the digits when both are present.
_WORD_NUMBERS = {
    "zero": 0, "five": 5, "seven": 7, "ten": 10, "fourteen": 14, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45, "forty-five": 45,
    "sixty": 60, "ninety": 90, "one hundred": 100, "one hundred and twenty": 120,
    "one": 1, "two": 2, "three": 3, "six": 6, "twelve": 12,
}


@dataclass(frozen=True, slots=True)
class Subject:
    """One thing the register tracks."""

    key: str
    label: str
    unit: str
    question: str  # what a model is asked, when rules find nothing


SUBJECTS: tuple[Subject, ...] = (
    Subject("payment_terms_days", "Payment terms", "days",
            "How many days does the customer have to pay an invoice?"),
    Subject("liability_cap", "Limitation of liability", "currency",
            "What is the cap on liability, as an amount or a multiple of fees?"),
    Subject("notice_period_days", "Termination notice", "days",
            "How many days notice is required to terminate for convenience?"),
    Subject("auto_renewal_notice_days", "Auto-renewal notice", "days",
            "How many days before renewal must notice of non-renewal be given?"),
    Subject("governing_law", "Governing law", "text",
            "Which jurisdiction's law governs this agreement?"),
    Subject("contract_value", "Contract value", "currency",
            "What is the total contract value or fee?"),
    Subject("effective_date", "Effective date", "date",
            "On what date does this agreement take effect?"),
    Subject("rate_day", "Day rate", "currency",
            "What is the quoted day rate or hourly rate?"),
    Subject("invoice_total", "Invoice total", "currency",
            "What is the total amount due on this invoice?"),
)

SUBJECT_BY_KEY = {s.key: s for s in SUBJECTS}

# Which document kinds may legitimately state each term.
#
# Without this, "Total contract value: $240,000" in a statement of work gets
# read as an invoice total, and the register reports a billing dispute that does
# not exist. A false conflict is worse than a missed one: it teaches reviewers
# to skim the list.
#
# "unknown" appears nowhere on purpose. A document we could not classify is not
# a source of contractual positions.
RELEVANT_KINDS: dict[str, frozenset[str]] = {
    "payment_terms_days": frozenset({"msa", "amendment", "sow", "invoice"}),
    "liability_cap": frozenset({"msa", "amendment"}),
    "notice_period_days": frozenset({"msa", "amendment"}),
    "auto_renewal_notice_days": frozenset({"msa", "amendment"}),
    "governing_law": frozenset({"msa", "amendment", "sow"}),
    "contract_value": frozenset({"msa", "amendment", "sow"}),
    "effective_date": frozenset({"msa", "amendment", "sow", "invoice"}),
    "rate_day": frozenset({"msa", "amendment", "sow", "invoice"}),
    "invoice_total": frozenset({"invoice"}),
}


def subjects_for(kind: str) -> list[Subject]:
    """The terms a document of this kind is allowed to speak to."""
    return [s for s in SUBJECTS if kind in RELEVANT_KINDS.get(s.key, frozenset())]


# --------------------------------------------------------------------- helpers


def _to_int(text: str) -> int | None:
    """Read a count out of a captured phrase.

    The capture is often wider than the number itself, because the pattern that
    finds "sixty (60) days notice" has to tolerate the words in between and ends
    up holding "either party gives sixty". So rather than requiring the phrase to
    *be* a number, we look for the number inside it, longest spelling first so
    "forty five" is not read as "five".
    """
    digits = re.search(r"\d+", text.replace(",", ""))
    if digits:
        return int(digits.group(0))

    words = " ".join(text.strip().lower().split())
    for spelling in sorted(_WORD_NUMBERS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(spelling)}\b", words):
            return _WORD_NUMBERS[spelling]
    return None


def _to_money(text: str) -> Decimal | None:
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


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


# ----------------------------------------------------------------- rule set

RuleFn = Callable[[SourceDocument], Iterable[Claim]]

_NUM = r"(\d{1,4}|[a-z][a-z\- ]{2,24})"


def _simple_rule(subject: str, unit: str, patterns: tuple[str, ...], cast) -> RuleFn:
    compiled = [re.compile(p, re.I) for p in patterns]

    def run(document: SourceDocument) -> list[Claim]:
        out: list[Claim] = []
        for pattern in compiled:
            for match in pattern.finditer(document.text):
                raw = match.group("v")
                value = cast(raw)
                if value is None:
                    continue
                out.append(_claim(subject, value, unit, document, match, f"rule:{subject}"))
                break  # first statement in a document wins; later ones are context
            if out:
                break
        return out

    return run


RULES: dict[str, RuleFn] = {
    "payment_terms_days": _simple_rule(
        "payment_terms_days", "days",
        (
            rf"payment\s+terms?\s+(?:are|is|shall\s+be|of)?\s*(?:net\s+)?(?P<v>{_NUM})\s*(?:\(\d+\)\s*)?days",
            rf"net\s+(?P<v>{_NUM})\s*(?:\(\d+\)\s*)?days",
            rf"within\s+(?P<v>{_NUM})\s*(?:\(\d+\)\s*)?days\s+of\s+(?:the\s+)?invoice",
            rf"pay(?:able)?\s+within\s+(?P<v>{_NUM})\s*(?:\(\d+\)\s*)?days",
        ),
        _to_int,
    ),
    "notice_period_days": _simple_rule(
        "notice_period_days", "days",
        (
            rf"terminate[^.]{{0,60}}?(?:upon|on|with|giving)\s+(?P<v>{_NUM})\s*(?:\(\d+\)\s*)?days[^.]{{0,40}}notice",
            rf"(?P<v>{_NUM})\s*(?:\(\d+\)\s*)?days[^.]{{0,30}}written\s+notice[^.]{{0,40}}terminat",
        ),
        _to_int,
    ),
    "auto_renewal_notice_days": _simple_rule(
        "auto_renewal_notice_days", "days",
        (
            rf"(?:renew|renewal)[^.]{{0,90}}?(?P<v>{_NUM})\s*(?:\(\d+\)\s*)?days[^.]{{0,40}}notice",
            rf"(?P<v>{_NUM})\s*(?:\(\d+\)\s*)?days[^.]{{0,60}}(?:prior\s+to|before)[^.]{{0,40}}renew",
        ),
        _to_int,
    ),
    "liability_cap": _simple_rule(
        "liability_cap", "currency",
        (
            r"liability[^.]{0,120}?(?:shall\s+not\s+exceed|capped\s+at|limited\s+to)[^.]{0,40}?[$£€]\s*(?P<v>[\d,]+(?:\.\d+)?)",
            r"(?:aggregate\s+)?liability[^.]{0,80}?exceed[^.]{0,30}?[$£€]\s*(?P<v>[\d,]+(?:\.\d+)?)",
        ),
        _to_money,
    ),
    "contract_value": _simple_rule(
        "contract_value", "currency",
        (
            r"(?:total\s+(?:contract\s+)?(?:value|fees?)|contract\s+value)[^.]{0,40}?[$£€]\s*(?P<v>[\d,]+(?:\.\d+)?)",
        ),
        _to_money,
    ),
    "rate_day": _simple_rule(
        "rate_day", "currency",
        (
            r"[$£€]\s*(?P<v>[\d,]+(?:\.\d+)?)\s*(?:per|/)\s*(?:day|diem)",
            r"day\s+rate[^.]{0,30}?[$£€]\s*(?P<v>[\d,]+(?:\.\d+)?)",
        ),
        _to_money,
    ),
    "invoice_total": _simple_rule(
        "invoice_total", "currency",
        (
            r"(?:total\s+(?:amount\s+)?due|amount\s+due|invoice\s+total|total\s+payable)[^.\n]{0,30}?[$£€]\s*(?P<v>[\d,]+(?:\.\d+)?)",
        ),
        _to_money,
    ),
    "effective_date": _simple_rule(
        "effective_date", "date",
        (
            r"effective\s+(?:from|as\s+of|on)\s+(?P<v>\d{1,2}\s+\w+\s+\d{4})",
            r"\*\*Effective:\*\*\s*(?P<v>\d{1,2}\s+\w+\s+\d{4})",
            r"effective\s+date[^.\n]{0,12}?[:\s]\s*(?P<v>\d{1,2}\s+\w+\s+\d{4})",
        ),
        lambda s: s.strip() or None,
    ),
    "governing_law": _simple_rule(
        "governing_law", "text",
        (
            r"governed\s+by[^.]{0,60}?laws?\s+of\s+(?:the\s+)?(?P<v>[A-Z][A-Za-z ]{2,40}?)(?:\s*[,.]|\s+and\b)",
            r"governing\s+law[^.]{0,20}?:\s*(?P<v>[A-Z][A-Za-z ]{2,40})",
        ),
        lambda s: s.strip().rstrip(".,") or None,
    ),
}


def extract_with_rules(document: SourceDocument) -> list[Claim]:
    """Every claim the deterministic rules can find in one document.

    Only subjects this kind of document may speak to are attempted, so an
    invoice cannot contribute a liability cap and a statement of work cannot
    contribute an invoice total.
    """
    allowed = {s.key for s in subjects_for(document.kind)}
    claims: list[Claim] = []
    for subject, rule in RULES.items():
        if subject not in allowed:
            continue
        try:
            claims.extend(rule(document))
        except re.error as exc:  # pragma: no cover - guards a malformed pattern
            log.error("rule %s failed on %s: %s", subject, document.name, exc)
    return claims


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


def missing_subjects(claims: Iterable[Claim]) -> list[Subject]:
    """Subjects no rule could answer, which a model may be asked about."""
    found = {c.subject for c in claims if not c.is_unknown}
    return [s for s in SUBJECTS if s.key not in found]
