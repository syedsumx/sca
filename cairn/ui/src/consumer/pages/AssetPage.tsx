import { Alert, Button, Empty, Table, Tabs, Tag, Timeline, message } from 'antd';
import React, { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import styled from 'styled-components';

import type { DatasetHealth } from '../../api/types';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { EnrichmentMap, ExplainResponse } from '../api/types';
import { DiscussionPanel } from '../components/Discussion';
import { BookmarkToggle, FollowButton } from '../components/Engagement';
import { SimilarAssets } from '../components/Semantic';
import { Muted, Page, Skeleton, Surface, tokens } from '../components/tokens';
import { TrustBadges } from '../components/TrustBadges';
import { useDheSession } from '../context/CairnProvider';
import { useCoreHref } from '../context/NativeLinks';
import { actorName, entityTypeLabel, platformName, relativeTime, shortUrn } from '../utils/format';
import { MarkdownView } from '../utils/markdown';

export const ASSET_QUERY = `
query cairnConsumerAsset($urn: String!) {
  entity(urn: $urn) {
    urn
    type
    ... on Dataset {
      name
      platform { name }
      properties { name description }
      editableProperties { description }
      domain { domain { urn properties { name } } }
      ownership { owners { owner { ... on CorpUser { urn username } ... on CorpGroup { urn name } } ownershipType { info { name } } } }
      glossaryTerms { terms { term { urn properties { name } } } }
      tags { tags { tag { urn properties { name } } } }
      schemaMetadata(version: 0) {
        fields { fieldPath nativeDataType description glossaryTerms { terms { term { urn } } } }
      }
      editableSchemaMetadata {
        editableSchemaFieldInfo { fieldPath description glossaryTerms { terms { term { urn } } } }
      }
    }
    ... on Dashboard { platform { name } properties { name description } }
    ... on Chart { platform { name } properties { name description } }
    ... on DataProduct { properties { name description } }
    ... on GlossaryTerm { properties { name description } }
  }
}`;

interface Term {
    term: { urn: string; properties?: { name?: string } | null };
}

export interface AssetDetails {
    urn: string;
    type: string;
    name?: string;
    platform?: { name: string } | null;
    properties?: { name?: string; description?: string } | null;
    editableProperties?: { description?: string } | null;
    domain?: { domain: { urn: string; properties?: { name?: string } } } | null;
    ownership?: {
        owners: { owner: { urn: string; username?: string; name?: string }; ownershipType?: { info?: { name?: string } } | null }[];
    } | null;
    glossaryTerms?: { terms: Term[] } | null;
    tags?: { tags: { tag: { urn: string; properties?: { name?: string } | null } }[] } | null;
    schemaMetadata?: {
        fields: { fieldPath: string; nativeDataType?: string; description?: string; glossaryTerms?: { terms: Term[] } | null }[];
    } | null;
    editableSchemaMetadata?: {
        editableSchemaFieldInfo: { fieldPath: string; description?: string; glossaryTerms?: { terms: Term[] } | null }[];
    } | null;
}

export interface ColumnRow {
    fieldPath: string;
    type: string;
    description: string;
    classifications: string[];
}

/** Merge ingested + editable schema info; editable (human/AI-curated) wins. */
export function mergeColumns(d: AssetDetails | undefined): ColumnRow[] {
    const editable = new Map((d?.editableSchemaMetadata?.editableSchemaFieldInfo ?? []).map((f) => [f.fieldPath, f]));
    return (d?.schemaMetadata?.fields ?? []).map((f) => {
        const e = editable.get(f.fieldPath);
        const terms = [...(f.glossaryTerms?.terms ?? []), ...(e?.glossaryTerms?.terms ?? [])].map((t) => t.term.urn);
        return {
            fieldPath: f.fieldPath,
            type: f.nativeDataType ?? '',
            description: e?.description || f.description || '',
            classifications: Array.from(new Set(terms.filter((t) => t.includes('Classification.')).map((t) => t.replace(/^urn:li:glossaryTerm:Classification\./, '')))),
        };
    });
}

const Header = styled(Surface)`
    padding: 18px 20px;
    margin-top: 16px;
    h1 {
        margin: 0;
        font-size: 24px;
        font-weight: 600;
    }
`;

export function AssetPage() {
    const { urn: rawUrn } = useParams<{ urn: string }>();
    const urn = decodeURIComponent(rawUrn);
    const { client } = useConsumerApi();
    const session = useDheSession();
    const me = session.config?.user.urn;
    const [tab, setTab] = useState('summary');
    const details = useDheQuery<AssetDetails | null>(`gql:asset:${urn}`, async (c, signal) => {
        const data = await c.gql<{ entity: AssetDetails | null }>(ASSET_QUERY, { urn }, signal);
        return data.entity;
    });
    const enrichment = useDheQuery<EnrichmentMap>(`/collab/enrich?${urn}`, (c, signal) => c.enrich([urn], { signal }));
    const health = useDheQuery<DatasetHealth>(
        urn.startsWith('urn:li:dataset:') ? `/quality/datasets/${urn}/health` : null,
        (c, signal) => c.getDatasetHealth(urn, { signal }),
    );
    const [explain, setExplain] = useState<ExplainResponse | null>(null);
    const [explaining, setExplaining] = useState(false);

    useEffect(() => {
        client.recordView(urn).catch(() => undefined);
    }, [client, urn]);

    const catalogHref = useCoreHref(urn);
    const d = details.data;
    const e = enrichment.data?.[urn];
    const name = d?.properties?.name || d?.name || shortUrn(urn);
    const description = d?.editableProperties?.description || d?.properties?.description || '';
    const columns = mergeColumns(d ?? undefined);
    const sensitive = columns.some((c) => c.classifications.some((x) => x.startsWith('PII')));

    const runExplain = async () => {
        setExplaining(true);
        try {
            setExplain(await client.explainLineage(urn));
        } catch (err) {
            message.error(`Explanation unavailable: ${(err as Error).message}`);
        } finally {
            setExplaining(false);
        }
    };

    if (details.loading) {
        return (
            <Page>
                <Header>
                    <Skeleton h={24} w="40%" />
                    <div style={{ height: 10 }} />
                    <Skeleton h={14} w="60%" />
                </Header>
            </Page>
        );
    }
    if (details.error || d === null) {
        return (
            <Page>
                <Alert
                    type={d === null ? 'warning' : 'error'}
                    message={d === null ? 'Asset not found or you do not have access to it.' : `Could not load asset: ${details.error?.message}`}
                    action={<Link to={`/search?q=${encodeURIComponent(shortUrn(urn))}`}>Search instead</Link>}
                />
            </Page>
        );
    }

    return (
        <Page>
            <Header>
                <Muted style={{ fontSize: 12 }}>
                    {entityTypeLabel(d?.type)}
                    {d?.platform?.name ? ` · ${platformName(d.platform.name)}` : ''}
                    {d?.domain?.domain.properties?.name ? ` · ${d.domain.domain.properties.name}` : ''}
                </Muted>
                <h1>{name}</h1>
                <div style={{ margin: '10px 0' }}>
                    <TrustBadges
                        certification={e?.certification}
                        qualityStatus={health.data?.status ?? e?.quality_status}
                        sensitive={sensitive}
                    />
                </div>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    <BookmarkToggle urn={urn} />
                    <FollowButton urn={urn} />
                    <Link to={`/marketplace?q=${encodeURIComponent(name)}`}>
                        <Button>Request access</Button>
                    </Link>
                    <Button onClick={() => session.setAssistantOpen(true)}>✨ Ask about this asset</Button>
                    {catalogHref && (
                        <a href={catalogHref}>
                            <Button type="link">Open catalog view ↗</Button>
                        </a>
                    )}
                </div>
            </Header>
            <Tabs
                activeKey={tab}
                onChange={setTab}
                style={{ marginTop: 12 }}
                items={[
                    {
                        key: 'summary',
                        label: 'Summary',
                        children: (
                            <>
                                <Surface style={{ padding: 18 }}>
                                    <h3>What it is</h3>
                                    {description ? (
                                        <MarkdownView source={description} />
                                    ) : (
                                        <Muted>No description yet. Start a discussion or generate one with AI from the Context inbox.</Muted>
                                    )}
                                    <h3 style={{ marginTop: 16 }}>Owners</h3>
                                    {(d?.ownership?.owners ?? []).length === 0 && <Muted>No owners assigned.</Muted>}
                                    {(d?.ownership?.owners ?? []).map((o) => (
                                        <Tag key={o.owner.urn}>
                                            {o.owner.username || o.owner.name || actorName(o.owner.urn)}
                                            {o.ownershipType?.info?.name ? ` (${o.ownershipType.info.name})` : ''}
                                        </Tag>
                                    ))}
                                    <h3 style={{ marginTop: 16 }}>Business terms & tags</h3>
                                    {(d?.glossaryTerms?.terms ?? []).map((t) => (
                                        <Tag color="purple" key={t.term.urn}>
                                            {t.term.properties?.name ?? t.term.urn.split(':').pop()}
                                        </Tag>
                                    ))}
                                    {(d?.tags?.tags ?? []).map((t) => (
                                        <Tag key={t.tag.urn}>{t.tag.properties?.name ?? t.tag.urn.split(':').pop()}</Tag>
                                    ))}
                                    {!(d?.glossaryTerms?.terms ?? []).length && !(d?.tags?.tags ?? []).length && <Muted>None yet.</Muted>}
                                    {e && (
                                        <p style={{ marginTop: 16, color: tokens.color.textMuted }}>
                                            👁 {e.views_7d} views this week · ☆ {e.bookmarks} bookmarks · 💬 {e.threads_open} open discussions
                                        </p>
                                    )}
                                </Surface>
                                <SimilarAssets urn={urn} />
                            </>
                        ),
                    },
                    {
                        key: 'columns',
                        label: `Columns${columns.length ? ` (${columns.length})` : ''}`,
                        children: (
                            <Table
                                size="small"
                                rowKey="fieldPath"
                                dataSource={columns}
                                pagination={{ pageSize: 50, hideOnSinglePage: true }}
                                locale={{ emptyText: 'No schema available' }}
                                columns={[
                                    { title: 'Column', dataIndex: 'fieldPath', render: (v: string) => <code>{v}</code> },
                                    { title: 'Type', dataIndex: 'type' },
                                    {
                                        title: 'Sensitivity',
                                        dataIndex: 'classifications',
                                        render: (v: string[]) =>
                                            v.map((c) => (
                                                <Tag key={c} color="orange" title="Values may be masked for you by a governance purpose">
                                                    🔒 {c}
                                                </Tag>
                                            )),
                                    },
                                    { title: 'Description', dataIndex: 'description', render: (v: string) => v || <Muted>—</Muted> },
                                ]}
                            />
                        ),
                    },
                    {
                        key: 'discussion',
                        label: `Discussion${e?.threads_open ? ` (${e.threads_open})` : ''}`,
                        children: <DiscussionPanel entityUrn={urn} me={me} />,
                    },
                    {
                        key: 'quality',
                        label: 'Quality',
                        children: (
                            <Surface style={{ padding: 18 }}>
                                {health.loading && <Skeleton h={60} />}
                                {!health.loading && !health.data?.monitors.length && (
                                    <Empty description="No quality monitors on this asset yet." />
                                )}
                                {(health.data?.monitors ?? []).map(({ monitor, last_evaluation: ev }) => (
                                    <div key={monitor.id} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0' }}>
                                        <span>
                                            <strong>{monitor.name}</strong> <Muted>{monitor.type}</Muted>
                                        </span>
                                        <span>
                                            <TrustBadges qualityStatus={ev?.status ?? 'UNKNOWN'} compact />{' '}
                                            <Muted>{ev ? relativeTime(ev.ts) : 'never run'}</Muted>
                                        </span>
                                    </div>
                                ))}
                            </Surface>
                        ),
                    },
                    {
                        key: 'lineage',
                        label: 'Lineage',
                        children: (
                            <Surface style={{ padding: 18 }}>
                                <p>
                                    Something looks off? Get a plain-English explanation of upstream failures, schema changes and
                                    pipeline runs.
                                </p>
                                <Button type="primary" loading={explaining} onClick={() => void runExplain()}>
                                    Explain lineage anomalies
                                </Button>{' '}
                                {catalogHref && <a href={`${catalogHref}/Lineage`}>Open full lineage ↗</a>}
                                {explain && (
                                    <div style={{ marginTop: 16 }}>
                                        <MarkdownView source={explain.summary_md} />
                                        {explain.likely_causes.map((c) => (
                                            <Alert
                                                key={c.urn}
                                                style={{ marginBottom: 8 }}
                                                type={c.score > 0.6 ? 'warning' : 'info'}
                                                message={
                                                    <span>
                                                        <Link to={`/asset/${encodeURIComponent(c.urn)}`}>{c.name}</Link> — {c.reason}
                                                    </span>
                                                }
                                                description={c.evidence.join(' · ')}
                                            />
                                        ))}
                                        {explain.timeline.length > 0 && (
                                            <Timeline>
                                                {explain.timeline.map((t) => (
                                                    <Timeline.Item key={`${t.at}-${t.urn}-${t.event}`}>
                                                        {new Date(t.at).toLocaleString()} — {t.event}
                                                    </Timeline.Item>
                                                ))}
                                            </Timeline>
                                        )}
                                        <Muted style={{ fontSize: 12 }}>
                                            {explain.used_llm ? 'Explanation drafted by AI from catalog evidence.' : 'Rule-based explanation from catalog evidence.'}
                                        </Muted>
                                    </div>
                                )}
                            </Surface>
                        ),
                    },
                    {
                        key: 'access',
                        label: 'Access',
                        children: (
                            <Surface style={{ padding: 18 }}>
                                {sensitive ? (
                                    <Alert
                                        type="warning"
                                        showIcon
                                        message="Contains personal data"
                                        description="Sample values and profiles of classified columns may be masked for you by a governance purpose. Request access through the data product to use the data."
                                    />
                                ) : (
                                    <Alert type="info" showIcon message="No sensitive classifications on this asset." />
                                )}
                                <div style={{ marginTop: 12 }}>
                                    <Link to={`/marketplace?q=${encodeURIComponent(name)}`}>Find the data product that includes this asset →</Link>
                                </div>
                            </Surface>
                        ),
                    },
                ]}
            />
        </Page>
    );
}
