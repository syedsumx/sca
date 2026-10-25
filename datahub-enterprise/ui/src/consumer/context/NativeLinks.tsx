import { createContext, useContext } from "react";

/** DataHub's own entity page paths, keyed by the urn's entity type. */
const ENTITY_PATHS: Record<string, string> = {
  dataset: "dataset",
  dashboard: "dashboard",
  chart: "chart",
  dataJob: "tasks",
  dataFlow: "pipelines",
  dataProduct: "dataProduct",
  glossaryTerm: "glossaryTerm",
  domain: "domain",
  container: "container",
  corpuser: "user",
  corpGroup: "group",
};

/**
 * Base URL of the DataHub UI that hosts this workspace ('' = same origin, the MFE default).
 * `null` means there is no DataHub UI to link to (standalone demo), so native links are hidden.
 */
export const DataHubUrlContext = createContext<string | null>("");

export function datahubEntityHref(base: string, urn: string): string | null {
  const type = urn.startsWith("urn:li:") ? urn.split(":")[2] : "";
  const path = ENTITY_PATHS[type];
  return path
    ? `${base.replace(/\/+$/, "")}/${path}/${encodeURIComponent(urn)}`
    : null;
}

/** Link to the asset's page in DataHub itself, or `null` when none applies. */
export function useDataHubHref(urn: string): string | null {
  const base = useContext(DataHubUrlContext);
  return base === null ? null : datahubEntityHref(base, urn);
}
