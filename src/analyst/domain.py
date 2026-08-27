"""What the system knows about an industry, as data.

Everything that makes this system "about vendor contracts" lives in one json
file: which document kinds exist and how to recognise them, which terms are
tracked, which kinds may state which term, and the patterns that find them.

Pointing it at a different industry is a new file, not a new pipeline. That is
the difference between a tool a team can extend and one that needs an engineer
every time the world changes, and it is why `domains/land-title.json` exists
next to `domains/vendor-contracts.json` with no code shared beyond this loader.

A malformed pack fails at load with the offending rule quoted. A domain that
half-loads would produce a register that is quietly missing terms, which is
exactly the silent wrongness this system is built to avoid.
"""

from __future__ import annotations

import json
import pathlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

CastName = Literal["int", "money", "text", "date"]

DOMAIN_DIR = pathlib.Path(__file__).parent.parent.parent / "domains"
DEFAULT_DOMAIN = "vendor-contracts"


class DomainError(ValueError):
    """The domain pack is malformed."""


@dataclass(frozen=True, slots=True)
class Subject:
    """One thing the register tracks."""

    key: str
    label: str
    unit: str
    question: str  # what a model is asked, when the rules find nothing
    hints: tuple[str, ...] = ()  # words the offline extractor looks near


@dataclass(frozen=True, slots=True)
class KindRule:
    """How to recognise one kind of document."""

    kind: str
    patterns: tuple[str, ...]
    weight: int = 1


@dataclass(frozen=True, slots=True)
class ExtractionRule:
    """Patterns that find one subject, tried in order."""

    subject: str
    unit: str
    cast: CastName
    patterns: tuple[str, ...]


@dataclass(slots=True)
class Domain:
    """A whole industry's worth of knowledge, loaded from one file."""

    name: str
    description: str
    kinds: tuple[KindRule, ...]
    subjects: tuple[Subject, ...]
    rules: tuple[ExtractionRule, ...]
    relevant: dict[str, frozenset[str]] = field(default_factory=dict)
    per_document_subjects: frozenset[str] = frozenset()

    def subject(self, key: str) -> Subject | None:
        return next((s for s in self.subjects if s.key == key), None)

    def subjects_for(self, kind: str) -> list[Subject]:
        """The terms a document of this kind is allowed to speak to.

        A document kind that is not listed for a subject cannot contribute it.
        Without this an invoice contributes a liability cap and a statement of
        work contributes an invoice total, and the register reports disputes
        that do not exist.
        """
        return [s for s in self.subjects if kind in self.relevant.get(s.key, frozenset())]

    def rules_for(self, kind: str) -> list[ExtractionRule]:
        allowed = {s.key for s in self.subjects_for(kind)}
        return [r for r in self.rules if r.subject in allowed]


# ------------------------------------------------------------------ casting


def cast_int(text: str, words: dict[str, int]) -> int | None:
    """Read a count out of a captured phrase.

    The capture is usually wider than the number, because a pattern that finds
    "sixty (60) days notice" has to tolerate the words between and ends up
    holding "either party gives sixty". So look for the number inside the
    phrase, longest spelling first so "forty five" is not read as "five".
    """
    digits = re.search(r"\d+", text.replace(",", ""))
    if digits:
        return int(digits.group(0))
    flat = " ".join(text.strip().lower().split())
    for spelling in sorted(words, key=len, reverse=True):
        if re.search(rf"\b{re.escape(spelling)}\b", flat):
            return words[spelling]
    return None


def cast_money(text: str) -> Decimal | None:
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def cast_text(text: str) -> str | None:
    return text.strip().rstrip(".,;") or None


# ------------------------------------------------------------------ loading


def _require(raw: Any, keys: set[str], what: str) -> None:
    if not isinstance(raw, dict):
        raise DomainError(f"{what} must be an object, got {type(raw).__name__}: {raw!r}")
    missing = keys - set(raw)
    if missing:
        raise DomainError(f"{what} is missing {', '.join(sorted(missing))}: {raw!r}")


