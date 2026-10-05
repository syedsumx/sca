import StatusBadge from "../components/StatusBadge";
import { api } from "../api";
import { useAsync } from "../hooks";

export default function ModelsPage() {
  const models = useAsync(() => api.models(), []);
  return (
    <div className="page">
      <div className="row between">
        <h2>Model versions</h2>
        <button className="button" onClick={models.reload}>
          Refresh
        </button>
      </div>
      <p className="muted">
        Models link to the dataset snapshots they were trained on. When a subject in a snapshot
        is deleted, every model trained on that snapshot is flagged for retraining.
      </p>
      {models.error && <div className="error">{models.error}</div>}
      <table>
        <thead>
          <tr>
            <th>Model</th>
            <th>Trained on snapshots</th>
            <th>Status</th>
            <th>Retraining reasons</th>
          </tr>
        </thead>
        <tbody>
          {models.data?.map((m) => (
            <tr key={m.id} className={m.needs_retraining ? "flagged" : ""}>
              <td>
                <strong>{m.name}</strong> <code>v{m.version}</code>
              </td>
              <td>
                {m.trained_on.map((s) => (
                  <div key={s}>
                    <code className="small">{s}</code>
                  </div>
                ))}
              </td>
              <td>
                <StatusBadge value={m.status} />
              </td>
              <td>
                {m.retraining_reasons.length === 0 && <span className="muted">—</span>}
                {m.retraining_reasons.map((r, i) => (
                  <div key={i} className="small">
                    {r.reason}
                    {r.request_id && (
                      <a href={`#/deletions/${r.request_id}`} className="muted">
                        {" "}
                        (request)
                      </a>
                    )}
                  </div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
