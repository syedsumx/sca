/**
 * Cairn Core's catalog (L147): the GraphQL the Catalog section asks Cairn Core (through the masking proxy, as the
 * signed-in person) and the Cairn API it pairs with (data products made in Cairn, contracts). Every answer is Core's
 * or Cairn's own; nothing here is made up when a call fails (the pages say what could not be read).
 */
import type { ConsumerClient } from './client';
import type { RequestOptions } from '../../api/client';
import type { SearchResponse } from './types';

/** The kinds of asset the catalog browses (Cairn Core entity types), in the order they are offered. */
export const ASSET_TYPES = [
    'DATASET', 'DASHBOARD', 'CHART', 'DATA_FLOW', 'DATA_JOB', 'MLMODEL', 'MLMODEL_GROUP', 'MLFEATURE_TABLE', 'CONTAINER',
] as const;

/**
 * What a search also finds (the Catalog's search is the one search, where /search leads): business terms, data
 * products, domains, people and groups, as the earlier search page did. Browsing without a query stays on assets.
 */
export const SEARCH_ONLY_TYPES = ['GLOSSARY_TERM', 'DATA_PRODUCT', 'DOMAIN', 'CORP_USER', 'CORP_GROUP'] as const;

/** The types offered: the assets, and with a query what a search also finds. */
export function catalogTypes(query: string): string[] {
    return query.trim() ? [...ASSET_TYPES, ...SEARCH_ONLY_TYPES] : [...ASSET_TYPES];
}

/** Counts per entity type, platform and domain across the whole catalog (`filters` narrow it; types are not). */
export async function catalogCounts(c: ConsumerClient, query: string, filters: Record<string, string[]>,
    signal?: AbortSignal): Promise<SearchResponse> {
    const rest = Object.fromEntries(Object.entries(filters).filter(([k]) => k !== '_entityType'));
    return c.search({ query, filters: { ...rest, _entityType: catalogTypes(query) }, start: 0, count: 0 }, signal);
}

/** One page of the catalog: the chosen types (all asset types when none is chosen) and facets. */
export function catalogPage(c: ConsumerClient, query: string, filters: Record<string, string[]>, start: number,
    count: number, signal?: AbortSignal): Promise<SearchResponse> {
    const types = filters._entityType?.length ? filters._entityType : catalogTypes(query);
    return c.search({ query, filters: { ...filters, _entityType: types }, start, count }, signal);
}

// ------------------------------------------------------------------------------------------------ glossary

export interface NamedEntity {
    urn: string;
    type?: string;
    name?: string | null;
    properties?: { name?: string | null; description?: string | null; definition?: string | null } | null;
}

export interface GlossaryNodeSummary extends NamedEntity {
    childrenCount?: { termsCount: number; nodesCount: number } | null;
}

export interface GlossaryRoot {
    nodes: GlossaryNodeSummary[];
    terms: NamedEntity[];
}

export const GLOSSARY_ROOT_QUERY = `
query cairnGlossaryRoot {
  getRootGlossaryNodes(input: { start: 0, count: 1000 }) {
    nodes { urn type properties { name description } childrenCount { termsCount nodesCount } }
  }
  getRootGlossaryTerms(input: { start: 0, count: 1000 }) {
    terms { urn type name properties { name description definition } }
  }
}`;

export async function glossaryRoot(c: ConsumerClient, signal?: AbortSignal): Promise<GlossaryRoot> {
    const d = await c.gql<{
        getRootGlossaryNodes: { nodes: GlossaryNodeSummary[] } | null;
        getRootGlossaryTerms: { terms: NamedEntity[] } | null;
    }>(GLOSSARY_ROOT_QUERY, {}, signal);
    return { nodes: d.getRootGlossaryNodes?.nodes ?? [], terms: d.getRootGlossaryTerms?.terms ?? [] };
}

const OWNERS = `ownership { owners { owner { ... on CorpUser { urn username properties { displayName } } ... on CorpGroup { urn name } } } }`;

export interface Owners {
    owners: { owner: { urn: string; username?: string; name?: string; properties?: { displayName?: string | null } | null } }[];
}

export interface GlossaryGroup extends NamedEntity {
    parentNodes?: { nodes: NamedEntity[] } | null;
    children?: { total: number; relationships: { entity: NamedEntity | null }[] } | null;
    ownership?: Owners | null;
}

export const GLOSSARY_NODE_QUERY = `
query cairnGlossaryNode($urn: String!) {
  glossaryNode(urn: $urn) {
    urn type
    properties { name description }
    parentNodes { nodes { urn properties { name } } }
    ${OWNERS}
    children: relationships(input: { types: ["IsPartOf"], direction: INCOMING, start: 0, count: 1000 }) {
      total
      relationships { entity { urn type ... on GlossaryTerm { name properties { name description definition } } ... on GlossaryNode { properties { name description } } } }
    }
  }
}`;

export async function glossaryNode(c: ConsumerClient, urn: string, signal?: AbortSignal): Promise<GlossaryGroup | null> {
    return (await c.gql<{ glossaryNode: GlossaryGroup | null }>(GLOSSARY_NODE_QUERY, { urn }, signal)).glossaryNode;
}

