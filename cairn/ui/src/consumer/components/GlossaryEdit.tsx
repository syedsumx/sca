import { Alert, Button, Input, Select, Space, Tag, message } from 'antd';
import React, { useEffect, useMemo, useState } from 'react';
import { useHistory } from 'react-router-dom';

import {
    ASSET_TYPES, AssetLabels, CatalogDomain, CatalogTerm, EditRights, applyTerm, assetLabels, catalogDomains,
    catalogTerms, setAssetDomain,
} from '../api/catalog';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import { useT } from '../i18n';
import { RESOURCE_SPECS } from '../resources/catalog';
import { ActionDialog } from '../resources/ResourceView';
import type { ResourceAction, ResourceSpec } from '../resources/types';
import { notifyError } from './ErrorNotice';
import { StatusBadge } from './StatusBadge';
import { Muted, Surface } from './tokens';

export const NODES = RESOURCE_SPECS['catalog-glossary-nodes'];
export const TERMS = RESOURCE_SPECS['catalog-glossary-terms'];
export const DOMAINS = RESOURCE_SPECS['catalog-domains'];

/** A spec's action: its create (POST without an id), edit (PUT), delete, or another POST by its path. */
export function specAction(spec: ResourceSpec, method: string, path?: string): ResourceAction | undefined {
    return spec.actions?.find((a) => a.method === method && (path ? a.path === path : method === 'POST' ? !/\{\w+\}/.test(a.path) : true));
}

/** The glossary's and domains' create, edit, delete and propose dialogs (the generated forms); after a change the
 *  Core reads are asked again (Core's search is fed asynchronously) and a deleted entry's page is left. */
export function useEditDialog(leaveTo: string) {
    const { cache } = useConsumerApi();
    const history = useHistory();
    const [open, setOpen] = useState<{ spec: ResourceSpec; action: ResourceAction; row: Record<string, unknown> | null } | null>(null);
    const dialog = open && (
        <ActionDialog spec={open.spec} action={open.action} row={open.row} onClose={() => setOpen(null)}
            startFromRow={open.action.path === '/catalog/glossary/proposals'}
            onDone={() => {
                for (const k of ['gql:glossary', 'gql:domain', 'gql:search', '/catalog/glossary', '/catalog/domains', '/catalog/assets']) cache.invalidate(k);
                if (open.action.method === 'DELETE') history.push(leaveTo);
            }} />
    );
    const show = (spec: ResourceSpec, action: ResourceAction | undefined, row: Record<string, unknown> | null = null) => {
        if (action) setOpen({ spec, action, row });
    };
    return { dialog, show };
}

/** A deprecated term, in words and a shape (not colour alone), with what to use instead. */
export function Deprecated({ note }: { note: string }) {
    const t = useT();
    return <StatusBadge tone="warn" label={note ? t('edit.deprecatedNote', { note }) : t('edit.deprecated')} state="deprecated" />;
}

/** Why the person sees the page read-only (the policy decision's reason), when they may not change it. */
export function ReadOnlyReason({ rights, what }: { rights: EditRights | undefined; what: 'glossary' | 'domain' }) {
    const t = useT();
    if (!rights || rights.may_manage) return null;
    return (
        <Muted style={{ display: 'block', marginTop: 4 }} data-read-only="">
            {t(what === 'glossary' ? 'edit.readOnlyGlossary' : 'edit.readOnlyDomain', { reason: rights.reason })}
        </Muted>
    );
}

/** A search over the catalog's assets (Cairn Core's search, as the person): picks one asset's URN. */
export function AssetSearch({ value, onChange, label }: { value: string | null; onChange: (urn: string | null) => void; label: string }) {
    const { client } = useConsumerApi();
    const [q, setQ] = useState('');
    const [options, setOptions] = useState<{ value: string; label: string }[]>([]);
    useEffect(() => {
        if (q.trim().length < 2) return undefined;
        const ctrl = new AbortController();
        const timer = setTimeout(() => {
            client.search({ query: q.trim(), filters: { _entityType: [...ASSET_TYPES] }, count: 10 }, ctrl.signal)
                .then((r) => setOptions(r.results.map((e) => ({ value: e.urn, label: `${e.name}${e.platform ? ` (${e.platform})` : ''}` }))))
                .catch(() => setOptions([]));
        }, 250);
        return () => {
            clearTimeout(timer);
            ctrl.abort();
        };
    }, [client, q]);
    return (
        <Select showSearch allowClear aria-label={label} placeholder={label} style={{ minWidth: 320 }} value={value ?? undefined}
            filterOption={false} onSearch={setQ} onChange={(v) => onChange((v as string | undefined) ?? null)} options={options}
            notFoundContent={null} />
    );
}

