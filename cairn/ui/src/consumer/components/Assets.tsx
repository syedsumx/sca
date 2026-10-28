import React from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import type { Enrichment } from '../api/types';
import { entityTypeLabel, platformName } from '../utils/format';
import { BookmarkToggle } from './Engagement';
import { focusRing, tokens } from './tokens';
import { TrustBadges } from './TrustBadges';

export function assetPath(urn: string): string {
    return `/asset/${encodeURIComponent(urn)}`;
}

const Card = styled(Link)`
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    min-height: 150px;
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.md};
    box-shadow: ${tokens.shadow.card};
    color: ${tokens.color.text};
    transition: box-shadow 120ms ease, transform 120ms ease;
    ${focusRing}
    &:hover {
        box-shadow: ${tokens.shadow.hover};
        transform: translateY(-1px);
        color: ${tokens.color.text};
    }
`;

const Title = styled.div`
    font-weight: 600;
    font-size: 15px;
    display: flex;
    align-items: center;
    gap: 6px;
    overflow: hidden;
    text-overflow: ellipsis;
`;

const Meta = styled.div`
    font-size: 12px;
    color: ${tokens.color.textMuted};
`;

const Desc = styled.div`
    font-size: 13px;
    color: ${tokens.color.textMuted};
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    flex: 1;
`;

const Footer = styled.div`
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 12px;
    color: ${tokens.color.textMuted};
`;

export interface AssetSummary {
    urn: string;
    name: string;
    type: string;
    platform?: string | null;
    description?: string | null;
}

const TREND: Record<string, { icon: string; label: string }> = {
    up: { icon: '▲', label: 'Trending up' },
    new: { icon: '✦', label: 'New' },
    steady: { icon: '', label: '' },
};

export function AssetCard({
    asset,
    trend,
    views,
    certification,
    qualityStatus,
}: {
    asset: AssetSummary;
    trend?: 'up' | 'new' | 'steady';
    views?: number;
    certification?: string | null;
    qualityStatus?: string | null;
}) {
    const t = trend ? TREND[trend] : undefined;
    return (
        <Card to={assetPath(asset.urn)} aria-label={`${asset.name}, ${entityTypeLabel(asset.type)}`}>
            <Title>
                {t?.icon && (
                    <span title={t.label} aria-label={t.label} style={{ color: tokens.color.primary, fontSize: 11 }}>
                        {t.icon}
                    </span>
                )}
                {asset.name}
            </Title>
            <Meta>
                {entityTypeLabel(asset.type)}
                {asset.platform ? ` · ${platformName(asset.platform)}` : ''}
            </Meta>
            <Desc>{asset.description || 'No description yet.'}</Desc>
            <TrustBadges certification={certification} qualityStatus={qualityStatus} compact />
            <Footer>
                <span>{views !== undefined ? `👁 ${views.toLocaleString()} views` : ''}</span>
                <BookmarkToggle urn={asset.urn} compact />
            </Footer>
        </Card>
    );
}

const RowLink = styled(Link)<{ $selected?: boolean }>`
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 6px 16px;
    padding: 12px 14px;
    border-radius: ${tokens.radius.sm};
    color: ${tokens.color.text};
    background: ${(p) => (p.$selected ? tokens.color.primarySoft : 'transparent')};
    border: 1px solid ${(p) => (p.$selected ? tokens.color.primary : 'transparent')};
    ${focusRing}
    &:hover {
        background: ${tokens.color.surfaceAlt};
        color: ${tokens.color.text};
    }
`;

export function AssetRow({
    asset,
    enrichment,
    selected,
    rowRef,
}: {
    asset: AssetSummary;
    enrichment?: Enrichment;
    selected?: boolean;
    rowRef?: React.Ref<HTMLAnchorElement>;
}) {
    return (
        <RowLink
            to={assetPath(asset.urn)}
            $selected={selected}
            innerRef={rowRef as never}
            aria-current={selected ? 'true' : undefined}
            data-urn={asset.urn}
        >
            <div>
                <Title>{asset.name}</Title>
                <Meta>
                    {entityTypeLabel(asset.type)}
                    {asset.platform ? ` · ${platformName(asset.platform)}` : ''}
                </Meta>
                {asset.description && <Desc style={{ marginTop: 4 }}>{asset.description}</Desc>}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
                <TrustBadges
                    certification={enrichment?.certification}
                    qualityStatus={enrichment?.quality_status}
                    compliance={enrichment?.compliance}
                    contract={enrichment?.contract}
                    compact
                />
                <Meta>
                    {enrichment ? `👁 ${enrichment.views_7d}  💬 ${enrichment.threads_open}` : ''}
                </Meta>
                <BookmarkToggle urn={asset.urn} compact />
            </div>
        </RowLink>
    );
}
