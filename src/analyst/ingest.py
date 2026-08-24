"""Getting documents into the system.

Mixed formats in, one plain-text representation out, with a stable id and a
content hash. The hash is what makes a re-run cheap: a document whose bytes have
not changed is not re-read, not re-classified and not re-extracted.

Classification is deliberately rule-first. A model is a poor tool for deciding
whether a file is an invoice when the word "Invoice" is in 40 point type at the
top, and a rule that says so can explain itself, which a reviewer needs.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import DocumentKind, SourceDocument

log = logging.getLogger(__name__)

SUPPORTED = {".txt", ".md", ".docx", ".pdf", ".html", ".htm"}


class UnsupportedDocument(ValueError):
    """The file is not a format this system reads."""


@dataclass(frozen=True, slots=True)
class KindRule:
    kind: DocumentKind
    patterns: tuple[str, ...]
    weight: int = 1


# Ordered most specific first. An amendment usually also says "agreement", so it
# has to be tested before the master agreement rule can claim it.
KIND_RULES: tuple[KindRule, ...] = (
    KindRule("amendment", (r"\bamendment\b", r"\baddend(um|a)\b", r"\bvariation\s+order\b",
                           r"\bchange\s+order\b"), weight=3),
    KindRule("invoice", (r"\binvoice\b", r"\bremittance\b", r"\bamount\s+due\b",
                         r"\binvoice\s+number\b"), weight=3),
    KindRule("sow", (r"\bstatement\s+of\s+work\b", r"\bSOW\b", r"\bwork\s+order\b"), weight=3),
    KindRule("policy", (r"\bpolicy\b", r"\bplaybook\b", r"\bstandard\s+operating\b"), weight=2),
    KindRule("msa", (r"\bmaster\s+(services?|service)\s+agreement\b", r"\bMSA\b",
                     r"\bthis\s+agreement\b", r"\bservices?\s+agreement\b"), weight=2),
)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_id(name: str, digest: str) -> str:
    """Stable across runs for the same content and filename."""
    return hashlib.sha256(f"{name}:{digest}".encode()).hexdigest()[:16]


def read_text(path: pathlib.Path) -> tuple[str, int]:
    """Extract plain text and a page count.

    Offsets into the returned string are what every citation is anchored to, so
    this function is the definition of "where in the document". It must stay
    deterministic: the same bytes must always produce the same string, or every
    stored citation silently rots.
    """
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise UnsupportedDocument(
            f"{path.name}: {suffix or 'no extension'} is not supported. "
            f"Supported: {', '.join(sorted(SUPPORTED))}"
        )

    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace"), 1

    if suffix in (".html", ".htm"):
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"[ \t]{2,}", " ", text), 1

    if suffix == ".docx":
        try:
            import docx  # python-docx
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise UnsupportedDocument(
                "reading .docx needs python-docx, install it with pip install python-docx"
            ) from exc
        document = docx.Document(str(path))
        blocks = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                blocks.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(blocks), 1

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise UnsupportedDocument(
                "reading .pdf needs pypdf, install it with pip install pypdf"
            ) from exc
        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n\n".join(pages), len(pages)

    raise UnsupportedDocument(f"{path.name}: unhandled format")


def classify(text: str, filename: str = "") -> tuple[DocumentKind, str]:
    """Decide what a document is, and be able to say why.

    Returns the kind and the evidence for it. The reason is not decoration: a
    misclassified document sends the whole extraction down the wrong path, and
    a reviewer needs to see the sentence that caused it.
    """
    haystack = f"{filename}\n{text[:4000]}".lower()
    scores: dict[DocumentKind, tuple[int, str]] = {}

    for rule in KIND_RULES:
        for pattern in rule.patterns:
            match = re.search(pattern, haystack, re.I)
            if match:
                score = rule.weight
                # A hit in the filename or the first 200 characters is stronger
                # evidence than the word appearing somewhere in the body.
                if match.start() < len(filename) + 200:
                    score += 2
                previous = scores.get(rule.kind, (0, ""))
                if score > previous[0]:
                    scores[rule.kind] = (score, match.group(0))
                break

    if not scores:
        return "unknown", "no classifying phrase found"

    kind = max(scores, key=lambda k: scores[k][0])
    score, evidence = scores[kind]
    return kind, f"matched {evidence!r} (score {score})"


def ingest_file(path: pathlib.Path) -> SourceDocument:
    """Read one file into a :class:`SourceDocument`."""
    data = path.read_bytes()
    digest = sha256_of(data)
    text, pages = read_text(path)
    kind, reason = classify(text, path.name)
    return SourceDocument(
        id=document_id(path.name, digest),
        name=path.name,
        kind=kind,
        text=text,
        sha256=digest,
        ingested_at=datetime.now(UTC).isoformat(timespec="seconds"),
        page_count=pages,
        kind_reason=reason,
    )


def ingest_directory(
    folder: pathlib.Path, *, known_hashes: set[str] | None = None
) -> tuple[list[SourceDocument], list[str]]:
    """Read every supported file in a folder.

    Returns the documents and a list of problems. A folder with one unreadable
    file still produces a register from the rest, with the failure reported,
    rather than failing the whole run. Documents whose hash is already known are
    skipped, which is what keeps an update costing like an update.
    """
    known = known_hashes or set()
    documents: list[SourceDocument] = []
    problems: list[str] = []

    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in SUPPORTED:
            continue
        try:
            digest = sha256_of(path.read_bytes())
            if digest in known:
                log.info("skipping unchanged document %s", path.name)
                continue
            documents.append(ingest_file(path))
        except UnsupportedDocument as exc:
            problems.append(str(exc))
        except OSError as exc:
            problems.append(f"{path.name}: could not be read ({exc})")
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the pile
            problems.append(f"{path.name}: failed to parse ({type(exc).__name__}: {exc})")

    return documents, problems


def locator_for(text: str, position: int) -> str:
    """A human readable pointer to a character offset.

    Prefers a clause number when the document has them, because "clause 7.2" is
    what a reviewer will search for, and falls back to a line number.
    """
    before = text[:position]
    clause = None
    for match in re.finditer(r"(?m)^\s*(\d+(?:\.\d+)*)\s+[A-Z(]", before):
        clause = match.group(1)
    if clause:
        return f"clause {clause}"
    return f"line {before.count(chr(10)) + 1}"
