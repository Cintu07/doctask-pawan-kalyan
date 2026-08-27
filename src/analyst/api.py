"""HTTP surface.

A thin shell over :class:`analyst.service.Analyst`. Approval is an ordinary
endpoint here, not a special human-only path, which is what lets another program
drive the whole flow end to end without touching a browser.

Errors carry a cause and a fix. A reviewer reading a 409 in a sidebar should not
have to open the logs to find out what to do next.
"""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .domain import DomainError, available_domains
from .ingest import UnsupportedDocument
from .playbook import PlaybookError
from .register import RegisterDrift
from .service import Analyst, LeaseHeld

log = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.environ.get("ANALYST_DATA", "data"))
CORPUS_ROOT = pathlib.Path(os.environ.get("ANALYST_CORPUS_ROOT", "corpus")).resolve()

app = FastAPI(
    title="The analyst that never sleeps",
    version="0.1.0",
    summary="Reads a pile of contract documents, builds a cited register, checks it "
    "against a playbook, and keeps it current.",
)

analyst = Analyst(data_dir=DATA_DIR)


class AnalyseRequest(BaseModel):
    corpus_id: str = Field(..., description="Stable name for this pile of documents")
    source_dir: str = Field(..., description="Folder to read, relative to the corpus root")
    playbook: str | None = Field(None, description="Path to a rules file")
    incremental: bool | None = Field(
        None, description="Omit to decide automatically from whether this corpus is known"
    )
    domain: str | None = Field(None, description="industry pack, e.g. land-title")
    actor: str = "api"


class DecideRequest(BaseModel):
    run_id: str
    decisions: dict[str, str] = Field(..., description="item id -> accepted | rejected")
    chosen: dict[str, str] = Field(
        default_factory=dict, description="conflict id -> the claim id that wins"
    )
    notes: dict[str, str] = Field(default_factory=dict)
    actor: str = "api"


class PublishRequest(BaseModel):
    run_id: str
    actor: str = "api"
    force: bool = False


def _safe_source(raw: str) -> pathlib.Path:
    """Keep source folders inside the configured root.

    The corpus path arrives over HTTP, so without this an attacker could walk
    the filesystem by asking the analyst to read it.
    """
    asked = pathlib.Path(raw)
    candidate = asked.resolve() if asked.is_absolute() else (CORPUS_ROOT / raw).resolve()
    if not str(candidate).startswith(str(CORPUS_ROOT)):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "source_dir is outside the corpus root",
                "fix": f"put the documents under {CORPUS_ROOT}, or set ANALYST_CORPUS_ROOT",
            },
        )
    if not candidate.is_dir():
        raise HTTPException(
            status_code=404,
            detail={"error": f"no folder at {candidate}", "fix": "check the path"},
        )
    return candidate


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# The review interface, served from the built bundle committed in static/.
# Mounting it here means a stranger with no node installed still gets the ui,
# which is the whole point of committing the build output.
_STATIC = pathlib.Path(__file__).parent / "static"
if _STATIC.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/ui", StaticFiles(directory=_STATIC, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def root() -> Response:
        return Response(status_code=307, headers={"Location": "/ui/"})


@app.post("/analyse")
def analyse(request: AnalyseRequest) -> dict[str, Any]:
    """Read the pile and propose a register. Publishes nothing."""
    source = _safe_source(request.source_dir)
    try:
        proposal = analyst.analyse(
            corpus_id=request.corpus_id,
            source_dir=source,
            playbook=request.playbook,
            incremental=request.incremental,
            actor=request.actor,
            domain=request.domain,
        )
    except DomainError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc
    except PlaybookError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc
    except UnsupportedDocument as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc
    except LeaseHeld as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
    except RegisterDrift as exc:
        # Refuse to show a proposal we cannot vouch for.
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal consistency check failed",
                "cause": str(exc),
                "fix": "this is a bug, nothing was published",
            },
        ) from exc
    return proposal.to_dict()


