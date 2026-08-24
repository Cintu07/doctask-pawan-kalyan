import { useCallback, useEffect, useState } from "react";

// The api base: in dev vite proxies /api, in production FastAPI serves this
// bundle itself so same-origin relative paths work.
const API = import.meta.env.DEV ? "/api" : "";

async function call(path, body) {
  const res = await fetch(API + path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    payload = { detail: text };
  }
  if (!res.ok) {
    const d = payload.detail ?? payload;
    // The api answers with a cause and a fix. Show both: the person reading
    // this is an analyst, not an engineer with the logs open.
    throw new Error(
      typeof d === "string" ? d : [d.error, d.cause, d.fix && `try: ${d.fix}`].filter(Boolean).join("\n")
    );
  }
  return payload;
}

function Quote({ text }) {
  return <blockquote className="quote">{text}</blockquote>;
}

function Position({ position, chosen, onChoose, disabled }) {
  return (
    <label className={"position" + (chosen ? " is-chosen" : "")}>
      <input
        type="radio"
        name={position.claim_id}
        checked={chosen}
        disabled={disabled}
        onChange={onChoose}
      />
      <div className="position-body">
        <div className="position-head">
          <b>{position.value}</b>
          <span className="cite">{position.cite}</span>
        </div>
        <Quote text={position.quote} />
      </div>
    </label>
  );
}

function Conflict({ conflict, onDecide, busy }) {
  const [chosen, setChosen] = useState(conflict.positions[0]?.claim_id ?? null);
  const settled = conflict.decision !== "pending";

  return (
    <article className={"card conflict" + (settled ? " is-settled" : "")}>
      <header>
        <h3>{conflict.subject.replace(/_/g, " ")}</h3>
        <span className={"tag tag-" + conflict.kind}>{conflict.kind.replace(/_/g, " ")}</span>
      </header>
      <p className="explain">{conflict.explanation}</p>

      <div className="positions">
        {conflict.positions.map((p) => (
          <Position
            key={p.claim_id}
            position={p}
            chosen={chosen === p.claim_id}
            disabled={settled || busy}
            onChoose={() => setChosen(p.claim_id)}
          />
        ))}
      </div>

      {settled ? (
        <p className="settled">decided: {conflict.decision}</p>
      ) : (
        <div className="actions">
          <button
            className="accept"
            disabled={busy || !chosen}
            onClick={() => onDecide(conflict.id, "accepted", chosen)}
          >
            this one governs
          </button>
          <button className="reject" disabled={busy} onClick={() => onDecide(conflict.id, "rejected")}>
            leave unresolved
          </button>
        </div>
      )}
    </article>
  );
}

function Finding({ finding, onDecide, busy }) {
  const settled = finding.decision !== "pending";
  return (
    <article className={"card finding sev-" + finding.severity}>
      <header>
        <h3>
          <span className="rule">{finding.rule_id}</span> {finding.message}
        </h3>
        <span className={"tag sev-" + finding.severity}>{finding.severity}</span>
      </header>
      {finding.cites?.length > 0 && <p className="cite">{finding.cites.join("; ")}</p>}
      {settled ? (
        <p className="settled">decided: {finding.decision}</p>
      ) : (
        <div className="actions">
          <button className="accept" disabled={busy} onClick={() => onDecide(finding.id, "accepted")}>
            acknowledge
          </button>
          <button className="reject" disabled={busy} onClick={() => onDecide(finding.id, "rejected")}>
            not applicable
          </button>
        </div>
      )}
    </article>
  );
}

