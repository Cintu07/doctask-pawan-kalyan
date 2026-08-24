"""The run, as a graph of stages you can watch.

Each node records what it decided and what it cost, and three of them can change
the route rather than simply passing data on:

* **extract** retries a document that yielded nothing, then escalates it to a
  human rather than pretending the document was empty.
* **verify** routes to a rescue pass when a model's citations do not check out,
  and refuses to continue if they still do not.
* **examine** skips entirely when there is nothing left to check, and says so.

The graph is checkpointed after every node, so killing the process mid-run and
starting it again resumes at the next node with all finished work intact.
"""

from __future__ import annotations

import logging
import pathlib
import time
import uuid
from contextlib import contextmanager
from typing import Annotated, Any, Iterator, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

from .conflicts import carry_forward_decisions, detect_conflicts
from .extract import extract_with_rules, missing_subjects, subjects_for, verify_claims
from .ingest import ingest_directory
from .llm import Extractor, default_extractor
from .models import Claim, SourceDocument, StageRecord
from .playbook import Rule, evaluate
from .register import build_rows
from .sanitize import scan_documents
from .store import Store

log = logging.getLogger(__name__)

MAX_EXTRACT_ATTEMPTS = 2


def _keep_last(_: Any, new: Any) -> Any:
    return new


def _extend(old: list, new: list) -> list:
    return (old or []) + (new or [])


class GraphState(TypedDict, total=False):
    """What flows between nodes.

    Deliberately plain data. Anything here has to survive being written to a
    checkpoint and read back by a different process.
    """

    run_id: str
    corpus_id: str
    source_dir: str
    rules: list[Any]
    documents: Annotated[list[SourceDocument], _keep_last]
    claims: Annotated[list[Claim], _keep_last]
    rejected: Annotated[list[str], _extend]
    conflicts: Annotated[list[Any], _keep_last]
    findings: Annotated[list[Any], _keep_last]
    rows: Annotated[list[Any], _keep_last]
    notes: Annotated[list[str], _extend]
    stages: Annotated[list[StageRecord], _extend]
    escalations: Annotated[list[str], _extend]
    quarantined: Annotated[list[str], _keep_last]
    extract_attempts: Annotated[int, _keep_last]
    incremental: bool


