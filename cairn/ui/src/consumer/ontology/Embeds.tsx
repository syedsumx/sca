/** The ontology elsewhere in the product: business terms on an asset page, terms among search results, and what an
 * Ask-your-data answer was grounded in. Each stays quiet when Cairn Ontology is not configured or has nothing. */

import { Table, Tag } from 'antd';
import React from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import { useDheQuery } from '../api/hooks';
import { Muted, tokens } from '../components/tokens';
import { AssetTerms, Grounded, TermHit, cardinalityLabel, ontologyApi, tableLabel, termHref } from './api';
import { StatusTag } from './TermDetail';

const Box = styled.section`
    margin-top: 16px;
    padding: 18px;
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.md};
    background: ${tokens.color.surface};
    h3 {
        margin: 0 0 8px;
    }
`;

/** "Business terms" on an asset page: the table's terms and the term of each column, linked to the term pages. */
export function BusinessTerms({ urn }: { urn: string }) {
    const q = useDheQuery<AssetTerms>(urn.startsWith('urn:li:dataset:') ? `ontology:asset:${urn}` : null,
        (c) => ontologyApi(c).asset(urn), { staleMs: 30_000 });
    const a = q.data;
    if (!a || (!a.terms.length && !a.columns.length && !a.bridge)) return null;
    return (
        <Box aria-label="Business terms" data-testid="business-terms">
            <h3>Business terms</h3>
            {a.bridge && <p><Tag color="orange">bridge table</Tag><Muted>It joins other tables; every column is a foreign key.</Muted></p>}
            <div>
                {a.terms.map((t) => (
                    <span key={t.id} style={{ marginRight: 12 }}>
                        <Link to={termHref(t.id)}><strong>{t.name}</strong></Link> <StatusTag status={t.status} />
                        {t.synonyms.length > 0 && <Muted>also {t.synonyms.join(', ')}</Muted>}
                    </span>
                ))}
            </div>
            {a.columns.length > 0 && (
                <Table
                    size="small"
                    style={{ marginTop: 8 }}
                    rowKey="column"
                    pagination={false}
                    dataSource={a.columns}
                    columns={[
                        { title: 'Column', dataIndex: 'column', render: (c: string) => <code>{c}</code> },
                        { title: 'Business name', dataIndex: 'name' },
                        { title: 'Term', dataIndex: 'term_name', render: (n: string, r) => <Link to={termHref(r.term)}>{n}</Link> },
                        { title: 'Date format', dataIndex: 'format', render: (f: string | null) => (f ? <Tag>{f}</Tag> : '') },
                    ]}
                />
            )}
            {a.relationships.length > 0 && (
                <div style={{ marginTop: 8 }}>
                    <Muted>Joins: </Muted>
                    {a.relationships.map((r) => (
                        <Tag key={r.id}>
                            {r.column} → {r.other_term ? r.other_term.name : tableLabel(r.other_table)}.{r.other_column} ({cardinalityLabel(r.cardinality)})
                        </Tag>
                    ))}
                </div>
            )}
        </Box>
    );
}

/** Terms matching a search (by name or synonym), shown above the asset results with a "Term" badge. */
export function TermSearchResults({ query }: { query: string }) {
    const q = useDheQuery<{ terms: TermHit[] }>(query.trim() ? `ontology:search:${query}` : null,
        (c) => ontologyApi(c).searchTerms(query, 5), { staleMs: 30_000 });
    const hits = q.data?.terms ?? [];
    if (!hits.length) return null;
    return (
        <section aria-label="Business terms matching the search" data-testid="term-results" style={{ marginBottom: 8 }}>
            {hits.map((h) => (
                <div key={h.id} style={{ padding: '10px 12px', borderBottom: `1px solid ${tokens.color.border}` }}>
                    <Tag color="purple">Term</Tag>
                    <Link to={termHref(h.id)}><strong>{h.name}</strong></Link>
                    {h.matched === 'synonym' && h.synonym && <Muted> (synonym “{h.synonym}”)</Muted>}
                    <div><Muted style={{ fontSize: 12 }}>{h.description} · {h.table_name}</Muted></div>
                </div>
            ))}
        </section>
    );
}

/** "Grounded in" chips on an answer: the terms and relationships of the ontology it used. */
export function GroundedChips({ grounded }: { grounded?: Grounded | null }) {
    if (!grounded || (!grounded.terms.length && !grounded.relationships.length)) return null;
    return (
        <span data-testid="grounded-in">
            <Muted>Grounded in: </Muted>
            {grounded.terms.map((t) => (
                <Link key={t.id} to={termHref(t.id)}><Tag color="purple">{t.name}</Tag></Link>
            ))}
            {grounded.relationships.map((r) => (
                <Tag key={r.id} color="geekblue" title="A relationship from the ontology">{r.label}</Tag>
            ))}
        </span>
    );
}
