import { createContext, useContext } from "react";

/** Cairn Core's own entity page paths, keyed by the urn's entity type. */
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
 * Base URL of the Cairn Core UI that hosts this workspace ('' = same origin, the MFE default).
 * `null` means there is no Cairn Core UI to link to (standalone demo), so native links are hidden.
 */
export const CatalogUrlContext = createContext<string | null>("");

export function catalogEntityHref(base: string, urn: string): string | null {
  const type = urn.startsWith("urn:li:") ? urn.split(":")[2] : "";
  const path = ENTITY_PATHS[type];
  return path
    ? `${base.replace(/\/+$/, "")}/${path}/${encodeURIComponent(urn)}`
    : null;
}

/** Link to the asset's page in Cairn Core itself, or `null` when none applies. */
export function useCoreHref(urn: string): string | null {
  const base = useContext(CatalogUrlContext);
  return base === null ? null : catalogEntityHref(base, urn);
}
