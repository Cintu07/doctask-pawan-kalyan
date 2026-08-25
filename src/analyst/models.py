"""Core types.

The system rests on one rule: **nothing enters the deliverable that cannot be
traced to a span of text in a source document.** A claim carries the document it
came from, the character range, and the exact quote. A quote that does not
appear verbatim in that range is not a weak claim, it is a rejected one.

That single rule is what makes "it never bluffs" a property we can test rather
than a promise we make.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

DocumentKind = Literal["msa", "amendment", "sow", "invoice", "policy", "unknown"]
Severity = Literal["critical", "high", "medium", "low", "info"]


class Decision(str, Enum):
    """A human's answer. Nothing commits without one."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a claim came from, precisely enough to go and look."""

    document_id: str
    document_name: str
    locator: str  # human readable: "clause 7.2", "page 3", "line 42"
    start: int
    end: int
    quote: str

    def verify(self, source_text: str) -> bool:
        """Does this span actually say what the claim says it says?

        The load-bearing check. A model that invents a citation fails here, and
        the claim never reaches the register.
        """
        if self.start < 0 or self.end > len(source_text) or self.start >= self.end:
            return False
        return _normalise(source_text[self.start : self.end]) == _normalise(self.quote)

    def cite(self) -> str:
        return f"{self.document_name} ({self.locator})"


def _normalise(text: str) -> str:
    """Whitespace-insensitive comparison.

    Extractors reflow text; a citation should not fail because a line break
    moved. Everything else must match exactly.
    """
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class Claim:
    """One assertion about the world, taken from one document."""

    id: str
    subject: str  # a key from the active domain pack
    value: str | Decimal | date | None
    unit: str
    provenance: Provenance
    extractor: str  # "rule:payment_terms" or "model:gpt" — always attributable
    effective_from: date | None = None
    confidence: float = 1.0

    @property
    def is_unknown(self) -> bool:
        return self.value is None

    def rendered(self) -> str:
        if self.value is None:
            return "not stated"
        if isinstance(self.value, Decimal):
            # "currency" and "text" are internal unit tags, not words a reader
            # should ever see in a register cell.
            suffix = f" {self.unit}" if self.unit in ("days", "months", "percent") else ""
            return f"{self.value:,}{suffix}"
        return str(self.value)

    def digest(self) -> str:
        payload = json.dumps(
            {"subject": self.subject, "value": str(self.value), "unit": self.unit},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(slots=True)
class Conflict:
    """Two or more documents that do not agree.

    Never resolved automatically. The brief is explicit that a contradiction is
    surfaced, not silently reconciled, and that is also the honest behaviour:
    which document wins is a commercial judgement, not a parsing one.
    """

    id: str
    subject: str
    claims: list[Claim]
    kind: Literal["value_mismatch", "supersession_unclear", "stated_vs_derived"]
    explanation: str
    decision: Decision = Decision.PENDING
    chosen_claim_id: str | None = None
    decided_by: str | None = None
    note: str | None = None

    @property
    def is_open(self) -> bool:
        return self.decision is Decision.PENDING

    def summary(self) -> str:
        values = " vs ".join(f"{c.rendered()} [{c.provenance.cite()}]" for c in self.claims)
        return f"{self.subject}: {values}"


@dataclass(slots=True)
class Finding:
    """A rule the corpus does not satisfy."""

    id: str
    rule_id: str
    severity: Severity
    subject: str
    message: str
    citations: list[Provenance] = field(default_factory=list)
    decision: Decision = Decision.PENDING
    note: str | None = None

    @property
    def is_open(self) -> bool:
        return self.decision is Decision.PENDING


@dataclass(slots=True)
class RegisterRow:
    """One line of the deliverable."""

    subject: str
    label: str
    value: str
    citations: list[Provenance] = field(default_factory=list)
    conflict_id: str | None = None
    note: str | None = None

    def render_html(self) -> str:
        """Deterministic HTML for this row.

        Byte stability matters: an unchanged row must render identically on
        every run, because that is what "only what moved changed" is checked
        against.
        """
        import html as html_lib

        cites = "; ".join(html_lib.escape(p.cite()) for p in self.citations) or "not stated"
        flag = ' <span class="conflict">conflict</span>' if self.conflict_id else ""
        note = f'<div class="note">{html_lib.escape(self.note)}</div>' if self.note else ""
        return (
            f'<tr data-subject="{html_lib.escape(self.subject)}">'
            f"<td>{html_lib.escape(self.label)}</td>"
            f"<td>{html_lib.escape(self.value)}{flag}{note}</td>"
            f'<td class="cite">{cites}</td>'
            "</tr>"
        )

    def digest(self) -> str:
        return hashlib.sha256(self.render_html().encode()).hexdigest()[:16]


@dataclass(slots=True)
class SourceDocument:
    """A document in the pile, after ingestion."""

    id: str
    name: str
    kind: DocumentKind
    text: str
    sha256: str
    ingested_at: str
    page_count: int = 0
    kind_reason: str = ""

    @property
    def short(self) -> str:
        return f"{self.name} [{self.kind}]"


@dataclass(slots=True)
class StageRecord:
    """What one stage of the run decided, and what it cost."""

    name: str
    status: Literal["ok", "retried", "skipped", "escalated", "failed"]
    seconds: float
    detail: str = ""
    attempts: int = 1
    model_calls: int = 0
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RunState:
    """Everything a run needs to survive being killed and started again."""

    run_id: str
    corpus_id: str
    revision: int = 0
    stage: str = "created"
    documents: list[SourceDocument] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    rows: list[RegisterRow] = field(default_factory=list)
    stages: list[StageRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def open_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.is_open]

    @property
    def open_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.is_open]

    @property
    def total_seconds(self) -> float:
        return round(sum(s.seconds for s in self.stages), 3)

    @property
    def model_calls(self) -> int:
        return sum(s.model_calls for s in self.stages)

    def cost_report(self) -> dict[str, Any]:
        """What this run spent and where the time went, stage by stage."""
        return {
            "run_id": self.run_id,
            "total_seconds": self.total_seconds,
            "model_calls": self.model_calls,
            "tokens": sum(s.tokens for s in self.stages),
            "stages": [
                {
                    "name": s.name,
                    "status": s.status,
                    "seconds": s.seconds,
                    "attempts": s.attempts,
                    "model_calls": s.model_calls,
                }
                for s in self.stages
            ],
        }
