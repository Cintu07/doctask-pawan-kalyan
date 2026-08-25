"""MCP surface.

The same operations as the HTTP API, over the protocol SuperDocs uses itself.
Approval is a tool like any other, so an agent can run the whole flow, read the
conflicts, decide each one, and publish, without a person clicking anything.

That is deliberate and it is also the sharp edge: a machine can drive this, so
the gate is only as strong as the caller's judgement. The tool descriptions say
so plainly, because an agent reading them should understand that accepting a
conflict is a commercial decision it may not be entitled to make.

Run with:  python -m analyst.mcp_server
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

from mcp.server.mcpserver import MCPServer

from .service import Analyst

DATA_DIR = pathlib.Path(os.environ.get("ANALYST_DATA", "data"))
CORPUS_ROOT = pathlib.Path(os.environ.get("ANALYST_CORPUS_ROOT", "corpus")).resolve()

mcp = MCPServer("analyst", instructions=__doc__)
analyst = Analyst(data_dir=DATA_DIR)


def _source(raw: str) -> pathlib.Path:
    candidate = (
        pathlib.Path(raw).resolve()
        if pathlib.Path(raw).is_absolute()
        else (CORPUS_ROOT / raw).resolve()
    )
    if not str(candidate).startswith(str(CORPUS_ROOT)):
        raise ValueError(f"source_dir must sit under {CORPUS_ROOT}")
    if not candidate.is_dir():
        raise ValueError(f"no folder at {candidate}")
    return candidate


@mcp.tool()
def analyse_corpus(
    corpus_id: str,
    source_dir: str,
    playbook: str | None = None,
    incremental: bool | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """Read a folder of contract documents and propose a register.

    Publishes nothing. Returns the proposed rows, every conflict between sources
    with the exact quotes on each side, the playbook findings, and what the run
    cost. Call decide_item for each conflict, then publish_register.

    On a corpus that has been analysed before, only new or changed documents are
    read and the result is a focused update rather than a rewrite.
    """
    proposal = analyst.analyse(
        corpus_id=corpus_id,
        source_dir=_source(source_dir),
        playbook=playbook,
        incremental=incremental,
        domain=domain,
        actor="mcp",
    )
    return proposal.to_dict()


@mcp.tool()
def decide_item(
    run_id: str,
    item_id: str,
    decision: str,
    chosen_claim_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Accept or reject one conflict or finding.

    ``decision`` is "accepted" or "rejected". Accepting a conflict requires
    ``chosen_claim_id``: the position that governs. Deciding one item never
    touches the others.

    This is a commercial judgement about which document controls. If you are an
    agent acting for someone, confirm with them rather than guessing.
    """
    state = analyst.store.load_run(run_id)
    if state is None:
        raise ValueError(f"unknown run {run_id}")
    return analyst.decide(
        corpus_id=state.corpus_id,
        run_id=run_id,
        decisions={item_id: decision},
        chosen={item_id: chosen_claim_id} if chosen_claim_id else {},
        notes={item_id: note} if note else {},
        actor="mcp",
    )


@mcp.tool()
def publish_register(run_id: str, force: bool = False) -> dict[str, Any]:
    """Commit the proposed register as the current one.

    Refuses while any conflict is undecided. ``force`` overrides that and is
    recorded in the audit trail as a forced publish.
    """
    state = analyst.store.load_run(run_id)
    if state is None:
        raise ValueError(f"unknown run {run_id}")
    return analyst.publish(
        corpus_id=state.corpus_id, run_id=run_id, actor="mcp", force=force
    )


@mcp.tool()
def resume_run(run_id: str) -> dict[str, Any]:
    """Continue a run that was interrupted, from its last checkpoint."""
    return analyst.resume(run_id)


@mcp.tool()
def corpus_status(corpus_id: str) -> dict[str, Any]:
    """Current revision, document count, and how much is awaiting a decision."""
    return analyst.status(corpus_id)


@mcp.tool()
def corpus_history(corpus_id: str, limit: int = 50) -> dict[str, Any]:
    """What changed, when, who decided it, and because of which source."""
    return {"corpus_id": corpus_id, "events": analyst.history(corpus_id, limit)}


@mcp.tool()
def get_register(corpus_id: str) -> str:
    """The published register as HTML, with every row's citations."""
    return analyst.register_html(corpus_id)


@mcp.tool()
def run_detail(run_id: str) -> dict[str, Any]:
    """Everything about one run: documents, rows, conflicts, findings, cost."""
    state = analyst.store.load_run(run_id)
    if state is None:
        raise ValueError(f"unknown run {run_id}")
    return {
        "run_id": state.run_id,
        "corpus_id": state.corpus_id,
        "revision": state.revision,
        "stage": state.stage,
        "documents": [{"name": d.name, "kind": d.kind, "why": d.kind_reason} for d in state.documents],
        "open_conflicts": [
            {"id": c.id, "subject": c.subject, "explanation": c.explanation,
             "positions": [
                 {"claim_id": x.id, "value": x.rendered(), "cite": x.provenance.cite()}
                 for x in c.claims
             ]}
            for c in state.open_conflicts
        ],
        "findings": [
            {"id": f.id, "severity": f.severity, "message": f.message}
            for f in state.findings if f.severity != "info"
        ],
        "notes": state.notes,
        "cost": state.cost_report(),
    }


if __name__ == "__main__":
    mcp.run()
