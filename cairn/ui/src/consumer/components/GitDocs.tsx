import { Button, List, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { Muted } from './tokens';

/** "Send docs to dbt": opens a pull request with the descriptions written in Cairn, for datasets a
 * configured dbt repository builds. Hidden for everything else. */
export function SendDocsToDbt({ urn }: { urn: string }) {
    const { client } = useConsumerApi();
    const match = useDheQuery(
        urn.startsWith('urn:li:dataset:') ? `/integrations/git/match?urn=${urn}` : null,
        (c, signal) => c.gitMatch(urn, { signal }),
    );
    const [busy, setBusy] = useState(false);
    if (!match.data) return null;
    return (
        <Button
            loading={busy}
            title={`Open a pull request in ${match.data.repo} writing these descriptions into ${match.data.model}'s YAML`}
            onClick={async () => {
                setBusy(true);
                try {
                    const out = await client.gitWriteback(urn);
                    if (out.pull_request)
                        message.success(
                            <span>
                                Pull request opened: <a href={out.pull_request}>{out.pull_request}</a>
                            </span>,
                            8,
                        );
                    else message.info(out.note ?? 'Nothing to send');
                } catch (e) {
                    message.error(e instanceof Error ? e.message : String(e));
                } finally {
                    setBusy(false);
                }
            }}
        >
            Send docs to dbt
        </Button>
    );
}

/** The dbt repositories Cairn watches for pull requests (config/git). */
export function GitRepositories() {
    const repos = useDheQuery('/integrations/git/repos', (c, signal) => c.gitRepos({ signal }));
    if (!repos.data?.length) return null;
    return (
        <List
            size="small"
            header={<Muted>dbt repositories: impact on pull requests, docs written back</Muted>}
            dataSource={repos.data}
            renderItem={(r) => (
                <List.Item>
                    <span>
                        <Tag>{r.provider}</Tag>
                        {r.repo} <Muted>· {r.base_branch}</Muted>
                    </span>
                    <span>
                        {!r.token_set && <Tag color="red">no token</Tag>}
                        {!r.webhook_secret_set && <Tag color="orange">no webhook secret</Tag>}
                        <Muted>
                            builds into {r.platform}
                            {r.database && ` ${r.database}${r.schema ? `.${r.schema}` : ''}`}
                        </Muted>
                    </span>
                </List.Item>
            )}
        />
    );
}