/** On a term's page: put the term on an asset, or on one of a dataset's columns. */
export function ApplyTerm({ term, deprecated }: { term: string; deprecated: boolean }) {
    const t = useT();
    const { client, cache } = useConsumerApi();
    const [asset, setAsset] = useState<string | null>(null);
    const [column, setColumn] = useState('');
    const [busy, setBusy] = useState(false);
    if (deprecated) return <Muted style={{ display: 'block', marginTop: 12 }}>{t('edit.deprecatedNoApply')}</Muted>;
    const apply = async () => {
        if (!asset) return;
        setBusy(true);
        try {
            const r = await applyTerm(client, asset, term, column.trim() || null, true);
            message.success(r.changed ? t('edit.applied') : t('edit.alreadySo'));
            setAsset(null);
            setColumn('');
            cache.invalidate('gql:search');
            cache.invalidate('/catalog/assets');
        } catch (e) {
            notifyError(e);
        } finally {
            setBusy(false);
        }
    };
    return (
        <Surface as="section" aria-label={t('edit.applyTitle')} style={{ padding: 16, marginTop: 16 }}>
            <h2 style={{ fontSize: 16, margin: '0 0 8px' }}>{t('edit.applyTitle')}</h2>
            <Space wrap>
                <AssetSearch value={asset} onChange={setAsset} label={t('edit.asset')} />
                <Input aria-label={t('edit.column')} placeholder={t('edit.column')} value={column}
                    onChange={(e) => setColumn(e.target.value)} style={{ width: 200 }} />
                <Button type="primary" disabled={!asset} loading={busy} onClick={() => void apply()}>{t('edit.apply')}</Button>
            </Space>
        </Surface>
    );
}

/** On a domain's page: put assets in the domain. */
export function AddToDomain({ domain }: { domain: string }) {
    const t = useT();
    const { client, cache } = useConsumerApi();
    const [asset, setAsset] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const add = async () => {
        if (!asset) return;
        setBusy(true);
        try {
            const r = await setAssetDomain(client, asset, domain);
            message.success(r.changed ? t('edit.added') : t('edit.alreadySo'));
            setAsset(null);
            cache.invalidate('gql:search');
            cache.invalidate('gql:domain');
            cache.invalidate('/catalog/assets');         // the asset's own domain picker
        } catch (e) {
            notifyError(e);
        } finally {
            setBusy(false);
        }
    };
    return (
        <Surface as="section" aria-label={t('edit.addAssets')} style={{ padding: 16, marginTop: 16 }}>
            <h2 style={{ fontSize: 16, margin: '0 0 8px' }}>{t('edit.addAssets')}</h2>
            <Space wrap>
                <AssetSearch value={asset} onChange={setAsset} label={t('edit.asset')} />
                <Button type="primary" disabled={!asset} loading={busy} onClick={() => void add()}>{t('edit.add')}</Button>
            </Space>
        </Surface>
    );
}

/** On an asset's page: its terms (on it and on a column) and its domain, changed here by people who may; the others
 *  see them read-only with the reason. */
