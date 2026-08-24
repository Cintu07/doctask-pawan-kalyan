"""Checking the corpus against the rules a user actually cares about.

Rules are data, not code. A new court, client, threshold or obligation is a JSON
edit, which is the difference between a tool a team can own and one they have to
call an engineer about.

A rule that cannot be evaluated says so. It never passes by default, because a
compliance report that silently skips the check it could not run is worse than
one that admits it.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from .models import Claim, Finding, Provenance, Severity

Comparator = Literal["lte", "gte", "eq", "neq", "present", "absent", "one_of"]


class PlaybookError(ValueError):
    """The playbook itself is malformed."""


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    subject: str
    comparator: Comparator
    value: Any
    severity: Severity
    message: str
    stage: int = 1  # rules are evaluated in stages, cheap and structural first

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Rule":
        missing = {"id", "subject", "comparator", "message"} - set(raw)
        if missing:
            raise PlaybookError(f"rule is missing {', '.join(sorted(missing))}: {raw!r}")
        comparator = raw["comparator"]
        if comparator not in (
            "lte", "gte", "eq", "neq", "present", "absent", "one_of",
        ):
            raise PlaybookError(f"rule {raw['id']}: unknown comparator {comparator!r}")
        if comparator not in ("present", "absent") and "value" not in raw:
            raise PlaybookError(f"rule {raw['id']}: comparator {comparator} needs a value")
        return cls(
            id=raw["id"],
            subject=raw["subject"],
            comparator=comparator,
            value=raw.get("value"),
            severity=raw.get("severity", "medium"),
            message=raw["message"],
            stage=int(raw.get("stage", 1)),
        )


def load_playbook(path: pathlib.Path | str) -> list[Rule]:
    """Read a playbook file, failing loudly on a bad rule."""
    path = pathlib.Path(path)
    if not path.exists():
        raise PlaybookError(
            f"no playbook at {path}. Point --playbook at a JSON file, or use the "
            f"one in playbooks/ as a starting point."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlaybookError(f"{path} is not valid JSON: {exc}") from exc

    rules = raw.get("rules") if isinstance(raw, dict) else raw
    if not isinstance(rules, list):
        raise PlaybookError(f"{path}: expected a list of rules, or an object with 'rules'")
    return [Rule.from_dict(item) for item in rules]


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - any unparseable value is simply not numeric
        return None


def _passes(rule: Rule, claim: Claim | None) -> tuple[bool | None, str]:
    """Evaluate one rule. ``None`` means the rule could not be evaluated."""
    if rule.comparator == "absent":
        return (claim is None or claim.is_unknown), "term is absent"
    if rule.comparator == "present":
        return (claim is not None and not claim.is_unknown), "term is present"

    if claim is None or claim.is_unknown:
        # Not a pass and not a fail: the sources do not say.
        return None, "the sources do not state this term, so the rule could not be checked"

    if rule.comparator == "one_of":
        options = [str(v).strip().lower() for v in (rule.value or [])]
        return str(claim.value).strip().lower() in options, f"value is {claim.rendered()}"

    if rule.comparator == "eq":
        return str(claim.value).strip().lower() == str(rule.value).strip().lower(), ""
    if rule.comparator == "neq":
        return str(claim.value).strip().lower() != str(rule.value).strip().lower(), ""

    left, right = _as_decimal(claim.value), _as_decimal(rule.value)
    if left is None or right is None:
        return None, f"cannot compare {claim.rendered()!r} numerically"
    if rule.comparator == "lte":
        return left <= right, f"{left} vs limit {right}"
    return left >= right, f"{left} vs minimum {right}"


def evaluate(
    rules: list[Rule], claims: list[Claim]
) -> tuple[list[Finding], list[str]]:
    """Run the playbook in stages.

    Returns findings and a list of rules that could not be evaluated. An empty
    findings list on a corpus that was fully checked is a real result and the
    register says so in those words.
    """
    by_subject: dict[str, Claim] = {}
    for claim in claims:
        if not claim.is_unknown and claim.subject not in by_subject:
            by_subject[claim.subject] = claim

    findings: list[Finding] = []
    unevaluated: list[str] = []

    for rule in sorted(rules, key=lambda r: (r.stage, r.id)):
        claim = by_subject.get(rule.subject)
        result, detail = _passes(rule, claim)

        if result is None:
            unevaluated.append(f"{rule.id}: {detail}")
            findings.append(
                Finding(
                    id=f"fnd_{uuid.uuid4().hex[:12]}",
                    rule_id=rule.id,
                    severity="info",
                    subject=rule.subject,
                    message=f"Could not check {rule.id}: {detail}",
                    citations=[],
                )
            )
            continue

        if result:
            continue

        citations: list[Provenance] = [claim.provenance] if claim else []
        findings.append(
            Finding(
                id=f"fnd_{uuid.uuid4().hex[:12]}",
                rule_id=rule.id,
                severity=rule.severity,
                subject=rule.subject,
                message=rule.message + (f" ({detail})" if detail else ""),
                citations=citations,
            )
        )

    return findings, unevaluated
