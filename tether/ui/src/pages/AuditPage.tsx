import { api } from "../api";
import { useAsync } from "../hooks";

export default function AuditPage() {
  const log = useAsync(() => api.audit(), []);
  return (
    <div className="page">
      <div className="row between">
        <h2>Audit log</h2>
        <div className="row">
          {log.data && (
            <span className={`badge ${log.data.chain_ok ? "good" : "bad"}`}>
              {log.data.chain_ok
                ? `✓ hash chain intact (${log.data.chain_entries} entries)`
                : `✗ chain broken at #${log.data.first_broken_id}`}
            </span>
          )}
          <button className="button" onClick={log.reload}>
            Refresh
          </button>
        </div>
      </div>
      <p className="muted">
        Append-only (a database trigger blocks UPDATE and DELETE) and hash-chained. Each
        entry commits to the one before it.
      </p>
      {log.error && <div className="error">{log.error}</div>}
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Time</th>
            <th>Event</th>
            <th>Subject</th>
            <th>Details</th>
            <th>Hash</th>
          </tr>
        </thead>
        <tbody>
          {log.data?.entries.map((e) => (
            <tr key={e.id}>
              <td>{e.id}</td>
              <td className="small nowrap">{new Date(e.ts).toLocaleString()}</td>
              <td>
                <code>{e.event_type}</code>
              </td>
              <td>{e.subject_id && <a href={`#/subjects/${e.subject_id}`}>{e.subject_id}</a>}</td>
              <td className="small mono">{JSON.stringify(e.payload).slice(0, 160)}</td>
              <td className="small mono">{e.hash.slice(0, 12)}…</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