@app.post("/decide")
def decide(request: DecideRequest) -> dict[str, Any]:
    """Record a human decision on each conflict or finding, one at a time."""
    try:
        return analyst.decide(
            corpus_id=analyst.store.load_run(request.run_id).corpus_id,  # type: ignore[union-attr]
            run_id=request.run_id,
            decisions=request.decisions,
            chosen=request.chosen,
            notes=request.notes,
            actor=request.actor,
        )
    except AttributeError as exc:
        raise HTTPException(status_code=404, detail={"error": f"unknown run {request.run_id}"}) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)}) from exc


@app.post("/publish")
def publish(request: PublishRequest) -> dict[str, Any]:
    """Commit the register. Refuses while conflicts are open."""
    state = analyst.store.load_run(request.run_id)
    if state is None:
        raise HTTPException(status_code=404, detail={"error": f"unknown run {request.run_id}"})
    try:
        return analyst.publish(
            corpus_id=state.corpus_id, run_id=request.run_id,
            actor=request.actor, force=request.force,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(exc),
                "fix": "POST /decide for each open conflict, then publish again",
            },
        ) from exc


@app.post("/resume")
def resume(run_id: str = Body(..., embed=True)) -> dict[str, Any]:
    """Continue a run that was interrupted, from its last checkpoint."""
    try:
        return analyst.resume(run_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": str(exc), "fix": "check the run id with GET /runs"},
        ) from exc


@app.get("/domains")
def domains() -> dict[str, Any]:
    """The industry packs available. Adding one is a json file, not a deploy."""
    from .domain import load_domain

    return {
        "domains": [
            {
                "name": n,
                "description": load_domain(n).description,
                "subjects": len(load_domain(n).subjects),
            }
            for n in available_domains()
        ]
    }


@app.get("/runs")
def runs(corpus_id: str | None = None) -> dict[str, Any]:
    return {"runs": analyst.store.list_runs(corpus_id)}


@app.get("/corpora/{corpus_id}/status")
def status(corpus_id: str) -> dict[str, Any]:
    return analyst.status(corpus_id)


@app.get("/corpora/{corpus_id}/history")
def history(corpus_id: str, limit: int = 100) -> dict[str, Any]:
    """What changed, when, and because of which source."""
    return {"corpus_id": corpus_id, "events": analyst.history(corpus_id, limit)}


@app.get("/corpora/{corpus_id}/register", response_class=HTMLResponse)
def register(corpus_id: str) -> HTMLResponse:
    try:
        return HTMLResponse(analyst.register_html(corpus_id))
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": str(exc), "fix": "run /analyse then /publish first"},
        ) from exc


@app.get("/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    """Everything a reviewer needs about one run, including what it cost."""
    state = analyst.store.load_run(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail={"error": f"unknown run {run_id}"})
    return {
        "run_id": state.run_id,
        "corpus_id": state.corpus_id,
        "revision": state.revision,
        "stage": state.stage,
        "next_checkpoint": analyst.pending_stage(run_id),
        "documents": [
            {"name": d.name, "kind": d.kind, "why": d.kind_reason, "sha256": d.sha256[:12]}
            for d in state.documents
        ],
        "rows": [
            {"subject": r.subject, "label": r.label, "value": r.value,
             "cites": [p.cite() for p in r.citations], "conflict": r.conflict_id}
            for r in state.rows
        ],
        "conflicts": [
            {"id": c.id, "subject": c.subject, "kind": c.kind, "decision": c.decision.value,
             "explanation": c.explanation,
             "positions": [
                 {"claim_id": x.id, "value": x.rendered(), "cite": x.provenance.cite(),
                  "quote": x.provenance.quote[:300]}
                 for x in c.claims
             ]}
            for c in state.conflicts
        ],
        "findings": [
            {"id": f.id, "rule_id": f.rule_id, "severity": f.severity, "message": f.message,
             "decision": f.decision.value, "cites": [p.cite() for p in f.citations]}
            for f in state.findings
        ],
        "notes": state.notes,
        "cost": state.cost_report(),
    }
