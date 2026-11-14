import React from 'react';
import styled from 'styled-components';

import { TONE_COLORS, qualityLabel, qualityTone, relativeTime } from '../utils/format';
import { tokens } from './tokens';

const Row = styled.div`
    display: inline-flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
`;

const Pill = styled.span<{ fg: string }>`
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    line-height: 18px;
    padding: 1px 8px;
    border-radius: 999px;
    color: ${(p) => p.fg};
    background: ${(p) => `${p.fg}14`};
    border: 1px solid ${(p) => `${p.fg}33`};
    white-space: nowrap;
`;

const Dot = styled.span<{ fg: string }>`
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: ${(p) => p.fg};
`;

export interface TrustBadgesProps {
    certification?: string | null;
    qualityStatus?: string | null;
    sensitive?: boolean;
    lastUpdated?: string | null;
    compact?: boolean;
    compliance?: 'COMPLIANT' | 'INCOMPLETE' | null;
    contract?: 'MET' | 'BROKEN' | 'PENDING' | null;
}

const CONTRACT = {
    MET: { label: 'Contract met', icon: '🤝', title: 'Every part of the data contract holds' },
    BROKEN: { label: 'Contract broken', icon: '⚠', title: 'The producer is not keeping the data contract' },
    PENDING: { label: 'Contract pending', icon: '🤝', title: 'Some contract checks have not reported yet' },
};

/** Same compact trust language on every card, row, embed and chat answer (spec principle 3). */
export function TrustBadges({ certification, qualityStatus, sensitive, lastUpdated, compact, compliance, contract }: TrustBadgesProps) {
    const tone = qualityTone(qualityStatus);
    const certified = (certification || '').toUpperCase() === 'CERTIFIED';
    return (
        <Row aria-label="Trust signals">
            {certified && (
                <Pill fg={tokens.color.good} title="Certified by a data steward">
                    <span aria-hidden>✔</span>
                    {!compact && 'Certified'}
                    {compact && <span className="sr-only">Certified</span>}
                </Pill>
            )}
            <Pill fg={TONE_COLORS[tone]} title={`Data quality: ${qualityLabel(qualityStatus)}`}>
                <Dot fg={TONE_COLORS[tone]} aria-hidden />
                {qualityLabel(qualityStatus)}
            </Pill>
            {compliance === 'COMPLIANT' && (
                <Pill fg={tokens.color.good} title="Every compliance form for this asset is complete">
                    <span aria-hidden>☑</span>
                    {!compact && 'Compliant'}
                    {compact && <span className="sr-only">Compliant</span>}
                </Pill>
            )}
            {compliance === 'INCOMPLETE' && (
                <Pill fg={tokens.color.warn} title="Compliance forms for this asset are still open">
                    <span aria-hidden>☐</span>
                    {!compact && 'Forms open'}
                    {compact && <span className="sr-only">Forms open</span>}
                </Pill>
            )}
            {contract && (
                <Pill
                    fg={contract === 'MET' ? tokens.color.good : contract === 'BROKEN' ? tokens.color.bad : tokens.color.textMuted}
                    title={CONTRACT[contract].title}
                >
                    <span aria-hidden>{CONTRACT[contract].icon}</span>
                    {!compact && CONTRACT[contract].label}
                    {compact && <span className="sr-only">{CONTRACT[contract].label}</span>}
                </Pill>
            )}
            {sensitive && (
                <Pill fg={tokens.color.warn} title="Contains personal data">
                    <span aria-hidden>🔒</span>PII
                </Pill>
            )}
            {lastUpdated && !compact && <Pill fg={tokens.color.textMuted}>⏱ {relativeTime(lastUpdated)}</Pill>}
        </Row>
    );
}

export function ConfidenceMeter({ value, label }: { value: number; label?: string }) {
    const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
    const color = pct >= 80 ? tokens.color.good : pct >= 50 ? tokens.color.warn : tokens.color.bad;
    return (
        <span
            role="meter"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={pct}
            aria-label={label ?? 'Confidence'}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12 }}
        >
            <span style={{ width: 64, height: 6, background: '#eee', borderRadius: 3, overflow: 'hidden' }}>
                <span style={{ display: 'block', width: `${pct}%`, height: '100%', background: color }} />
            </span>
            {pct}%
        </span>
    );
}
