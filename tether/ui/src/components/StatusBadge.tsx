const TONES: Record<string, string> = {
  certified: "good",
  ok: "good",
  active: "good",
  granted: "good",
  failed: "bad",
  needs_retraining: "bad",
  revoked: "bad",
  consent_revoked: "bad",
  deleted: "bad",
  purged: "muted",
  deletion_pending: "warn",
  needs_regeneration: "warn",
  pending: "warn",
};

export default function StatusBadge({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  return <span className={`badge ${TONES[value] ?? "info"}`}>{value.replaceAll("_", " ")}</span>;
}