def load_domain(name_or_path: str | pathlib.Path = DEFAULT_DOMAIN) -> Domain:
    """Read a domain pack, failing loudly on anything malformed."""
    path = pathlib.Path(name_or_path)
    if not path.suffix:
        path = DOMAIN_DIR / f"{path.name}.json"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in DOMAIN_DIR.glob("*.json"))) or "none found"
        raise DomainError(f"no domain pack at {path}. available: {available}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DomainError(f"{path} is not valid json: {exc}") from exc

    _require(raw, {"name", "kinds", "subjects", "rules"}, f"domain {path.name}")

    kinds = []
    for item in raw["kinds"]:
        _require(item, {"kind", "patterns"}, "document kind")
        kinds.append(
            KindRule(
                kind=item["kind"],
                patterns=tuple(item["patterns"]),
                weight=int(item.get("weight", 1)),
            )
        )

    subjects, relevant = [], {}
    for item in raw["subjects"]:
        _require(item, {"key", "label", "unit"}, "subject")
        subjects.append(
            Subject(
                key=item["key"],
                label=item["label"],
                unit=item["unit"],
                question=item.get("question", f"What is the {item['label'].lower()}?"),
                hints=tuple(item.get("hints", [])),
            )
        )
        relevant[item["key"]] = frozenset(item.get("kinds", []))

    known = {s.key for s in subjects}
    rules = []
    for item in raw["rules"]:
        _require(item, {"subject", "patterns"}, "extraction rule")
        if item["subject"] not in known:
            raise DomainError(
                f"rule targets unknown subject {item['subject']!r}. "
                f"known: {', '.join(sorted(known))}"
            )
        cast = item.get("cast", "text")
        if cast not in ("int", "money", "text", "date"):
            raise DomainError(f"rule {item['subject']}: unknown cast {cast!r}")
        for pattern in item["patterns"]:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise DomainError(f"rule {item['subject']}: bad pattern {pattern!r}: {exc}") from exc
            if "v" not in compiled.groupindex:
                raise DomainError(
                    f"rule {item['subject']}: pattern must capture the value as (?P<v>...): {pattern!r}"
                )
        subject_unit = next(s.unit for s in subjects if s.key == item["subject"])
        rules.append(
            ExtractionRule(
                subject=item["subject"],
                unit=subject_unit,
                cast=cast,  # type: ignore[arg-type]
                patterns=tuple(item["patterns"]),
            )
        )

    return Domain(
        name=raw["name"],
        description=raw.get("description", ""),
        kinds=tuple(kinds),
        subjects=tuple(subjects),
        rules=tuple(rules),
        relevant=relevant,
        per_document_subjects=frozenset(raw.get("per_document_subjects", [])),
    )


def available_domains() -> list[str]:
    return sorted(p.stem for p in DOMAIN_DIR.glob("*.json"))


_WORD_NUMBERS_DEFAULT = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "five": 5, "six": 6, "seven": 7,
    "ten": 10, "twelve": 12, "fourteen": 14, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "forty five": 45, "forty-five": 45, "sixty": 60,
    "ninety": 90, "one hundred": 100, "one hundred and twenty": 120,
}


def caster(cast: CastName) -> Callable[[str], Any]:
    if cast == "int":
        return lambda text: cast_int(text, _WORD_NUMBERS_DEFAULT)
    if cast == "money":
        return cast_money
    return cast_text


_ACTIVE_FOR_INGEST: Domain | None = None


def set_active(domain: Domain) -> None:
    """Record the domain in force, so ingest can classify with its kinds."""
    global _ACTIVE_FOR_INGEST
    _ACTIVE_FOR_INGEST = domain


def active_domain_kinds() -> tuple[KindRule, ...]:
    """Document kinds for the domain in force, loading the default if unset."""
    global _ACTIVE_FOR_INGEST
    if _ACTIVE_FOR_INGEST is None:
        _ACTIVE_FOR_INGEST = load_domain()
    return _ACTIVE_FOR_INGEST.kinds
