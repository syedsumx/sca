import { useState } from "react";
import { api } from "../api";
import type { RagHit } from "../types";

export default function RetrievalPage() {
  const [query, setQuery] = useState("refund to my card for the Quasar laptop");
  const [hits, setHits] = useState<RagHit[]>();
  const [error, setError] = useState<string>();

  const search = async () => {
    setError(undefined);
    try {
      setHits((await api.rag(query, 10)).results);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <h2>Retrieval playground</h2>
      <p className="muted">
        <code>POST /rag/query</code>. Vectors tied to a subject with revoked consent, a pending
        or completed deletion, or an expired retention date are filtered inside the database
        query, before the top-k cut.
      </p>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          void search();
        }}
      >
        <input className="search grow" value={query} onChange={(e) => setQuery(e.target.value)} />
        <button className="button" type="submit">
          Search
        </button>
      </form>
      {error && <div className="error">{error}</div>}
      <ol className="hits">
        {hits?.map((h) => (
          <li key={h.vector_id}>
            <div className="row between">
              <span>
                {h.subject_ids.map((s) => (
                  <a key={s} href={`#/subjects/${s}`} className="count-chip">
                    {s}
                  </a>
                ))}
              </span>
              <span className="muted small">score {h.score.toFixed(3)}</span>
            </div>
            <div>{h.content}</div>
            <code className="small muted">{h.vector_id}</code>
          </li>
        ))}
        {hits?.length === 0 && <li className="muted">No results.</li>}
      </ol>
    </div>
  );
}
