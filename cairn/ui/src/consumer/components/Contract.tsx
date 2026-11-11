import { Alert, Empty, Table, Tag } from 'antd';
import React from 'react';

import { CairnApiError } from '../../api/client';
import { useDheQuery } from '../api/hooks';
import type { Contract, ContractPart, ContractStatus } from '../api/types';
import { actorName, relativeTime } from '../utils/format';
import { Muted } from './tokens';
import { ErrorNotice } from './ErrorNotice';
import { StatusBadge, StatusTone } from './StatusBadge';

const TONE: Record<ContractStatus, StatusTone> = { MET: 'ok', BROKEN: 'error', PENDING: 'waiting' };
const WORD: Record<ContractStatus, string> = { MET: 'Met', BROKEN: 'Broken', PENDING: 'Pending' };
const PART: Record<ContractPart['part'], string> = {
    schema: 'Schema',
    freshness: 'Freshness',
    volume: 'Volume',
    quality: 'Quality',
};

/** The dataset's data contract: what the producer promises, and whether each part holds now. */
export function ContractPanel({ urn }: { urn: string }) {
    const q = useDheQuery<Contract>(`/quality/contracts/one?${urn}`, (c, signal) => c.contract(urn, { signal }));
    if (q.error instanceof CairnApiError && q.error.status === 404)
        return (
            <Empty
                description={
                    <>
                        No data contract. Producers publish one from a <code>contract.yaml</code> with{' '}
                        <code>cairn contract publish</code>, and check changes in CI with{' '}
                        <code>cairn contract check</code>.
                    </>
                }
            />
        );
    if (q.error) return <ErrorNotice error={q.error} />;
    const c = q.data;
    if (!c) return null;
    return (
        <section aria-label="Data contract">
            <p>
                <StatusBadge tone={c.status === 'MET' ? 'ok' : c.status === 'BROKEN' ? 'error' : 'waiting'}
                    label={c.status === 'MET' ? 'Met' : c.status === 'BROKEN' ? 'Broken' : 'Pending'} style={{ marginRight: 8 }} />
                Version <strong>{c.version}</strong>
                {c.owner && <> · owned by {actorName(c.owner)}</>}
                <Muted> · checked {relativeTime(new Date(c.checked_ms).toISOString())}</Muted>
            </p>
            <Table<ContractPart>
                size="small"
                rowKey={(p) => p.monitor_id ?? p.part}
                dataSource={c.parts}
                pagination={false}
                columns={[
                    { title: 'Promise', render: (_, p) => p.name?.replace(/ \(contract\)$/, '') ?? PART[p.part] },
                    {
                        title: 'Status',
                        dataIndex: 'status',
                        render: (s: ContractStatus) => <StatusBadge tone={TONE[s]} label={WORD[s]} />,
                    },
                    { title: 'Detail', dataIndex: 'detail' },
                ]}
            />
            {(c.versions?.length ?? 0) > 1 && (
                <Muted style={{ display: 'block', marginTop: 12 }}>
                    Versions:{' '}
                    {c
                        .versions!.map((v) => `${v.version} (${new Date(v.created_ms).toLocaleDateString()})`)
                        .join(' · ')}
                </Muted>
            )}
        </section>
    );
}
