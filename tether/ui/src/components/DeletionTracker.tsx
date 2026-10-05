import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DeletionRequest } from "../types";
import CertificateView from "./CertificateView";
import StatusBadge from "./StatusBadge";

export const STEPS = [
  ["received", "Received"],
  ["retrieval_blocked", "Retrieval blocked"],
  ["discovered", "Artifacts discovered"],
  ["purging", "Purging"],
  ["purged", "Purged"],
  ["models_flagged", "Models flagged"],
  ["certified", "Certified"],
] as const;

interface Props {
  requestId: string;
  onChange?: (req: DeletionRequest) => void;
}

/** Polls a deletion request until it is certified or failed, rendering a live stepper. */
export default function DeletionTracker({ requestId, onChange }: Props) {
  const [req, setReq] = useState<DeletionRequest>();
  const [error, setError] = useState<string>();
  const [nonce, setNonce] = useState(0); // bump to restart polling (after a retry)
  const lastStatus = useRef<string | undefined>(undefined);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const r = await api.deletion(requestId);
        if (!alive) return;
        setReq(r);
        setError(undefined);
        if (r.status !== lastStatus.current) {
          lastStatus.current = r.status;
          onChangeRef.current?.(r);
        }
        if (r.status !== "certified" && r.status !== "failed") {
          timer = window.setTimeout(poll, 700);
        }
      } catch (e) {
        if (alive) {
          setError(String(e));
          timer = window.setTimeout(poll, 2000);
        }
      }
    };
    lastStatus.current = undefined;
    void poll();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [requestId, nonce]);

  if (error && !req) return <div className="error">{error}</div>;
  if (!req) return <div className="muted">Loading deletion request…</div>;

  const reached = new Map(req.history.map((h) => [h.status, h.at]));
  const failed = req.status === "failed";
  const currentIdx = STEPS.findIndex(([s]) => s === req.status);

  return (
    <div className="tracker">
      <div className="tracker-head">
        <div>
          <strong>Deletion request</strong> <code className="small">{req.id}</code>
          {req.reason && <div className="muted small">Reason: {req.reason}</div>}
        </div>
        <StatusBadge value={req.status} />
      </div>
      <ol className="stepper">
        {STEPS.map(([key, label], i) => {
          const at = reached.get(key);
          const state = at
            ? "done"
            : !failed && i === currentIdx + 1
              ? "active"
              : "todo";
          return (
            <li key={key} className={state}>
              <span className="dot">{at ? "✓" : i + 1}</span>
              <span className="step-label">{label}</span>
              {at && <span className="muted small">{new Date(at).toLocaleTimeString()}</span>}
            </li>
          );
        })}
      </ol>
      {failed && (
        <div className="error">
          <strong>Failed</strong> in step <code>{String(req.error?.failed_in_status)}</code>:{" "}
          {String(req.error?.message)}
          <button
            className="button small"
            onClick={() => void api.retry(req.id).then(() => setNonce((n) => n + 1))}
          >
            Retry
          </button>
        </div>
      )}
      {req.inventory.counts && (
        <div className="muted small">
          Discovered:{" "}
          {Object.entries(req.inventory.counts)
            .map(([t, n]) => `${n} ${t}`)
            .join(" · ")}
        </div>
      )}
      {req.certificate && <CertificateView cert={req.certificate} />}
    </div>
  );
}
