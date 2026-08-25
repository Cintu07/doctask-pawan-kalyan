"""The operations a user or a machine can perform.

One class, so the HTTP API and the MCP server are both thin shells over the same
code and cannot drift apart. That matters for the brief's fourth requirement:
another program has to be able to drive the whole flow, approval included, and
the only honest way to guarantee that is for the machine path and the human path
to be the same path.

Nothing here publishes without a decision. A run produces a *proposal*; the
register on disk only moves when someone accepts it.
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from .graph import Pipeline
from .models import Decision, RunState, StageRecord
from .playbook import Rule, load_playbook
from .register import (
    RegisterDiff,
    build_rows,
    diff_rows,
    register_digest,
    render_register,
    verify_untouched,
)
from .store import LeaseHeld, Store

log = logging.getLogger(__name__)

__all__ = ["Analyst", "Proposal", "LeaseHeld"]


@dataclass(slots=True)
class Proposal:
    """What a run wants to do, before anyone has agreed to it."""

    run_id: str
    corpus_id: str
    revision: int
    state: RunState
    diff: RegisterDiff
    published: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.diff.touched) or bool(self.state.open_conflicts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "corpus_id": self.corpus_id,
            "revision": self.revision,
            "published": self.published,
            "summary": self.diff.summary(),
            "rows_changed": [
                {
                    "subject": c.subject,
                    "action": c.action,
                    "before": c.before.value if c.before else None,
                    "after": c.after.value if c.after else None,
                    "because": c.because,
                }
                for c in self.diff.touched
            ],
            "rows_kept": len(self.diff.kept),
            "conflicts": [
                {
                    "id": c.id,
                    "subject": c.subject,
                    "kind": c.kind,
                    "explanation": c.explanation,
                    "decision": c.decision.value,
                    "positions": [
                        {
                            "claim_id": x.id,
                            "value": x.rendered(),
                            "cite": x.provenance.cite(),
                            "quote": x.provenance.quote[:400],
                            "extractor": x.extractor,
                        }
                        for x in c.claims
                    ],
                }
                for c in self.state.conflicts
            ],
            "findings": [
                {
                    "id": f.id,
                    "rule_id": f.rule_id,
                    "severity": f.severity,
                    "message": f.message,
                    "decision": f.decision.value,
                    "cites": [p.cite() for p in f.citations],
                }
                for f in self.state.findings
            ],
            "notes": self.notes,
            "cost": self.state.cost_report(),
        }


class Analyst:
    """Everything the system can be asked to do."""

    def __init__(
        self,
        store: Store | None = None,
        *,
        pipeline: Pipeline | None = None,
        data_dir: pathlib.Path | str = "data",
    ) -> None:
        self.store = store or Store(pathlib.Path(data_dir) / "analyst.db")
        self.pipeline = pipeline or Pipeline(self.store)

    # ------------------------------------------------------------------ runs

    def analyse(
        self,
        *,
        corpus_id: str,
        source_dir: pathlib.Path | str,
        playbook: pathlib.Path | str | None = None,
        incremental: bool | None = None,
        run_id: str | None = None,
        actor: str = "system",
        domain: str | None = None,
    ) -> Proposal:
        """Read the pile and propose a register. Publishes nothing.

        ``incremental`` defaults to "whatever is true": if this corpus has been
        analysed before, only new or changed documents are read, and the
        existing register is the baseline the diff is taken against.
        """
        if domain:
            # Switching industry is a data change: a different pack, same pipeline.
            from .extract import use_domain

            use_domain(domain)
        rules: list[Rule] = load_playbook(playbook) if playbook else []
        previous_revision, previous_rows = self.store.load_register(corpus_id)
        if incremental is None:
            incremental = previous_revision > 0

        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        holder = f"{actor}:{uuid.uuid4().hex[:6]}"

        with self.store.lease(run_id, holder):
            raw = self.pipeline.run(
                corpus_id=corpus_id,
                source_dir=source_dir,
                rules=rules,
                run_id=run_id,
                incremental=incremental,
            )

            state = RunState(
                run_id=run_id,
                corpus_id=corpus_id,
                revision=previous_revision + 1,
                stage="proposed",
                documents=list(raw.get("documents", [])),
                claims=list(raw.get("claims", [])),
                conflicts=list(raw.get("conflicts", [])),
                findings=list(raw.get("findings", [])),
                rows=list(raw.get("rows", [])),
                stages=[s for s in raw.get("stages", []) if isinstance(s, StageRecord)],
                notes=list(raw.get("notes", [])) + list(raw.get("rejected", [])),
            )

            new_documents = [
                d for d in state.documents
                if d.sha256 not in self.store.known_hashes(corpus_id)
            ]
            diff = diff_rows(previous_rows, state.rows, new_documents)
            verify_untouched(diff)  # proves the promise on every run, not just in tests

            self.store.save_run(state)
            self.store.record(
                corpus_id, actor, "analysed",
                because=", ".join(d.name for d in new_documents) or "full run",
                detail=diff.summary(),
            )

        return Proposal(
            run_id=run_id,
            corpus_id=corpus_id,
            revision=state.revision,
            state=state,
            diff=diff,
            notes=state.notes,
        )

    def resume(self, run_id: str) -> dict[str, Any]:
        """Continue a run that was interrupted, from its last checkpoint."""
        stage = self.pipeline.checkpoint_stage(run_id)
        if stage is None:
            raise KeyError(f"no checkpoint for run {run_id}")
        raw = self.pipeline.resume(run_id)
        return {"run_id": run_id, "resumed_at": stage, "stage_count": len(raw.get("stages", []))}

    def pending_stage(self, run_id: str) -> str | None:
        return self.pipeline.checkpoint_stage(run_id)

    # ------------------------------------------------------------ the gate

    def decide(
        self,
        *,
        corpus_id: str,
        run_id: str,
        decisions: dict[str, str],
        actor: str = "reviewer",
        chosen: dict[str, str] | None = None,
        notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Record a person's answer on each conflict and finding, item by item.

        Rejecting one item never discards the others. Every decision is
        attributed and written to the audit trail, because "who accepted this"
        is the first question anyone asks of a register later.
        """
        state = self.store.load_run(run_id)
        if state is None:
            raise KeyError(f"unknown run {run_id}")

        chosen = chosen or {}
        notes = notes or {}
        applied: list[str] = []
        unknown: list[str] = []

        for item_id, verdict in decisions.items():
            try:
                decision = Decision(verdict)
            except ValueError as exc:
                raise ValueError(
                    f"{verdict!r} is not a decision. Use 'accepted' or 'rejected'."
                ) from exc

            conflict = next((c for c in state.conflicts if c.id == item_id), None)
            if conflict is not None:
                conflict.decision = decision
                conflict.decided_by = actor
                conflict.note = notes.get(item_id)
                if decision is Decision.ACCEPTED:
                    claim_id = chosen.get(item_id)
                    if not claim_id:
                        raise ValueError(
                            f"accepting conflict {item_id} needs the claim id that wins; "
                            f"pass it in 'chosen'"
                        )
                    if not any(c.id == claim_id for c in conflict.claims):
                        raise ValueError(
                            f"claim {claim_id} is not one of the positions in conflict {item_id}"
                        )
                    conflict.chosen_claim_id = claim_id
                applied.append(item_id)
                self.store.record(
                    corpus_id, actor, f"conflict {verdict}", subject=conflict.subject,
                    detail=conflict.summary()[:300],
                )
                continue

            finding = next((f for f in state.findings if f.id == item_id), None)
            if finding is not None:
                finding.decision = decision
                finding.note = notes.get(item_id)
                applied.append(item_id)
                self.store.record(
                    corpus_id, actor, f"finding {verdict}", subject=finding.subject,
                    detail=finding.message[:300],
                )
                continue

            unknown.append(item_id)

        # A settled conflict changes what the register shows, so rebuild rows.
        state.rows = build_rows(state.claims, state.conflicts)
        self.store.save_run(state)

        return {
            "run_id": run_id,
            "applied": applied,
            "unknown": unknown,
            "open_conflicts": len(state.open_conflicts),
            "open_findings": len(state.open_findings),
        }

    def publish(
        self, *, corpus_id: str, run_id: str, actor: str = "reviewer", force: bool = False
    ) -> dict[str, Any]:
        """Commit the register, but only once a person has cleared the gate.

        Refuses while conflicts are open. ``force`` is available and is recorded
        in the audit trail as a forced publish, because a system that cannot be
        overridden gets worked around, and an override that leaves no trace is
        worse than one that does.
        """
        state = self.store.load_run(run_id)
        if state is None:
            raise KeyError(f"unknown run {run_id}")

        open_conflicts = state.open_conflicts
        if open_conflicts and not force:
            raise PermissionError(
                f"{len(open_conflicts)} conflict(s) are still open: "
                + ", ".join(c.subject for c in open_conflicts)
                + ". Decide them, or publish with force=true to record an override."
            )

        previous_revision, previous_rows = self.store.load_register(corpus_id)
        diff = diff_rows(previous_rows, state.rows, state.documents)
        verify_untouched(diff)

        self.store.save_register(corpus_id, state.revision, state.rows)
        self.store.remember_documents(corpus_id, state.documents)
        state.stage = "published"
        self.store.save_run(state)
        self.store.record(
            corpus_id, actor, "published forced" if force and open_conflicts else "published",
            detail=f"revision {state.revision}, {diff.summary()}",
        )

        return {
            "corpus_id": corpus_id,
            "revision": state.revision,
            "digest": register_digest(state.rows),
            "changed": [c.subject for c in diff.touched],
            "unchanged": len(diff.kept),
            "forced": bool(force and open_conflicts),
        }

    # ---------------------------------------------------------------- output

    def register_html(self, corpus_id: str, *, title: str | None = None) -> str:
        revision, rows = self.store.load_register(corpus_id)
        if not rows:
            raise KeyError(f"no published register for {corpus_id}")
        documents = self.store.load_documents(corpus_id)
        latest = self.store.latest_run(corpus_id)
        return render_register(
            title=title or f"{corpus_id}: commercial terms register (revision {revision})",
            rows=rows,
            documents=documents,
            findings=[f for f in (latest.findings if latest else []) if f.severity != "info"],
            conflicts=[c for c in (latest.conflicts if latest else []) if c.is_open],
        )

    def history(self, corpus_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """What changed, when, and because of which source."""
        return self.store.history(corpus_id, limit)

    def status(self, corpus_id: str) -> dict[str, Any]:
        revision, rows = self.store.load_register(corpus_id)
        latest = self.store.latest_run(corpus_id)
        return {
            "corpus_id": corpus_id,
            "revision": revision,
            "rows": len(rows),
            "documents": len(self.store.load_documents(corpus_id)),
            "latest_run": latest.run_id if latest else None,
            "stage": latest.stage if latest else None,
            "open_conflicts": len(latest.open_conflicts) if latest else 0,
            "open_findings": len(latest.open_findings) if latest else 0,
        }