type Related = { relationships: { entity: NamedEntity | null }[] } | null;

export interface GlossaryTermDetail extends NamedEntity {
    hierarchicalName?: string | null;
    properties?: { name?: string | null; description?: string | null; definition?: string | null; termSource?: string | null;
        sourceUrl?: string | null } | null;
    parentNodes?: { nodes: NamedEntity[] } | null;
    ownership?: Owners | null;
    isA?: Related;
    hasA?: Related;
    related?: Related;
}

// The edges come from Core's glossaryRelatedTerms aspect: isRelatedTerms is "IsA", hasRelatedTerms is "HasA" and
// relatedTerms is "IsRelatedTo" (the @Relationship names in GlossaryRelatedTerms.pdl).
export const GLOSSARY_TERM_QUERY = `
query cairnGlossaryTerm($urn: String!) {
  glossaryTerm(urn: $urn) {
    urn type name hierarchicalName
    properties { name description definition termSource sourceUrl }
    parentNodes { nodes { urn properties { name } } }
    ${OWNERS}
    isA: relationships(input: { types: ["IsA"], direction: OUTGOING, start: 0, count: 100 }) {
      relationships { entity { urn type ... on GlossaryTerm { name properties { name } } } }
    }
    hasA: relationships(input: { types: ["HasA"], direction: OUTGOING, start: 0, count: 100 }) {
      relationships { entity { urn type ... on GlossaryTerm { name properties { name } } } }
    }
    related: relationships(input: { types: ["IsRelatedTo"], direction: OUTGOING, start: 0, count: 100 }) {
      relationships { entity { urn type ... on GlossaryTerm { name properties { name } } } }
    }
  }
}`;

export async function glossaryTerm(c: ConsumerClient, urn: string, signal?: AbortSignal): Promise<GlossaryTermDetail | null> {
    return (await c.gql<{ glossaryTerm: GlossaryTermDetail | null }>(GLOSSARY_TERM_QUERY, { urn }, signal)).glossaryTerm;
}

// ------------------------------------------------------------------------------------------------- domains

export interface DomainSummary extends NamedEntity {
    ownership?: Owners | null;
    entities?: { total: number } | null;
}

export const DOMAINS_QUERY = `
query cairnDomains {
  listDomains(input: { start: 0, count: 1000 }) {
    total
    domains { urn type properties { name description } ${OWNERS} entities(input: { start: 0, count: 0 }) { total } }
  }
}`;

export async function domains(c: ConsumerClient, signal?: AbortSignal): Promise<DomainSummary[]> {
    const d = await c.gql<{ listDomains: { domains: DomainSummary[] } | null }>(DOMAINS_QUERY, {}, signal);
    return d.listDomains?.domains ?? [];
}

export interface DomainDetail extends DomainSummary {
    parentDomains?: { domains: NamedEntity[] } | null;
    children?: { relationships: { entity: NamedEntity | null }[] } | null;
}

export const DOMAIN_QUERY = `
query cairnDomain($urn: String!) {
  domain(urn: $urn) {
    urn type
    properties { name description }
    ${OWNERS}
    parentDomains { domains { urn ... on Domain { properties { name } } } }
    children: relationships(input: { types: ["IsPartOf"], direction: INCOMING, start: 0, count: 1000 }) {
      relationships { entity { urn type ... on Domain { properties { name description } } } }
    }
  }
}`;

export async function domain(c: ConsumerClient, urn: string, signal?: AbortSignal): Promise<DomainDetail | null> {
    return (await c.gql<{ domain: DomainDetail | null }>(DOMAIN_QUERY, { urn }, signal)).domain;
}

// ------------------------------------------------------------------------------------------- column lineage

export interface FieldRef {
    urn: string;
    path: string;
}

export interface ColumnLineage {
    upstreams: FieldRef[];
    downstreams: FieldRef[];
    transformOperation?: string | null;
}

export const COLUMN_LINEAGE_QUERY = `
query cairnColumnLineage($urn: String!) {
  dataset(urn: $urn) {
    urn
    fineGrainedLineages { upstreams { urn path } downstreams { urn path } transformOperation }
  }
}`;

export async function columnLineage(c: ConsumerClient, urn: string, signal?: AbortSignal): Promise<ColumnLineage[]> {
    const d = await c.gql<{ dataset: { fineGrainedLineages?: ColumnLineage[] | null } | null }>(COLUMN_LINEAGE_QUERY,
        { urn }, signal);
    return d.dataset?.fineGrainedLineages ?? [];
}

// -------------------------------------------------------------------------------- Cairn's side of the catalog

/** A data product as Cairn writes it to Cairn Core (`/catalog/products`). */
export interface CatalogProduct {
    id: string;
    urn: string;
    name: string;
    description: string;
    domain: string | null;
    owners: string[];
    assets: string[];
    output_ports: string[];
    managed_by_cairn: boolean;
}

export function catalogProducts(c: ConsumerClient, opts?: RequestOptions) {
    return c.request<{ products: CatalogProduct[] }>('GET', '/catalog/products', undefined, opts);
}

