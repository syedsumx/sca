import { useRoute } from "./router";
import SubjectsPage from "./pages/SubjectsPage";
import DeletionsPage from "./pages/DeletionsPage";
import ModelsPage from "./pages/ModelsPage";
import AuditPage from "./pages/AuditPage";
import RetrievalPage from "./pages/RetrievalPage";

const NAV = [
  ["subjects", "Subjects & lineage"],
  ["deletions", "Deletion requests"],
  ["models", "Models"],
  ["retrieval", "Retrieval"],
  ["audit", "Audit log"],
] as const;

export default function App() {
  const [page = "subjects", ...rest] = useRoute();
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden>
            ⛓
          </span>
          Tether
          <span className="tagline">AI data lineage &amp; deletion</span>
        </div>
        <nav>
          {NAV.map(([key, label]) => (
            <a key={key} href={`#/${key}`} className={page === key ? "active" : ""}>
              {label}
            </a>
          ))}
          <a href="/api/docs" target="_blank" rel="noreferrer">
            API docs ↗
          </a>
        </nav>
      </header>
      <main>
        {page === "subjects" && <SubjectsPage subjectId={rest[0]} />}
        {page === "deletions" && <DeletionsPage requestId={rest[0]} />}
        {page === "models" && <ModelsPage />}
        {page === "retrieval" && <RetrievalPage />}
        {page === "audit" && <AuditPage />}
      </main>
    </div>
  );
}
