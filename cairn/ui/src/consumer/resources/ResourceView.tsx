import { Alert, Button, Descriptions, Drawer, Empty, Input, Modal, Skeleton, Space, Table, Typography, message } from 'antd';
import React, { useEffect, useMemo, useState } from 'react';
import { useHistory, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import type { CairnApiError } from '../../api/client';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import { ApiDisclosure, RowActions } from '../components/RowActions';
import { ErrorNotice } from '../components/ErrorNotice';
import { tokens } from '../components/tokens';
import { useT } from '../i18n';
import { DetailPanel } from './DetailPanel';
import { columnTitle, formatValue, humanize, shortUrn } from './format';
import {
    FORMS, FieldValueError, FormSchema, Problem, Values, fillPath, formFor, initialValues, mapServerErrors, needsForm, toBody,
    validate,
} from './forms';
import { FormRenderer, FormState } from './FormRenderer';
import { CopyAsCode } from './IacPanel';
import type { ResourceAction, ResourceSpec } from './types';

export { formatValue, humanize } from './format';

type Row = Record<string, unknown>;

const Wrap = styled.section`
    .cairn-resource-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 14px;
    }
    .cairn-resource-head p {
        margin: 0;
        color: ${tokens.color.textMuted};
        max-width: 760px;
    }
    .ant-table-wrapper {
        background: ${tokens.color.surface};
        border: 1px solid ${tokens.color.border};
        border-radius: ${tokens.radius.md};
        overflow: hidden;
    }
    code.cairn-path {
        font-size: 12px;
        color: ${tokens.color.textMuted};
        background: ${tokens.color.surfaceAlt};
        padding: 1px 6px;
        border-radius: 6px;
    }
    .cairn-empty {
        background: ${tokens.color.surface};
        border: 1px dashed ${tokens.color.border};
        border-radius: ${tokens.radius.md};
        padding: 28px 24px;
        text-align: center;
    }
    .cairn-empty p {
        max-width: 620px;
        margin: 8px auto 16px;
        color: ${tokens.color.textMuted};
    }
`;

const isRowAction = (a: ResourceAction) => /\{\w+\}/.test(a.path) && a.scope !== 'page';

function rowKey(spec: ResourceSpec, row: Row, i: number): string {
    for (const k of [spec.rowKey, 'id', 'name', 'key', 'query_id', 'agent_id'].filter(Boolean) as string[]) {
        if (row[k] !== undefined && row[k] !== null) return String(row[k]);
    }
    return String(i);
}

function thingName(spec: ResourceSpec, row: Row | null): string {
    if (!row) return '';
    const first = spec.columns[0];
    const v = (first && row[first]) ?? row.name ?? row.title ?? row.id ?? row.key;
    return v === undefined || v === null ? '' : shortUrn(String(v));
}

function extract(spec: ResourceSpec, data: unknown): { rows: Row[]; summary: Row | null } {
    if (Array.isArray(data)) return { rows: data as Row[], summary: null };
    if (data && typeof data === 'object') {
        const obj = data as Row;
        if (spec.listKey && Array.isArray(obj[spec.listKey])) {
            const rest = { ...obj };
            delete rest[spec.listKey];
            return { rows: obj[spec.listKey] as Row[], summary: Object.keys(rest).length ? rest : null };
        }
        return { rows: [], summary: obj };
    }
    return { rows: [], summary: null };
}

function listOf(data: unknown, key: string): Row[] {
    if (Array.isArray(data)) return data as Row[];
    if (data && typeof data === 'object' && key && Array.isArray((data as Row)[key])) return (data as Row)[key] as Row[];
    return [];
}

/** The area's create action: the page-level one that makes a new item, else none. */
export function createAction(spec: ResourceSpec): ResourceAction | null {
    const page = (spec.actions ?? []).filter((a) => !isRowAction(a) && a.method !== 'DELETE');
    return page.find((a) => {
        const f = formFor(a);
        return f && (f.mode === 'create' || f.mode === 'upsert');
    }) ?? page.find((a) => /^(new|create|register|declare|define|deploy|list|start)\b/i.test(a.label)) ?? null;
}

/** The sensitive and write-only fields of the area's forms: masked in a row's details. */
function secretsOf(spec: ResourceSpec): string[] {
    const out = new Set<string>();
    for (const a of spec.actions ?? []) {
        for (const f of FORMS[`${a.method} ${a.path}`]?.fields ?? []) if (f.sensitive || f.write_only) out.add(f.read_as ?? f.name);
    }
    return [...out];
}

export function ActionDialog({ spec, action, row, onDone, onClose, startFromRow }: {
    spec: ResourceSpec; action: ResourceAction; row: Row | null; onDone: () => void; onClose: () => void;
    /** The form starts from the row's values (an action about the row, such as proposing a change to it). */
    startFromRow?: boolean;
}) {
    const t = useT();
    const { client } = useConsumerApi();
    const form: FormSchema | null = formFor(action);
    const excluded = needsForm(action) && !form;
    const rowAction = isRowAction(action);
    const name = thingName(spec, row);
    // An edit changes the row itself (or a setting); an upsert of something under the row (a scope's secret) is new.
    const ownKind = !form?.source.startsWith('registry:') || !spec.iac || form.source === `registry:${spec.iac.kind}`;
    const editing = !!form && (form.mode === 'update' || form.mode === 'upsert') && ownKind && (!!row || !!form.prefill?.singleton);
    const prefill = editing ? form?.prefill : undefined;
    const needRecord = !!prefill && (prefill.singleton || prefill.read.split('?')[0] !== spec.path || prefill.read.includes('?'));
    const rec = useDheQuery<unknown>(needRecord && prefill ? `prefill:${prefill.read}` : null, (c, signal) =>
        c.request<unknown>('GET', prefill!.read, undefined, { signal }));
    const record: Row | null = useMemo(() => {
        if (!prefill || !rec.data) return null;
        if (prefill.singleton) return rec.data as Row;
        const id = row?.[prefill.id];
        return listOf(rec.data, prefill.list_key).find((r) => id !== undefined && String(r[prefill.id]) === String(id)) ?? null;
    }, [prefill, rec.data, row]);
    const ready = !needRecord || rec.data !== undefined || rec.error !== undefined;
    const [state, setState] = useState<FormState | null>(form ? null : { values: {}, extra: {}, raw: null });
    const [initial, setInitial] = useState<Values | undefined>(undefined);
    useEffect(() => {
        if (!form || state || !ready) return;
        const start = initialValues(form, action, row, record, editing || (!!startFromRow && !!row));
        setInitial(start);
        setState({ values: start, extra: {}, raw: null });
    }, [form, state, ready, action, row, record, editing, startFromRow]);
    const [rawBody, setRawBody] = useState(action.body ? JSON.stringify(action.body, null, 2) : '');
    const [busy, setBusy] = useState(false);
    const [errors, setErrors] = useState<Record<string, Problem | string>>({});
    const [notice, setNotice] = useState<string | null>(null);
    const [apiError, setApiError] = useState<unknown>(null);
    const [answer, setAnswer] = useState<unknown>(undefined);

    const run = async () => {
        setNotice(null);
        setApiError(null);
        let body: unknown;
        let path: string;
        if (form && state) {
            if (state.raw !== null) {
                try {
                    body = state.raw.trim() ? JSON.parse(state.raw) : {};
                } catch {
                    setNotice(t('form.rawJsonInvalid', { error: '' }).replace(/:\s*$/, '.'));
                    return;
                }
            } else {
                const problems = validate(form, state.values);
                if (Object.keys(problems).length) {
                    setErrors(problems);
                    setNotice(t('form.fixErrors'));
                    return;
                }
                try {
                    body = toBody(form, state.values, state.extra, initial, action.method);
                } catch (e) {
                    if (e instanceof FieldValueError) {
                        setErrors({ [e.field]: { code: e.code } });
                        setNotice(t('form.fixErrors'));
                        return;
                    }
                    throw e;
                }
            }
            setErrors({});
            const filled = fillPath(action, form, state.values, row);
            if (filled.path === null) {
                setNotice(t('form.missingPath', { label: filled.missing }));
                return;
            }
            path = filled.path;
        } else {
            const filled = fillPath(action, null, {}, row);
            if (filled.path === null) {
                setNotice(t('form.missingPath', { label: filled.missing }));
                return;
            }
            path = filled.path;
            if (excluded) {
                try {
                    body = rawBody.trim() ? JSON.parse(rawBody) : undefined;
                } catch {
                    setNotice(t('form.rawJsonNotObject'));
                    return;
                }
            } else body = action.method === 'DELETE' ? undefined : action.body ?? undefined;
        }
        setBusy(true);
        try {
            const out = await client.request<unknown>(action.method, path, body);
            onDone();
            const keeps = form ? form.mode === 'action' : action.method !== 'DELETE' && !action.fixed;
            if (keeps && out !== undefined && out !== null && out !== '') {
                setAnswer(out);
            } else {
                message.success(action.method === 'DELETE' ? t('form.deleted', { name: name || spec.title }) : t('form.saved'));
                onClose();
            }
        } catch (e) {
            setApiError(e);
            if (form) {
                const err = e as CairnApiError;
                const mapped = mapServerErrors(form, err.status, err.body);
                if (Object.keys(mapped.fields).length) setErrors(mapped.fields);
            }
        } finally {
            setBusy(false);
        }
    };

    const title = action.method === 'DELETE' || !name ? action.label : `${action.label}: ${name}`;
    return (
        <Modal open title={title} onCancel={onClose} width={form ? 720 : 560} destroyOnClose
            footer={[
                <Button key="close" onClick={onClose}>{answer !== undefined ? t('form.close') : t('form.cancel')}</Button>,
                answer === undefined && (
                    <Button key="run" type="primary" danger={action.method === 'DELETE'} loading={busy} disabled={!!form && !state}
                        onClick={() => void run()}>
                        {action.label}
                    </Button>
                ),
            ]}>
            {action.method === 'DELETE' ? (
                <p>{name ? t('form.confirmDelete', { name }) : t('form.confirmDeleteThis')}</p>
            ) : form ? (
                state ? (
                    <FormRenderer form={form} action={action} state={state} setState={setState} errors={errors}
                        editing={editing} rowAction={rowAction} initial={initial} />
                ) : (
                    <Skeleton active paragraph={{ rows: 4 }} />
                )
            ) : excluded ? (
                <>
                    <Typography.Text type="secondary">{t('form.rawJsonLabel')}</Typography.Text>
                    <Input.TextArea aria-label={t('form.rawJsonLabel')} value={rawBody} onChange={(e) => setRawBody(e.target.value)}
                        autoSize={{ minRows: 3, maxRows: 14 }} style={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 12 }} />
                </>
            ) : (
                <p>{rowAction && name ? t('form.confirmAction', { action: action.label, name }) : t('form.confirmPage', { action: action.label })}</p>
            )}
            {notice && <Alert style={{ marginTop: 12 }} type="warning" showIcon message={notice} role="alert" />}
            {apiError !== null && (
                <ErrorNotice style={{ marginTop: 12 }} error={apiError} action={action.label} area={spec.title} docs={spec.docs} />
            )}
            {answer !== undefined && (
                <div style={{ marginTop: 12 }}>
                    <Alert type="success" showIcon message={t('form.answered')} style={{ marginBottom: 8 }} />
                    {answer && typeof answer === 'object' && !Array.isArray(answer)
                        ? <DetailPanel row={answer as Row} />
                        : <pre style={{ whiteSpace: 'pre-wrap', margin: 0, maxHeight: 280, overflow: 'auto', fontSize: 12 }}>
                            {typeof answer === 'string' ? answer : JSON.stringify(answer, null, 2)}
                        </pre>}
                </div>
            )}
        </Modal>
    );
}

