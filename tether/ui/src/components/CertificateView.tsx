import { api } from "../api";
import type { Certificate } from "../types";

export default function CertificateView({ cert }: { cert: Certificate }) {
  const counts = cert.content.purged?.counts ?? {};
  const models = cert.content.models_flagged_for_retraining ?? [];
  const regen = cert.content.memory_needing_regeneration ?? [];
  return (
    <div className="certificate">
      <div className="cert-head">
        <div>
          <h3>Deletion certificate</h3>
          <div className="muted small">
            {cert.certificate_id} · issued {new Date(cert.issued_at).toLocaleString()}
          </div>
        </div>
        <span className={`badge ${cert.verified ? "good" : "bad"}`}>
          {cert.verified ? "✓ hash verified" : "✗ hash mismatch"}
        </span>
      </div>
      <div className="hash">
        <span className="muted small">SHA-256</span>
        <code>{cert.sha256}</code>
      </div>
      <div className="cert-grid">
        <div>
          <h4>Purged</h4>
          <ul>
            {Object.entries(counts).map(([type, n]) => (
              <li key={type}>
                {n} × {type.replace("_", " ")}
              </li>
            ))}
            {Object.keys(counts).length === 0 && <li className="muted">nothing found</li>}
          </ul>
          <div className="muted small">{cert.content.purged?.rows_deleted ?? 0} rows deleted</div>
        </div>
        <div>
          <h4>Models flagged for retraining</h4>
          <ul>
            {models.map((m) => (
              <li key={m.model}>
                <strong>{m.model}</strong>
                <div className="muted small">{m.reason}</div>
              </li>
            ))}
            {models.length === 0 && <li className="muted">none</li>}
          </ul>
        </div>
        <div>
          <h4>Memory to regenerate</h4>
          <ul>
            {regen.map((m) => (
              <li key={m}>{m.replace("memory:", "")}</li>
            ))}
            {regen.length === 0 && <li className="muted">none</li>}
          </ul>
        </div>
      </div>
      <div className="row">
        <a className="button" href={api.certificateDownloadUrl(cert.request_id)} download>
          ⬇ Download JSON
        </a>
        <details>
          <summary>Raw certificate</summary>
          <pre>{JSON.stringify(cert.content, null, 2)}</pre>
        </details>
      </div>
    </div>
  );
}
