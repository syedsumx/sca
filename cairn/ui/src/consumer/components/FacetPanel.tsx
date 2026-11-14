import { Checkbox, Input } from 'antd';
import React, { useState } from 'react';
import styled from 'styled-components';

import type { Facet } from '../api/types';
import { tokens } from './tokens';

const Group = styled.fieldset`
    border: 0;
    padding: 0;
    margin: 0 0 18px;
    legend {
        font-size: 12px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: ${tokens.color.textMuted};
        margin-bottom: 6px;
    }
`;

const Item = styled.label`
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 3px 0;
    font-size: 13px;
    cursor: pointer;
    .count {
        color: ${tokens.color.textMuted};
        font-variant-numeric: tabular-nums;
    }
`;

/** Business-friendly labels for Cairn Core facet fields. */
export const FACET_LABELS: Record<string, string> = {
    _entityType: 'Type',
    platform: 'Platform',
    domains: 'Domain',
    tags: 'Tags',
    glossaryTerms: 'Business terms',
    owners: 'Owner',
    origin: 'Environment',
    container: 'Container',
    typeNames: 'Sub-type',
};

const ORDER = ['_entityType', 'domains', 'platform', 'glossaryTerms', 'tags', 'owners', 'origin'];

export interface FacetPanelProps {
    facets: Facet[];
    selected: Record<string, string[]>;
    onToggle: (field: string, value: string) => void;
    maxValues?: number;
}

export function FacetPanel({ facets, selected, onToggle, maxValues = 8 }: FacetPanelProps) {
    const [filterText, setFilterText] = useState<Record<string, string>>({});
    const [expanded, setExpanded] = useState<Record<string, boolean>>({});
    const sorted = [...facets]
        .filter((f) => f.aggregations.length > 0)
        .sort((a, b) => {
            const ia = ORDER.indexOf(a.field);
            const ib = ORDER.indexOf(b.field);
            return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
        });
    return (
        <nav aria-label="Search filters">
            {sorted.map((facet) => {
                const q = (filterText[facet.field] || '').toLowerCase();
                const values = facet.aggregations.filter((a) => !q || a.label.toLowerCase().includes(q));
                const shown = expanded[facet.field] ? values : values.slice(0, maxValues);
                const label = FACET_LABELS[facet.field] ?? facet.displayName;
                return (
                    <Group key={facet.field}>
                        <legend>{label}</legend>
                        {facet.aggregations.length > maxValues && (
                            <Input
                                size="small"
                                placeholder={`Filter ${label.toLowerCase()}…`}
                                aria-label={`Filter ${label} values`}
                                value={filterText[facet.field] || ''}
                                onChange={(e) => setFilterText({ ...filterText, [facet.field]: e.target.value })}
                                style={{ marginBottom: 6 }}
                            />
                        )}
                        {shown.map((agg) => {
                            const checked = (selected[facet.field] || []).includes(agg.value);
                            return (
                                <Item key={agg.value}>
                                    <Checkbox checked={checked} onChange={() => onToggle(facet.field, agg.value)}>
                                        {agg.label}
                                    </Checkbox>
                                    <span className="count">{agg.count.toLocaleString()}</span>
                                </Item>
                            );
                        })}
                        {values.length > maxValues && (
                            <button
                                type="button"
                                style={{ border: 0, background: 'none', color: tokens.color.primary, padding: 0, cursor: 'pointer' }}
                                onClick={() => setExpanded({ ...expanded, [facet.field]: !expanded[facet.field] })}
                            >
                                {expanded[facet.field] ? 'Show less' : `Show ${values.length - maxValues} more`}
                            </button>
                        )}
                    </Group>
                );
            })}
        </nav>
    );
}
