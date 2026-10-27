import React from 'react';

import { useDheQuery } from '../api/hooks';
import type { EnrichmentMap, SemanticHit } from '../api/types';
import { useDheSession } from '../context/CairnProvider';
import { AssetRow } from './Assets';
import { Muted, SectionTitle, Surface } from './tokens';

/** Below this, a hit is more noise than meaning for the built-in embedder. */
export const MIN_SCORE = 0.15;

function MatchedTerms({ terms }: { terms?: string[] }) {
    if (!terms?.length) return null;
    return (
        <Muted style={{ display: 'block', fontSize: 12, padding: '0 14px 8px' }}>
            Matched: {terms.join(', ')}
        </Muted>
    );
}

/**
 * Assets close in meaning to the query that keyword search did not return (`exclude`): shown under
 * the keyword results, or instead of them when there are none.
 */
export function RelatedByMeaning({
    query,
    exclude,
    enrichment,
    limit = 5,
}: {
    query: string;
    exclude: string[];
    enrichment?: EnrichmentMap;
    limit?: number;
}) {
    const session = useDheSession();
    const enabled = session.feature('semantic_search', false) && query.trim().length > 1 && query.trim() !== '*';
    const key = enabled ? `/ai/search/semantic?${query}|${exclude.join('|')}` : null;
    const related = useDheQuery<{ results: SemanticHit[] }>(
        key,
        (c, signal) => c.semanticRelated(query, exclude, limit, MIN_SCORE, { signal }),
        { staleMs: 60_000 },
    );
    const hits = related.data?.results ?? [];
    if (!enabled || !hits.length) return null;
    return (
        <section aria-label="Related by meaning" style={{ marginTop: 20 }}>
            <SectionTitle style={{ fontSize: 15 }}>Related by meaning</SectionTitle>
            <Muted style={{ display: 'block', fontSize: 12, marginBottom: 6 }}>
                Close to what you asked even where the words differ.
            </Muted>
            {hits.map((h) => (
                <div key={h.urn}>
                    <AssetRow asset={h} enrichment={enrichment?.[h.urn]} />
                    <MatchedTerms terms={h.matched} />
                </div>
            ))}
        </section>
    );
}

/** "Similar assets" on an asset page: finds the certified copy of a table, or the other half of a join. */
export function SimilarAssets({ urn, limit = 5 }: { urn: string; limit?: number }) {
    const session = useDheSession();
    const enabled = session.feature('semantic_search', false);
    const similar = useDheQuery<{ results: SemanticHit[] }>(
        enabled ? `/ai/similar?${urn}` : null,
        (c, signal) => c.similarAssets(urn, limit, { signal }),
        { staleMs: 300_000 },
    );
    const hits = similar.data?.results ?? [];
    if (!enabled || !hits.length) return null;
    return (
        <Surface style={{ padding: 18, marginTop: 16 }} aria-label="Similar assets">
            <h3>Similar assets</h3>
            {hits.map((h) => (
                <AssetRow key={h.urn} asset={h} />
            ))}
        </Surface>
    );
}