export function AssetLabelsEditor({ urn }: { urn: string }) {
    const t = useT();
    const { client, cache } = useConsumerApi();
    const labels = useDheQuery<AssetLabels>(`/catalog/assets/labels?${urn}`, (c, signal) => assetLabels(c, urn, { signal }));
    const terms = useDheQuery<{ terms: CatalogTerm[] }>('/catalog/glossary/terms', (c, signal) => catalogTerms(c, { signal }), { staleMs: 60_000 });
    const domains = useDheQuery<{ domains: CatalogDomain[] }>('/catalog/domains', (c, signal) => catalogDomains(c, { signal }), { staleMs: 60_000 });
    const [term, setTerm] = useState<string | null>(null);
    const [column, setColumn] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const names = useMemo(() => new Map((terms.data?.terms ?? []).map((x) => [x.urn, x])), [terms.data]);
    const l = labels.data;
    if (!l) return labels.error ? <Alert type="warning" role="alert" message={String(labels.error)} /> : null;
    const columns = Object.keys(l.columns);
    // A real button that can be reached by keyboard and says which term it removes (the tag's own icon cannot).
    const removeButton = (name: string) => (
        <button type="button" aria-label={t('edit.removeTerm', { name })}
            style={{ border: 0, background: 'none', padding: '0 0 0 4px', cursor: 'pointer', color: 'inherit', font: 'inherit' }}>×</button>
    );
    const refresh = () => {
        cache.invalidate(`/catalog/assets/labels?${urn}`);
        cache.invalidate(`gql:asset:${urn}`);
        cache.invalidate('gql:search');
        cache.invalidate('gql:domain');                  // the Domains page's counts
    };
    const run = async (fn: () => Promise<unknown>) => {
        setBusy(true);
        try {
            await fn();
            refresh();
        } catch (e) {
            notifyError(e);
        } finally {
            setBusy(false);
        }
    };
    const termName = (u: string) => names.get(u)?.name ?? u.split(':').slice(3).join(':');
    const mayAdd = column ? l.may.column_terms : l.may.terms;
    return (
        <Surface as="section" aria-label={t('edit.labelsTitle')} style={{ padding: 18, marginTop: 16 }} data-asset-labels="">
            <h3 style={{ marginTop: 0 }}>{t('edit.labelsTitle')}</h3>
            <div role="list" aria-label={t('edit.termsOnAsset')} style={{ marginBottom: 8 }}>
                {l.terms.length === 0 && <Muted>{t('edit.noTerms')}</Muted>}
                {l.terms.map((u) => (
                    <span role="listitem" key={u}>
                        <Tag color="purple" closable={l.may.terms} closeIcon={l.may.terms ? removeButton(termName(u)) : false}
                            onClose={(e) => { e.preventDefault(); void run(() => applyTerm(client, urn, u, null, false)); }}>
                            {termName(u)}
                        </Tag>
                    </span>
                ))}
            </div>
            {Object.entries(l.columns).filter(([, v]) => v.length).map(([c, v]) => (
                <div key={c} style={{ marginBottom: 4 }}>
                    <code>{c}</code>:{' '}
                    {v.map((u) => (
                        <Tag key={u} closable={l.may.column_terms} closeIcon={l.may.column_terms ? removeButton(`${termName(u)} (${c})`) : false}
                            onClose={(e) => { e.preventDefault(); void run(() => applyTerm(client, urn, u, c, false)); }}>
                            {termName(u)}
                        </Tag>
                    ))}
                </div>
            ))}
            {(l.may.terms || l.may.column_terms) ? (
                <Space wrap style={{ marginTop: 8 }}>
                    <Select showSearch allowClear aria-label={t('edit.term')} placeholder={t('edit.term')} style={{ minWidth: 240 }}
                        value={term ?? undefined} onChange={(v) => setTerm((v as string | undefined) ?? null)} optionFilterProp="label"
                        options={(terms.data?.terms ?? []).filter((x) => !x.deprecated).map((x) => ({ value: x.urn, label: x.name }))} />
                    {columns.length > 0 && (
                        <Select allowClear aria-label={t('edit.column')} placeholder={t('edit.wholeAsset')} style={{ minWidth: 180 }}
                            value={column ?? undefined} onChange={(v) => setColumn((v as string | undefined) ?? null)}
                            options={columns.map((c) => ({ value: c, label: c, disabled: !l.may.column_terms }))} />
                    )}
                    <Button disabled={!term || !mayAdd} loading={busy}
                        onClick={() => term && void run(async () => {
                            await applyTerm(client, urn, term, column, true);
                            setTerm(null);
                        })}>{t('edit.addTerm')}</Button>
                </Space>
            ) : (
                <Muted style={{ display: 'block' }} data-read-only="">{l.why.terms}</Muted>
            )}
            <h3 style={{ marginTop: 16 }}>{t('domains.domain')}</h3>
            {l.may.domain ? (
                <Space wrap>
                    <Select allowClear showSearch aria-label={t('domains.domain')} placeholder={t('edit.noDomain')} style={{ minWidth: 240 }}
                        value={l.domain ?? undefined} optionFilterProp="label" disabled={busy}
                        onChange={(v) => void run(() => setAssetDomain(client, urn, (v as string | undefined) ?? null))}
                        options={(domains.data?.domains ?? []).map((d) => ({ value: d.urn, label: d.name }))} />
                </Space>
            ) : (
                <>
                    {l.domain ? <Tag>{(domains.data?.domains ?? []).find((d) => d.urn === l.domain)?.name ?? l.domain}</Tag>
                        : <Muted>{t('edit.noDomain')}</Muted>}
                    <Muted style={{ display: 'block' }} data-read-only="">{l.why.domain}</Muted>
                </>
            )}
            {labels.error && <Alert type="warning" message={String(labels.error)} />}
        </Surface>
    );
}