export function catalogProduct(c: ConsumerClient, id: string, opts?: RequestOptions) {
    return c.request<CatalogProduct>('GET', `/catalog/products/${encodeURIComponent(id)}`, undefined, opts);
}

/** Every data contract the person may see (`GET /quality/contracts`). */
export function contracts(c: ConsumerClient, opts?: RequestOptions) {
    return c.request<import('./types').Contract[]>('GET', '/quality/contracts', undefined, opts);
}

/** The name an entity is known by: its properties' name, else its name, else the URN's last part. */
export function nameOf(e: NamedEntity | null | undefined): string {
    if (!e) return '';
    return e.properties?.name || e.name || e.urn.split(':').slice(3).join(':');
}

/** People and groups that own an entity, by URN. */
export function ownerUrns(o: Owners | null | undefined): string[] {
    return (o?.owners ?? []).map((x) => x.owner?.urn).filter((u): u is string => !!u);
}

// ------------------------------------------------------------- the glossary and domains, edited through Cairn (L148)

/** What the signed-in person may do on a glossary entry or domain (or at the top level), and why not. */
export interface EditRights {
    may_manage: boolean;
    reason: string;
    /** Glossary terms: a change can be proposed for approval instead. */
    may_propose?: boolean;
}

export function glossaryRights(c: ConsumerClient, urn?: string, opts?: RequestOptions) {
    const q = urn ? `?urn=${encodeURIComponent(urn)}` : '';
    return c.request<EditRights>('GET', `/catalog/glossary/rights${q}`, undefined, opts);
}

export function domainRights(c: ConsumerClient, urn?: string, opts?: RequestOptions) {
    const q = urn ? `?urn=${encodeURIComponent(urn)}` : '';
    return c.request<EditRights>('GET', `/catalog/domains/rights${q}`, undefined, opts);
}

/** A glossary term as Cairn writes it to Cairn Core (`/catalog/glossary/terms`). */
export interface CatalogTerm {
    id: string;
    urn: string;
    name: string;
    definition: string;
    parent: string | null;
    is_a: string[];
    has_a: string[];
    related: string[];
    owners: string[];
    stewards: string[];
    custom_properties: Record<string, string>;
    source_url: string | null;
    deprecated: boolean;
    deprecation_note: string;
}

/** A term group (`/catalog/glossary/nodes`). */
export interface CatalogNode {
    id: string;
    urn: string;
    name: string;
    description: string;
    parent: string | null;
    owners: string[];
    custom_properties: Record<string, string>;
}

/** A domain (`/catalog/domains`). */
export interface CatalogDomain {
    id: string;
    urn: string;
    name: string;
    description: string;
    parent: string | null;
    owners: string[];
}

/** The id in a URN (`urn:li:glossaryTerm:<id>`). */
export const idOf = (urn: string): string => urn.split(':').slice(3).join(':');

export function catalogTerms(c: ConsumerClient, opts?: RequestOptions) {
    return c.request<{ terms: CatalogTerm[] }>('GET', '/catalog/glossary/terms', undefined, opts);
}

export function catalogTerm(c: ConsumerClient, urn: string, opts?: RequestOptions) {
    return c.request<CatalogTerm>('GET', `/catalog/glossary/terms/${encodeURIComponent(idOf(urn))}`, undefined, opts);
}

export function catalogNode(c: ConsumerClient, urn: string, opts?: RequestOptions) {
    return c.request<CatalogNode>('GET', `/catalog/glossary/nodes/${encodeURIComponent(idOf(urn))}`, undefined, opts);
}

export function catalogDomains(c: ConsumerClient, opts?: RequestOptions) {
    return c.request<{ domains: CatalogDomain[] }>('GET', '/catalog/domains', undefined, opts);
}

export function catalogDomain(c: ConsumerClient, urn: string, opts?: RequestOptions) {
    return c.request<CatalogDomain>('GET', `/catalog/domains/${encodeURIComponent(idOf(urn))}`, undefined, opts);
}

/** An asset's terms (on it and on its columns) and domain, and what the person may change (with reasons). */
export interface AssetLabels {
    asset: string;
    terms: string[];
    columns: Record<string, string[]>;
    domain: string | null;
    may: { terms: boolean; column_terms: boolean; domain: boolean };
    why: { terms: string; column_terms: string; domain: string };
}

export function assetLabels(c: ConsumerClient, urn: string, opts?: RequestOptions) {
    return c.request<AssetLabels>('GET', `/catalog/assets/labels?urn=${encodeURIComponent(urn)}`, undefined, opts);
}

/** Puts a term on an asset (or one of its columns), or with `on` false takes it off. */
export function applyTerm(c: ConsumerClient, asset: string, term: string, field: string | null, on: boolean) {
    return c.request<{ changed: boolean }>('POST', on ? '/catalog/assets/terms' : '/catalog/assets/terms/remove',
        { asset, term, field: field || null });
}

/** Puts an asset in a domain, or with none takes it out. */
export function setAssetDomain(c: ConsumerClient, asset: string, domain: string | null) {
    return c.request<{ changed: boolean; domain: string | null }>('PUT', '/catalog/assets/domain', { asset, domain });
}
