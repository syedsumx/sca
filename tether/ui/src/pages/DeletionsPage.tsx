import DeletionTracker from "../components/DeletionTracker";
import StatusBadge from "../components/StatusBadge";
import { api } from "../api";
import { useAsync } from "../hooks";
import { navigate } from "../router";

export default function DeletionsPage({ requestId }: { requestId?: string }) {
  const list = useAsync(() => api.deletions(), [requestId]);
  return (
    <div className="split">
      <aside className="sidebar">
        <h3>Deletion requests</h3>
        {list.error && <div className="error">{list.error}</div>}
        <ul className="subject-list">
          {list.data?.map((r) => (
            <li
              key={r.id}
              className={r.id === requestId ? "selected" : ""}
              onClick={() => navigate("deletions", r.id)}
            >
              <div className="row between">
                <strong>{r.subject_id}</strong>
                <StatusBadge value={r.status} />
              </div>
              <div className="muted small">{new Date(r.created_at).toLocaleString()}</div>
            </li>
          ))}
          {list.data?.length === 0 && <li className="muted">No deletion requests yet.</li>}
        </ul>
      </aside>
      <section className="content">
        {requestId ? (
          <div className="card">
            <DeletionTracker key={requestId} requestId={requestId} onChange={list.reload} />
          </div>
        ) : (
          <div className="empty">
            <h2>Deletion requests</h2>
            <p>Select a request to see its live status and certificate.</p>
          </div>
        )}
      </section>
    </div>
  );
}
