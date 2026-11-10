import React from 'react';
import styled from 'styled-components';

import { CairnApiError } from '../../api/client';
import { useDheQuery } from '../api/hooks';
import type { DataResult } from '../api/types';
import { useDheSession } from '../context/CairnProvider';
import { MessageKey, useT } from '../i18n';
import type { CheckAnswer } from '../pages/PeoplePage';
import { tokens } from './tokens';

const Aside = styled.aside`
    flex: 1 1 300px;
    min-width: 0;
    align-self: flex-start;
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.lg};
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    h2 {
        margin: 0;
        font-size: 16px;
        font-weight: 600;
    }
    ul {
        list-style: none;
        margin: 0;
        padding: 0;
        display: grid;
        gap: 12px;
    }
    li {
        display: flex;
        gap: 10px;
        align-items: flex-start;
    }
    .kind {
        flex: none;
        font-size: 11px;
        font-weight: 700;
        padding: 3px 7px;
        border-radius: 5px;
        letter-spacing: 0.02em;
    }
    .what {
        display: flex;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
        font-size: 14px;
        font-weight: 500;
        overflow-wrap: anywhere;
    }
    .what span {
        font-size: 13px;
        font-weight: 400;
        color: ${tokens.color.textMuted};
        line-height: 1.4;
    }
`;

type Kind = 'allow' | 'mask' | 'filter' | 'audit';
const KIND: Record<Kind, { label: MessageKey; bg: string; fg: string }> = {
    allow: { label: 'why.allow', bg: tokens.status.ok.bg, fg: tokens.status.ok.fg },
    filter: { label: 'why.filter', bg: tokens.status.running.bg, fg: tokens.status.running.fg },
    mask: { label: 'why.mask', bg: tokens.status.warn.bg, fg: tokens.status.warn.fg },
    audit: { label: 'why.audit', bg: tokens.status.waiting.bg, fg: tokens.status.waiting.fg },
};

/** The person's own user id from their URN (`urn:li:corpuser:ana` → `ana`). */
export function userId(urn: string | undefined): string {
    return (urn ?? '').replace(/^urn:li:corpuser:/, '');
}

/**
 * "Why you see this", beside a governed preview: what the preview itself says (masked columns, or rows withheld),
 * and, for someone who may run access checks, the policies that decide their own access. Nothing is assumed: a
 * line appears only when Cairn answered it.
 */
export function WhyPanel({ urn }: { urn: string }) {
    const t = useT();
    const session = useDheSession();
    const me = userId(session.config?.user?.urn);
    const preview = useDheQuery<DataResult>(`/data/preview?${urn}`, (c, signal) => c.previewData(urn, { signal }));
    // The access check explains a decision; it is for administrators (MANAGE_POLICIES), so others simply do not see it.
    const check = useDheQuery<CheckAnswer | null>(`why:${me}:${urn}`, (c, signal) => (me
        ? c.request<CheckAnswer>('POST', '/v1/people/access-check', { person: me, privilege: 'CAIRN_DATA_ACCESS', resource: urn },
            { signal })
        : Promise.resolve(null)), { staleMs: 60_000 });
    const denied = preview.error instanceof CairnApiError && preview.error.status === 403;
    const masked = preview.data?.masked_columns ?? [];
    const rows: { kind: Kind; name: string; what: string }[] = [];
    for (const b of check.data?.allowed ? check.data.because : []) {
        rows.push({ kind: 'allow', name: t('why.allowedBy', { name: `${b.kind} ${b.name}` }), what: b.description ?? b.policy_id });
    }
    for (const o of check.data?.obligations ?? []) {
        if (o.type === 'row_filter') {
            rows.push({ kind: 'filter', name: t('why.rowFilter'),
                what: typeof o.params?.expression === 'string' ? o.params.expression : '' });
        }
    }
    if (denied) rows.push({ kind: 'filter', name: t('why.denied'), what: '' });
    else if (preview.data) {
        rows.push(masked.length
            ? { kind: 'mask', name: t('why.masked', { cols: masked.join(', ') }), what: t('why.maskedHint') }
            : { kind: 'mask', name: t('why.noMask'), what: '' });
    }
    rows.push({ kind: 'audit', name: t('why.recorded'), what: t('why.recordedHint') });
    return (
        <Aside aria-labelledby="why-h">
            <h2 id="why-h">{t('why.title')}</h2>
            <ul>
                {rows.map((r, i) => (
                    // eslint-disable-next-line react/no-array-index-key
                    <li key={i}>
                        <span className="kind" style={{ background: KIND[r.kind].bg, color: KIND[r.kind].fg }}>{t(KIND[r.kind].label)}</span>
                        <span className="what">{r.name}{r.what && <span>{r.what}</span>}</span>
                    </li>
                ))}
            </ul>
        </Aside>
    );
}
