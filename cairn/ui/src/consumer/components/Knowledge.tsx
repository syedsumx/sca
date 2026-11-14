import { List } from 'antd';
import React from 'react';

import { useDheQuery } from '../api/hooks';
import { Muted, SectionTitle, Surface } from './tokens';

const SOURCE: Record<string, string> = {
    confluence: 'Confluence',
    notion: 'Notion',
    gdocs: 'Google Docs',
    sharepoint: 'SharePoint',
};

/** Pages from the team's documentation that talk about this asset (knowledge sources). */
export function KnowledgeDocs({ urn }: { urn: string }) {
    const docs = useDheQuery(
        urn.startsWith('urn:li:dataset:') ? `/context/knowledge/for?urn=${urn}` : null,
        (c, signal) => c.knowledgeFor(urn, { signal }),
    );
    if (!docs.data?.length) return null;
    return (
        <Surface style={{ padding: 18, marginTop: 16 }}>
            <SectionTitle as="h2" style={{ fontSize: 16 }}>
                From your team&apos;s docs
            </SectionTitle>
            <List
                size="small"
                dataSource={docs.data}
                renderItem={(d) => (
                    <List.Item>
                        <a href={d.url} target="_blank" rel="noreferrer">
                            {d.title}
                        </a>
                        <Muted>
                            {SOURCE[d.source] ?? d.source} · {new Date(d.updated_ms).toLocaleDateString()}
                        </Muted>
                    </List.Item>
                )}
            />
        </Surface>
    );
}