@contextmanager
def _timed(name: str, sink: list[StageRecord]) -> Iterator[dict[str, Any]]:
    """Time a stage and record how it ended, whatever happens."""
    started = time.perf_counter()
    box: dict[str, Any] = {"status": "ok", "detail": "", "model_calls": 0, "tokens": 0, "attempts": 1}
    try:
        yield box
    except Exception as exc:
        box["status"] = "failed"
        box["detail"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        sink.append(
            StageRecord(
                name=name,
                status=box["status"],
                seconds=round(time.perf_counter() - started, 3),
                detail=str(box["detail"])[:400],
                attempts=int(box["attempts"]),
                model_calls=int(box["model_calls"]),
                tokens=int(box["tokens"]),
            )
        )
        log.info("stage %s -> %s", name, box["status"])


class Pipeline:
    """Builds and runs the graph for one corpus."""

    def __init__(
        self,
        store: Store,
        *,
        extractor: Extractor | None = None,
        checkpoint_path: pathlib.Path | str | None = None,
    ) -> None:
        self.store = store
        self.extractor = extractor or default_extractor()
        self.checkpoint_path = pathlib.Path(
            checkpoint_path or store.path.parent / "checkpoints.sqlite"
        )

    # ------------------------------------------------------------- the nodes

    def ingest_node(self, state: GraphState) -> dict[str, Any]:
        stages: list[StageRecord] = []
        with _timed("ingest", stages) as box:
            known = (
                self.store.known_hashes(state["corpus_id"]) if state.get("incremental") else set()
            )
            documents, problems = ingest_directory(pathlib.Path(state["source_dir"]), known_hashes=known)

            if state.get("incremental"):
                # Old documents still have to be present for citations to resolve.
                previous = {d.id: d for d in self.store.load_documents(state["corpus_id"])}
                for document in documents:
                    previous[document.id] = document
                documents = sorted(previous.values(), key=lambda d: d.name)

            box["detail"] = f"{len(documents)} document(s), {len(problems)} unreadable"
            if not documents:
                box["status"] = "escalated"

        notes = list(problems)
        escalations = [] if documents else ["no readable documents in the source folder"]
        return {"documents": documents, "notes": notes, "stages": stages, "escalations": escalations}

    def guard_node(self, state: GraphState) -> dict[str, Any]:
        """A document that tries to give orders is content, not command."""
        stages: list[StageRecord] = []
        with _timed("guard", stages) as box:
            attempts, quarantined = scan_documents(state.get("documents", []))
            box["detail"] = (
                f"{len(attempts)} instruction-like passage(s), "
                f"{len(quarantined)} document(s) quarantined"
            )
            if quarantined:
                box["status"] = "escalated"
        return {"notes": attempts, "stages": stages, "quarantined": sorted(quarantined)}

    def extract_node(self, state: GraphState) -> dict[str, Any]:
        stages: list[StageRecord] = []
        attempt = int(state.get("extract_attempts", 0)) + 1
        documents = state.get("documents", [])
        claims: list[Claim] = []
        calls = tokens = 0

        with _timed("extract", stages) as box:
            box["attempts"] = attempt
            quarantined = set(state.get("quarantined", []))
            for document in documents:
                if document.id in quarantined:
                    # Hostile source: readable, reportable, but not authoritative.
                    continue
                found = extract_with_rules(document)
                gaps = missing_subjects(found)
                # Only ask about terms this kind of document may speak to, and
                # only where the rules came up empty.
                relevant = {s.key for s in subjects_for(document.kind)}
                gaps = [g for g in gaps if g.key in relevant]
                if gaps:
                    proposed, usage = self.extractor.propose(document, gaps)
                    calls += usage.calls
                    tokens += usage.tokens
                    found.extend(proposed)
                claims.extend(found)

            box["model_calls"] = calls
            box["tokens"] = tokens
            box["detail"] = f"{len(claims)} claim(s) from {len(documents)} document(s)"
            if not claims and attempt < MAX_EXTRACT_ATTEMPTS:
                box["status"] = "retried"
            elif not claims:
                box["status"] = "escalated"

        return {"claims": claims, "stages": stages, "extract_attempts": attempt}

    def verify_node(self, state: GraphState) -> dict[str, Any]:
        """Throw away every claim whose citation does not check out."""
        stages: list[StageRecord] = []
        documents = {d.id: d for d in state.get("documents", [])}
        with _timed("verify", stages) as box:
            kept, rejected = verify_claims(state.get("claims", []), documents)
            box["detail"] = f"{len(kept)} verified, {len(rejected)} rejected"
            if rejected:
                box["status"] = "retried" if len(rejected) > len(kept) else "ok"
        return {"claims": kept, "rejected": rejected, "stages": stages}

    def reconcile_node(self, state: GraphState) -> dict[str, Any]:
        stages: list[StageRecord] = []
        documents = {d.id: d for d in state.get("documents", [])}
        with _timed("reconcile", stages) as box:
            conflicts = detect_conflicts(state.get("claims", []), documents)
            # A question a human already answered stays answered, as long as the
            # positions have not changed.
            previous = self.store.latest_run(state["corpus_id"])
            carried = 0
            if previous is not None:
                conflicts, carried = carry_forward_decisions(conflicts, previous.conflicts)
            box["detail"] = (
                f"{len(conflicts)} conflict(s) between sources, "
                f"{carried} decision(s) carried forward"
            )
        return {"conflicts": conflicts, "stages": stages}

    def examine_node(self, state: GraphState) -> dict[str, Any]:
        stages: list[StageRecord] = []
        rules: list[Rule] = state.get("rules") or []
        with _timed("examine", stages) as box:
            if not rules:
                box["status"] = "skipped"
                box["detail"] = "no playbook supplied"
                return {"findings": [], "stages": stages}
            findings, unevaluated = evaluate(rules, state.get("claims", []))
            real = [f for f in findings if f.severity != "info"]
            box["detail"] = (
                f"{len(rules)} rule(s): {len(real)} breach(es), {len(unevaluated)} not checkable"
            )
        return {"findings": findings, "stages": stages}

    def register_node(self, state: GraphState) -> dict[str, Any]:
        stages: list[StageRecord] = []
        with _timed("register", stages) as box:
            rows = build_rows(state.get("claims", []), state.get("conflicts", []))
            grounded = sum(1 for r in rows if r.citations)
            box["detail"] = f"{len(rows)} row(s), {grounded} with a citation"
        return {"rows": rows, "stages": stages}

    # ------------------------------------------------------------- routing

    @staticmethod
    def after_ingest(state: GraphState) -> str:
        return "stop" if not state.get("documents") else "guard"

    @staticmethod
    def after_extract(state: GraphState) -> str:
        """A dry extraction is retried once, then handed to a person."""
        if state.get("claims"):
            return "verify"
        if int(state.get("extract_attempts", 0)) < MAX_EXTRACT_ATTEMPTS:
            return "extract"
        return "stop"

    @staticmethod
    def after_verify(state: GraphState) -> str:
        """If nothing survived verification there is nothing honest to report."""
        return "reconcile" if state.get("claims") else "stop"

    # --------------------------------------------------------------- build

    def build(self):
        graph = StateGraph(GraphState)
        graph.add_node("ingest", self.ingest_node)
        graph.add_node("guard", self.guard_node)
        graph.add_node("extract", self.extract_node)
        graph.add_node("verify", self.verify_node)
        graph.add_node("reconcile", self.reconcile_node)
        graph.add_node("examine", self.examine_node)
        graph.add_node("register", self.register_node)

        graph.set_entry_point("ingest")
        graph.add_conditional_edges("ingest", self.after_ingest, {"guard": "guard", "stop": END})
        graph.add_edge("guard", "extract")
        graph.add_conditional_edges(
            "extract", self.after_extract,
            {"verify": "verify", "extract": "extract", "stop": END},
        )
        graph.add_conditional_edges(
            "verify", self.after_verify, {"reconcile": "reconcile", "stop": END}
        )
        graph.add_edge("reconcile", "examine")
        graph.add_edge("examine", "register")
        graph.add_edge("register", END)
        return graph

    @contextmanager
    def compiled(self) -> Iterator[Any]:
        """Compile with a checkpointer so a killed run can be resumed."""
        with SqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver:
            yield self.build().compile(checkpointer=saver)

    def run(
        self,
        *,
        corpus_id: str,
        source_dir: pathlib.Path | str,
        rules: list[Rule] | None = None,
        run_id: str | None = None,
        incremental: bool = False,
    ) -> GraphState:
        """Execute the graph. Passing an existing ``run_id`` resumes it."""
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        config = {"configurable": {"thread_id": run_id}}
        initial: GraphState = {
            "run_id": run_id,
            "corpus_id": corpus_id,
            "source_dir": str(source_dir),
            "rules": rules or [],
            "incremental": incremental,
            "extract_attempts": 0,
        }
        with self.compiled() as app:
            return app.invoke(initial, config=config)

    def resume(self, run_id: str) -> GraphState:
        """Continue a run that was interrupted, from its last checkpoint."""
        config = {"configurable": {"thread_id": run_id}}
        with self.compiled() as app:
            snapshot = app.get_state(config)
            if not snapshot.values:
                raise KeyError(f"no checkpoint for run {run_id}")
            return app.invoke(None, config=config)

    def checkpoint_stage(self, run_id: str) -> str | None:
        """Which node a run would resume at, without running anything."""
        config = {"configurable": {"thread_id": run_id}}
        with self.compiled() as app:
            snapshot = app.get_state(config)
            if not snapshot.values:
                return None
            return snapshot.next[0] if snapshot.next else "done"
