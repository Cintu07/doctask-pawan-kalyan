"""Building the deliverable, and updating it without disturbing it.

The register is a table of commercial terms where every row cites the documents
it came from. Two properties matter and both are enforced here.

**Every row traces to a source.** A row with no citation cannot say a value; it
says "not stated". There is no path in this module that produces a figure
without a document behind it.

**A row that did not change renders identically.** Rows are hashed on their
rendered HTML, so an update can prove which rows it touched and, more usefully,
prove which ones it did not.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .conflicts import agreed_claim
from .extract import SUBJECT_BY_KEY, SUBJECTS
from .models import Claim, Conflict, RegisterRow, SourceDocument


class RegisterDrift(AssertionError):
    """A row promised untouched did not render identically."""


def build_rows(
    claims: list[Claim], conflicts: list[Conflict], subjects=SUBJECTS
) -> list[RegisterRow]:
    """One row per tracked subject, in a fixed order.

    Fixed order is deliberate. If rows moved around as claims appeared, every
    run would produce a different document and "only what moved changed" would
    be meaningless.
    """
    rows: list[RegisterRow] = []
    for subject in subjects:
        claim, conflict_id = agreed_claim(subject.key, claims, conflicts)
        conflict = next((c for c in conflicts if c.id == conflict_id), None) if conflict_id else None

        if claim is not None:
            rows.append(
                RegisterRow(
                    subject=subject.key,
                    label=subject.label,
                    value=_format(claim, subject.unit),
                    citations=[claim.provenance],
                    conflict_id=conflict_id,
                    note=(
                        f"settled by {conflict.decided_by}" if conflict and conflict.decided_by
                        else None
                    ),
                )
            )
        elif conflict is not None:
            # Sources disagree and nobody has settled it. Show both, commit to
            # neither. This is the register telling the truth about its inputs.
            rows.append(
                RegisterRow(
                    subject=subject.key,
                    label=subject.label,
                    value="disputed: " + " vs ".join(
                        sorted(c.rendered() for c in conflict.claims)
                    ),
                    citations=[c.provenance for c in conflict.claims],
                    conflict_id=conflict.id,
                    note=conflict.explanation,
                )
            )
        else:
            rows.append(
                RegisterRow(
                    subject=subject.key,
                    label=subject.label,
                    value="not stated",
                    citations=[],
                )
            )
    return rows


def _format(claim: Claim, unit: str) -> str:
    if claim.value is None:
        return "not stated"
    if unit == "days":
        return f"{claim.value} days"
    if unit == "currency":
        return f"{claim.value:,}"
    return str(claim.value)


# --------------------------------------------------------------- incremental


@dataclass(slots=True)
class RowChange:
    subject: str
    action: str  # "added", "changed", "removed", "kept"
    before: RegisterRow | None
    after: RegisterRow | None
    because: list[str] = field(default_factory=list)  # document names responsible


@dataclass(slots=True)
class RegisterDiff:
    changes: list[RowChange] = field(default_factory=list)

    @property
    def touched(self) -> list[RowChange]:
        return [c for c in self.changes if c.action != "kept"]

    @property
    def kept(self) -> list[RowChange]:
        return [c for c in self.changes if c.action == "kept"]

    @property
    def is_noop(self) -> bool:
        return not self.touched

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.action] = counts.get(change.action, 0) + 1
        parts = [f"{n} {action}" for action, n in sorted(counts.items())]
        return ", ".join(parts) or "nothing"


def diff_rows(
    before: list[RegisterRow], after: list[RegisterRow], new_documents: list[SourceDocument]
) -> RegisterDiff:
    """What an update actually did, row by row.

    ``because`` names the newly arrived documents a changed row now cites, which
    is how the system answers "what changed, when, and because of which source".
    """
    old = {row.subject: row for row in before}
    new = {row.subject: row for row in after}
    new_names = {d.name for d in new_documents}
    diff = RegisterDiff()

    for subject in sorted(set(old) | set(new)):
        was, now = old.get(subject), new.get(subject)
        if was is None:
            action = "added"
        elif now is None:
            action = "removed"
        elif was.digest() == now.digest():
            action = "kept"
        else:
            action = "changed"

        because = []
        if now is not None and action in ("added", "changed"):
            because = sorted({p.document_name for p in now.citations} & new_names)

        diff.changes.append(RowChange(subject, action, was, now, because))

    return diff


def verify_untouched(diff: RegisterDiff) -> None:
    """Prove every kept row is byte identical.

    Runs on every update, not only in tests. If a row marked untouched has
    drifted, the update is refused rather than shipped, because at that point
    the system can no longer tell the reviewer the truth about what it changed.
    """
    drifted = [
        change.subject
        for change in diff.kept
        if change.before is not None
        and change.after is not None
        and change.before.render_html() != change.after.render_html()
    ]
    if drifted:
        raise RegisterDrift(
            "rows reported untouched did not render identically: " + ", ".join(sorted(drifted))
        )


# ------------------------------------------------------------------ rendering

_STYLE = """
body{font-family:Georgia,serif;color:#1a1a1a;line-height:1.5;margin:0}
h1{font-size:22px;color:#1f4e79;margin:0 0 4px}
.meta{font-family:Arial,sans-serif;font-size:11px;color:#666;margin:0 0 18px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;background:#f1f3f4;font-family:Arial,sans-serif;font-size:11px;
   text-transform:uppercase;letter-spacing:.5px;color:#555;padding:7px 8px}
td{padding:7px 8px;border-bottom:1px solid #e6e6e6;vertical-align:top}
td.cite{font-family:Arial,sans-serif;font-size:11px;color:#666;width:32%}
.conflict{background:#fce8e6;color:#a50e0e;font-size:10px;padding:1px 5px;
          border-radius:3px;font-family:Arial,sans-serif;margin-left:6px}
.note{font-family:Arial,sans-serif;font-size:11px;color:#8a6d00;margin-top:3px}
h2{font-size:14px;color:#1f4e79;font-family:Arial,sans-serif;margin:24px 0 6px}
.finding{border-left:3px solid #f9ab00;background:#fef7e0;padding:8px;margin:6px 0;
         font-size:12px}
.finding.critical,.finding.high{border-left-color:#d93025;background:#fce8e6}
.clean{border-left:3px solid #34a853;background:#e6f4ea;padding:8px;font-size:12px}
"""


def render_register(
    *,
    title: str,
    rows: list[RegisterRow],
    documents: list[SourceDocument],
    findings=(),
    conflicts=(),
) -> str:
    """The whole deliverable as one HTML document."""
    import html as html_lib

    parts = [
        f"<style>{_STYLE}</style>",
        f"<h1>{html_lib.escape(title)}</h1>",
        f'<p class="meta">{len(documents)} source document(s): '
        + html_lib.escape(", ".join(sorted(d.name for d in documents)))
        + "</p>",
        "<table><thead><tr><th>Term</th><th>Position</th><th>Source</th></tr></thead><tbody>",
    ]
    parts += [row.render_html() for row in rows]
    parts.append("</tbody></table>")

    parts.append("<h2>Compliance findings</h2>")
    if findings:
        for finding in findings:
            cites = "; ".join(html_lib.escape(p.cite()) for p in finding.citations)
            parts.append(
                f'<div class="finding {finding.severity}">'
                f"<b>{html_lib.escape(finding.severity.upper())}</b> "
                f"{html_lib.escape(finding.message)}"
                + (f'<div class="note">{cites}</div>' if cites else "")
                + "</div>"
            )
    else:
        # The rarest output in this industry, and worth stating plainly.
        parts.append(
            '<div class="clean">No findings. Every rule in the playbook was checked '
            "against the sources and none was breached.</div>"
        )

    if conflicts:
        parts.append("<h2>Unresolved conflicts</h2>")
        for conflict in conflicts:
            parts.append(
                f'<div class="finding">{html_lib.escape(conflict.summary())}'
                f'<div class="note">{html_lib.escape(conflict.explanation)}</div></div>'
            )

    return "".join(parts)


def register_digest(rows: list[RegisterRow]) -> str:
    joined = "".join(row.render_html() for row in rows)
    return hashlib.sha256(joined.encode()).hexdigest()[:16]