/** One backend area as a table: its list endpoint, each row's details, and its actions as forms, all against the real API. */
export function ResourceView({ spec, extra, actions }: {
    spec: ResourceSpec;
    /** What a row's details add (a pipe's files, a stream's rejects). */
    extra?: (row: Row) => React.ReactNode;
    /** More page-level actions beside the area's own (a purpose-built form). */
    actions?: React.ReactNode;
}) {
    const t = useT();
    const q = useDheQuery<unknown>(spec.path ? `resource:${spec.path}` : null, (c, signal) =>
        c.request<unknown>('GET', spec.path as string, undefined, { signal }),
    );
    const [detail, setDetail] = useState<Row | null>(null);
    const [dialog, setDialog] = useState<{ action: ResourceAction; row: Row | null } | null>(null);
    const { rows, summary } = useMemo(() => extract(spec, q.data), [spec, q.data]);
    const rowActions = (spec.actions ?? []).filter(isRowAction);
    const pageActions = (spec.actions ?? []).filter((a) => !isRowAction(a));
    const create = createAction(spec);
    const secret = useMemo(() => secretsOf(spec), [spec]);
    const norm = (k: string, v: unknown) => (spec.seconds?.includes(k) && typeof v === 'number' ? v * 1000 : v);
    const show = (k: string, v: unknown, full = false) => formatValue(spec.seconds?.includes(k) ? `${k}_ms` : k, norm(k, v), full);
    const columnKeys = spec.columns.length ? spec.columns : Object.keys(rows[0] ?? {}).slice(0, 6);
    const docs = spec.docs ? `/docs/${spec.docs}` : null;
    const loading = q.loading && q.data === undefined && !q.error;
    const empty = !!spec.path && !loading && !q.error && rows.length === 0 && !summary;
    // `?action=<label>` (from the command palette) opens that page-level form once.
    const location = useLocation();
    const history = useHistory();
    useEffect(() => {
        const params = new URLSearchParams(location.search);
        const want = params.get('action');
        if (!want) return;
        const action = pageActions.find((a) => a.label === want);
        if (action) setDialog({ action, row: null });
        params.delete('action');
        history.replace({ pathname: location.pathname, search: params.toString() ? `?${params}` : '' });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [location.search]);

    const columns = [
        ...columnKeys.map((k, i) => ({
            title: columnTitle(k),
            dataIndex: k,
            key: k,
            render: (v: unknown, r: Row) =>
                i === 0 ? (
                    <a onClick={() => setDetail(r)} onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                                e.preventDefault();
                                setDetail(r);
                            }
                        }} role="button" tabIndex={0}
                        style={{ fontWeight: 600 }}>
                        {typeof v === 'string' || typeof v === 'number' ? shortUrn(String(v)) : show(k, v)}
                    </a>
                ) : secret.includes(k) && v ? '••••••' : show(k, v),
        })),
        ...(rowActions.length
            ? [{
                title: '',
                key: '__actions',
                align: 'right' as const,
                render: (_: unknown, r: Row) => {
                    // The main action stays visible; the rest, and every delete, is in the row's menu (L150).
                    const main = rowActions.find((x) => x.method !== 'DELETE');
                    const toItem = (x: ResourceAction) => ({ key: x.label, label: x.label, danger: x.method === 'DELETE',
                        confirm: false, onSelect: () => setDialog({ action: x, row: r }) });
                    return (
                        <RowActions name={thingName(spec, r)} primary={main ? [toItem(main)] : []}
                            actions={rowActions.filter((x) => x !== main).map(toItem)} />
                    );
                },
            }]
            : []),
    ];

    return (
        <Wrap aria-label={spec.title}>
            <div className="cairn-resource-head">
                <div>
                    <p>
                        {spec.description}
                        {docs && <> <a href={docs} target="_blank" rel="noreferrer">{t('empty.learnMore')}</a></>}
                    </p>
                    {spec.path && <ApiDisclosure paths={[`GET ${spec.path}`]} />}
                </div>
                <Space wrap>
                    {actions}
                    {pageActions.map((a) => (
                        <Button key={a.label} type={a === create ? 'primary' : 'default'} onClick={() => setDialog({ action: a, row: null })}>
                            {a.label}
                        </Button>
                    ))}
                    {spec.path && <Button onClick={() => q.refresh()} loading={q.loading}>{t('resource.refresh')}</Button>}
                </Space>
            </div>
            {!spec.path && <Alert type="info" showIcon message={spec.notes || t('resource.noList')} />}
            {q.error && <ErrorNotice style={{ marginBottom: 12 }} error={q.error} area={spec.title} docs={spec.docs} />}
            {loading && (
                <div role="status" aria-busy="true" aria-label={t('resource.loading', { area: spec.title })}>
                    <Skeleton active title={false} paragraph={{ rows: 5 }} />
                </div>
            )}
            {summary && (
                <Descriptions bordered size="small" column={{ xs: 1, md: 2, lg: 3 }} style={{ marginBottom: 12, background: tokens.color.surface }}>
                    {Object.entries(summary).map(([k, v]) => (
                        <Descriptions.Item key={k} label={humanize(k)}>{show(k, v)}</Descriptions.Item>
                    ))}
                </Descriptions>
            )}
            {empty && (
                <div className="cairn-empty" role="region" aria-label={t('empty.title', { area: spec.title })}>
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<strong>{t('empty.title', { area: spec.title })}</strong>} />
                    <p>{spec.description}</p>
                    <Space>
                        {create && <Button type="primary" onClick={() => setDialog({ action: create, row: null })}>{create.label}</Button>}
                        {docs && <a href={docs} target="_blank" rel="noreferrer">{t('empty.learnMore')}</a>}
                    </Space>
                </div>
            )}
            {spec.path && !loading && rows.length > 0 && (
                <Table<Row>
                    size="middle"
                    dataSource={rows}
                    rowKey={(r) => rowKey(spec, r, rows.indexOf(r))}
                    columns={columns}
                    pagination={rows.length > 15 ? { pageSize: 15, showSizeChanger: false } : false}
                    scroll={{ x: 'max-content' }}
                    onRow={(r) => ({ onDoubleClick: () => setDetail(r) })}
                />
            )}
            <Drawer open={!!detail} width={600} onClose={() => setDetail(null)}
                title={detail ? shortUrn(String(detail[columnKeys[0]] ?? spec.title)) : ''}>
                {detail && (
                    <>
                        <DetailPanel row={detail} secret={secret} format={show} />
                        {extra && <div style={{ marginTop: 16 }}>{extra(detail)}</div>}
                        {rowActions.length > 0 && (
                            <Space style={{ marginTop: 16 }} wrap>
                                {rowActions.map((a) => (
                                    <Button key={a.label} danger={a.method === 'DELETE'} onClick={() => setDialog({ action: a, row: detail })}>
                                        {a.label}
                                    </Button>
                                ))}
                            </Space>
                        )}
                        {spec.iac && detail[spec.iac.id ?? 'id'] !== undefined && detail[spec.iac.id ?? 'id'] !== null && (
                            <div style={{ marginTop: 16 }}>
                                <CopyAsCode kind={spec.iac.kind} id={String(detail[spec.iac.id ?? 'id'])} />
                            </div>
                        )}
                    </>
                )}
            </Drawer>
            {dialog && (
                <ActionDialog spec={spec} action={dialog.action} row={dialog.row} onClose={() => setDialog(null)}
                    onDone={() => {
                        if (dialog.action.method === 'DELETE' && detail) setDetail(null);
                        void q.refresh();
                    }} />
            )}
        </Wrap>
    );
}
