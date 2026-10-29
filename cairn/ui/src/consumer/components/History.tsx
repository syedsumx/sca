import { Alert, Button, Empty, Popconfirm, Space, Tag, Timeline, message } from 'antd';
import React, { useState } from 'react';

import type { CairnApiError } from '../../api/client';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { FieldChange, MetadataChange } from '../api/types';
import { relativeTime } from '../utils/format';
import { Muted, Surface } from './tokens';

const ASPECT_LABEL: Record<string, string> = {
    globalTags: 'Tags',
    glossaryTerms: 'Glossary terms',
    ownership: 'Owners',
    domains: 'Domain',
    deprecation: 'Deprecation',
    institutionalMemory: 'Links',
    editableSchemaMetadata: 'Column documentation',
    editableDatasetProperties: 'Description',
    structuredProperties: 'Properties',
    upstreamLineage: 'Lineage',
};
const OP_COLOR: Record<FieldChange['op'], string> = { added: 'green', removed: 'red', changed: 'blue' };

const short = (v: unknown): string => {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'string') return v.replace(/^urn:li:[A-Za-z]+:/, '');
    if (typeof v === 'object') {
        const o = v as Record<string, unknown>;
        const id = ['fieldPath', 'tag', 'urn', 'owner', 'url', 'dataset', 'propertyUrn'].find((k) => k in o);
        if (id) return short(o[id]);
        return JSON.stringify(v);
    }
    return String(v);
};

function Diff({ changes }: { changes: FieldChange[] }) {
    return (
        <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
            {changes.map((c) => (
                <li key={`${c.field}-${c.op}-${short(c.before)}-${short(c.after)}`}>
                    <Tag color={OP_COLOR[c.op]}>{c.op}</Tag>
                    <code>{c.field}</code>{' '}
                    {c.op === 'changed' ? (
                        <>
                            <del>{short(c.before)}</del> → <ins>{short(c.after)}</ins>
                        </>
                    ) : (
                        short(c.op === 'added' ? c.after : c.before)
                    )}
                </li>
            ))}
        </ul>
    );
}

/** Asset → History: every governed metadata change, its diff, and a revert for people who may edit. */
export function AssetHistory({ urn }: { urn: string }) {
    const { client } = useConsumerApi();
    const history = useDheQuery(`/history?urn=${urn}`, (c, signal) => c.history(urn, { signal }));
    const [conflict, setConflict] = useState<string | null>(null);
    const revert = async (id: string, force = false) => {
        try {
            await client.revertChange(id, force);
            message.success('Reverted');
            setConflict(null);
            history.refresh();
        } catch (e) {
            const err = e as CairnApiError;
            const detail = String((err.body as { detail?: string } | undefined)?.detail ?? err.message);
            if (err.status === 409 && detail.includes('changed since')) setConflict(id);
            else message.error(detail);
        }
    };
    const rows: MetadataChange[] = history.data ?? [];
    return (
        <Surface style={{ padding: 18 }}>
            <Muted>
                Changes to descriptions, columns, tags, terms, owners, domain, links and lineage, however they were
                made.
            </Muted>
            {conflict && (
                <Alert
                    style={{ marginTop: 12 }}
                    type="warning"
                    showIcon
                    message="This has changed again since. Revert the later change first, or overwrite it."
                    action={
                        <Button size="small" danger onClick={() => void revert(conflict, true)}>
                            Overwrite
                        </Button>
                    }
                />
            )}
            {rows.length === 0 && !history.loading ? (
                <Empty style={{ marginTop: 16 }} description="No changes recorded yet." />
            ) : (
                <Timeline style={{ marginTop: 16 }}>
                    {rows.map((r) => (
                        <Timeline.Item key={r.id} color={r.revert_of ? 'gray' : r.reverted_by ? 'red' : 'blue'}>
                            <Space wrap size={6}>
                                <strong>{ASPECT_LABEL[r.aspect] ?? r.aspect}</strong>
                                <span>{short(r.actor)}</span>
                                <Muted>{relativeTime(new Date(r.ts_ms).toISOString())}</Muted>
                                {r.revert_of && <Tag>revert</Tag>}
                                {r.reverted_by && <Tag color="red">reverted</Tag>}
                                {r.revertible && !r.reverted_by && !r.revert_of && (
                                    <Popconfirm
                                        title="Put back what was there before this change?"
                                        onConfirm={() => void revert(r.id)}
                                    >
                                        <Button size="small" aria-label={`Revert ${r.summary}`}>
                                            Revert
                                        </Button>
                                    </Popconfirm>
                                )}
                            </Space>
                            <Diff changes={r.changes} />
                        </Timeline.Item>
                    ))}
                </Timeline>
            )}
        </Surface>
    );
}
