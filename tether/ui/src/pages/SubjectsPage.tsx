import { useState } from "react";
import { api } from "../api";
import LineageGraph, { GraphLegend, TYPE_META } from "../components/LineageGraph";
import DeletionTracker from "../components/DeletionTracker";
import StatusBadge from "../components/StatusBadge";
import { useAsync } from "../hooks";
import { navigate } from "../router";
import type { LineageNode } from "../types";

export default function SubjectsPage({ subjectId }: { subjectId?: string }) {
  const [q, setQ] = useState("");
  const subjects = useAsync(() => api.subjects(q), [q]);

  return (
    <div className="split">
      <aside className="sidebar">
        <input
          className="search"
          placeholder="Search subject id, name or email…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          autoFocus
        />
        {subjects.error && <div className="error">{subjects.error}</div>}
        <ul className="subject-list">
          {subjects.data?.map((s) => (
            <li
              key={s.subject_id}
              className={s.subject_id === subjectId ? "selected" : ""}
              onClick={() => navigate("subjects", s.subject_id)}
            >
              <div className="row between">
                <strong>{s.subject_id}</strong>
                {s.deletion_status ? (
                  <StatusBadge value={s.deletion_status} />
                ) : (
                  s.consent_status !== "granted" && <StatusBadge value={s.consent_status} />
                )}
              </div>
              <div className="muted small">
                {s.display_name ?? "(erased)"} · {s.artifact_count} artifacts
              </div>
            </li>
          ))}
          {subjects.data?.length === 0 && (
            <li className="muted">No subjects. Run the pipelines first (make demo).</li>
          )}
        </ul>
      </aside>
      <section className="content">
        {subjectId ? (
          <SubjectDetail key={subjectId} subjectId={subjectId} onChanged={subjects.reload} />
        ) : (
          <div className="empty">
            <h2>Where did this person's data go?</h2>
            <p>
              Pick a subject to see every chunk, vector, training snapshot, model and memory
              entry derived from their data, then issue a deletion request and watch it go
              through each step.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}

function SubjectDetail({ subjectId, onChanged }: { subjectId: string; onChanged: () => void }) {
  const subject = useAsync(() => api.subject(subjectId), [subjectId]);
  const lineage = useAsync(() => api.lineage(subjectId), [subjectId]);
  const [selected, setSelected] = useState<LineageNode>();
  const [requestId, setRequestId] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string>();

  const s = subject.data;
  const activeRequest = requestId ?? s?.deletion_request_id ?? undefined;

  const refresh = () => {
    subject.reload();
    lineage.reload();
    onChanged();
  };

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setActionError(undefined);
    try {
      await fn();
      refresh();
    } catch (e) {
      setActionError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const counts = new Map<string, { total: number; active: number }>();
  for (const n of lineage.data?.nodes ?? []) {
    const c = counts.get(n.type) ?? { total: 0, active: 0 };
    c.total += 1;
    if (n.status === "active") c.active += 1;
    counts.set(n.type, c);
  }

  return (
    <div className="detail">
      <div className="card subject-card">
        <div>
          <h2>
            {subjectId} <span className="muted">{s?.display_name ?? (s ? "(erased)" : "")}</span>
          </h2>
          <div className="muted small">{s?.email}</div>
          <div className="row tags">
            <span>
              consent <StatusBadge value={s?.consent_status} />
            </span>
            <span>
              license <code>{s?.license ?? "—"}</code>
            </span>
            <span>
              retention until <code>{s?.retention_until ?? "—"}</code>
            </span>
            {s?.restrictions.map((r) => <StatusBadge key={r} value={r} />)}
          </div>
        </div>
        <div className="actions">
          {s?.consent_status === "revoked" ? (
            <button
              className="button"
              disabled={busy || !!s.deletion_request_id}
              onClick={() => void run(() => api.setConsent(subjectId, "granted"))}
            >
              Grant consent
            </button>
          ) : (
            <button
              className="button"
              disabled={busy || !s || !!s.deletion_request_id}
              onClick={() => void run(() => api.setConsent(subjectId, "revoked"))}
            >
              Revoke consent
            </button>
          )}
          <button
            className="button danger"
            disabled={busy || !s || !!s.deletion_request_id}
            onClick={() =>
              void run(async () => {
                const r = await api.requestDeletion(subjectId, "Erasure request from the UI");
                setRequestId(r.id);
              })
            }
          >
            Delete subject
          </button>
        </div>
      </div>
      {actionError && <div className="error">{actionError}</div>}

      {activeRequest && (
        <div className="card">
          <DeletionTracker requestId={activeRequest} onChange={refresh} />
        </div>
      )}

      <div className="card">
        <div className="row between">
          <h3>Lineage graph</h3>
          <div className="counts">
            {[...counts.entries()].map(([type, c]) => (
              <span key={type} className="count-chip">
                <span
                  className="swatch"
                  style={{ background: TYPE_META[type as keyof typeof TYPE_META]?.color }}
                />
                {c.active}/{c.total} {type.replace("_", " ")}
              </span>
            ))}
          </div>
        </div>
        <GraphLegend />
        {lineage.error && <div className="error">{lineage.error}</div>}
        <div className="graph-wrap">
          {lineage.data && <LineageGraph lineage={lineage.data} onSelect={setSelected} />}
          {selected && (
            <div className="node-panel">
              <div className="row between">
                <strong>{TYPE_META[selected.type].label}</strong>
                <button className="link" onClick={() => setSelected(undefined)}>
                  ✕
                </button>
              </div>
              <code className="small break">{selected.id}</code>
              <div>
                <StatusBadge value={selected.status} />
              </div>
              <h4>Subjects</h4>
              <div className="small">{selected.subject_ids.join(", ") || "—"}</div>
              <h4>Effective policy</h4>
              <pre>{JSON.stringify(selected.policy, null, 2)}</pre>
              <h4>Attributes</h4>
              <pre>{JSON.stringify(selected.attrs, null, 2)}</pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