export default function App() {
  const [corpus, setCorpus] = useState("northwind");
  const [folder, setFolder] = useState("northwind");
  const [playbook, setPlaybook] = useState("playbooks/vendor-contract.json");
  const [proposal, setProposal] = useState(null);
  const [detail, setDetail] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [published, setPublished] = useState(null);
  const [history, setHistory] = useState([]);

  const refresh = useCallback(async (runId, corpusId) => {
    const [d, h] = await Promise.all([
      call(`/runs/${runId}`),
      call(`/corpora/${corpusId}/history?limit=12`).catch(() => ({ events: [] })),
    ]);
    setDetail(d);
    setHistory(h.events ?? []);
  }, []);

  async function analyse() {
    setBusy(true);
    setError(null);
    setPublished(null);
    try {
      const p = await call("/analyse", {
        corpus_id: corpus,
        source_dir: folder,
        playbook: playbook || null,
        actor: "reviewer",
      });
      setProposal(p);
      await refresh(p.run_id, corpus);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function decide(itemId, decision, claimId) {
    setBusy(true);
    setError(null);
    try {
      await call("/decide", {
        run_id: proposal.run_id,
        decisions: { [itemId]: decision },
        chosen: claimId ? { [itemId]: claimId } : {},
        actor: "reviewer",
      });
      await refresh(proposal.run_id, corpus);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function publish(force = false) {
    setBusy(true);
    setError(null);
    try {
      const r = await call("/publish", { run_id: proposal.run_id, actor: "reviewer", force });
      setPublished(r);
      await refresh(proposal.run_id, corpus);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const openConflicts = (detail?.conflicts ?? []).filter((c) => c.decision === "pending");
  const realFindings = (detail?.findings ?? []).filter((f) => f.severity !== "info");

  return (
    <div className="app">
      <header className="top">
        <div>
          <h1>the analyst that never sleeps</h1>
          <p className="sub">
            every row traces to a span of text in a source document. nothing publishes until you say so.
          </p>
        </div>
        {detail && (
          <div className="cost">
            <span>revision {detail.revision}</span>
            <span>{detail.cost.total_seconds}s</span>
            <span>{detail.cost.model_calls} model calls</span>
          </div>
        )}
      </header>

      <section className="controls">
        <label>
          corpus
          <input value={corpus} onChange={(e) => setCorpus(e.target.value)} disabled={busy} />
        </label>
        <label>
          folder
          <input value={folder} onChange={(e) => setFolder(e.target.value)} disabled={busy} />
        </label>
        <label>
          playbook
          <input value={playbook} onChange={(e) => setPlaybook(e.target.value)} disabled={busy} />
        </label>
        <button className="primary" onClick={analyse} disabled={busy}>
          {busy ? "reading the pile..." : "analyse"}
        </button>
      </section>

      {error && <pre className="error">{error}</pre>}

      {detail && (
        <>
          <section className="stages">
            {detail.cost.stages.map((s) => (
              <div key={s.name} className={"stage st-" + s.status}>
                <b>{s.name}</b>
                <span>{s.status}</span>
                <span>{s.seconds}s</span>
              </div>
            ))}
          </section>

          {detail.notes?.length > 0 && (
            <section className="notes">
              {detail.notes.map((n, i) => (
                <p key={i}>{n}</p>
              ))}
            </section>
          )}

          <div className="columns">
            <div className="col">
              <h2>
                the register
                <span className="count">{detail.rows.length} rows</span>
              </h2>
              <table className="register">
                <tbody>
                  {detail.rows.map((r) => (
                    <tr key={r.subject} className={r.conflict ? "disputed" : ""}>
                      <td className="label">{r.label}</td>
                      <td>
                        <div className="value">{r.value}</div>
                        <div className="cite">{r.cites.join("; ") || "not stated"}</div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="col">
              <h2>
                needs a decision
                <span className="count">{openConflicts.length} open</span>
              </h2>
              {detail.conflicts.map((c) => (
                <Conflict key={c.id} conflict={c} onDecide={decide} busy={busy} />
              ))}

              {realFindings.length > 0 ? (
                <>
                  <h2>findings</h2>
                  {realFindings.map((f) => (
                    <Finding key={f.id} finding={f} onDecide={decide} busy={busy} />
                  ))}
                </>
              ) : (
                <p className="clean">
                  no findings. every rule in the playbook was checked against the sources and none was
                  breached.
                </p>
              )}

              <div className="gate">
                <button className="primary" onClick={() => publish(false)} disabled={busy}>
                  publish register
                </button>
                {openConflicts.length > 0 && (
                  <p className="hint">
                    {openConflicts.length} conflict(s) still open. publishing will be refused until they
                    are decided.
                  </p>
                )}
                {published && (
                  <p className="ok">
                    published revision {published.revision}. changed:{" "}
                    {published.changed.join(", ") || "nothing"}. {published.unchanged} row(s) unchanged
                    and byte identical.
                    {published.forced && " recorded as a forced publish."}
                  </p>
                )}
              </div>

              {history.length > 0 && (
                <>
                  <h2>audit trail</h2>
                  <ul className="history">
                    {history.map((e, i) => (
                      <li key={i}>
                        <span className="who">{e.actor}</span>
                        <span className="what">{e.action}</span>
                        <span className="when">{e.at?.slice(11, 19)}</span>
                        {e.because && <span className="because">because {e.because}</span>}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
