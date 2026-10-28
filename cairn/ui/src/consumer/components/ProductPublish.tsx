import { Alert, Button, Input, Radio, Space, Tag, Typography } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi } from '../api/hooks';
import type { PublishOptions, PublishPlan, PublishTarget } from '../api/types';
import { Muted, Surface } from './tokens';

const split = (s: string) =>
    s
        .split(/[\s,]+/)
        .map((x) => x.trim())
        .filter(Boolean);

/** Product page → Share outside: plan, then publish a Snowflake listing or a Delta Sharing share. */
export function ProductPublish({ urn }: { urn: string }) {
    const { client } = useConsumerApi();
    const [target, setTarget] = useState<PublishTarget>('snowflake');
    const [share, setShare] = useState('');
    const [consumers, setConsumers] = useState('');
    const [connection, setConnection] = useState('');
    const [plan, setPlan] = useState<PublishPlan | null>(null);
    const [error, setError] = useState<string | null>(null);
    const options = (): PublishOptions => ({ share, consumers: split(consumers) });

    const run = async (fn: () => Promise<PublishPlan>) => {
        setError(null);
        try {
            setPlan(await fn());
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };

    return (
        <Surface style={{ padding: 18 }} aria-label="Share outside the company">
            <Space direction="vertical" style={{ width: '100%' }}>
                <Radio.Group
                    aria-label="Target"
                    value={target}
                    onChange={(e) => {
                        setTarget(e.target.value);
                        setPlan(null);
                    }}
                >
                    <Radio.Button value="snowflake">Snowflake listing</Radio.Button>
                    <Radio.Button value="databricks">Delta Sharing</Radio.Button>
                </Radio.Group>
                <Input
                    aria-label="Share name"
                    placeholder="Share name"
                    value={share}
                    onChange={(e) => setShare(e.target.value)}
                />
                <Input
                    aria-label="Consumers"
                    placeholder={target === 'snowflake' ? 'Consumer accounts (ORG.ACCOUNT, …)' : 'Recipients'}
                    value={consumers}
                    onChange={(e) => setConsumers(e.target.value)}
                />
                <Button onClick={() => run(() => client.planPublication(urn, target, options()))} disabled={!share}>
                    Plan
                </Button>
                {error && <Alert type="error" showIcon message={error} />}
                {plan && (
                    <>
                        <div>
                            {plan.in_sync ? (
                                <Tag color="green">published, up to date</Tag>
                            ) : plan.published ? (
                                <Tag color="orange">published, out of date</Tag>
                            ) : (
                                <Tag>not published yet</Tag>
                            )}
                            {plan.tables.length} table{plan.tables.length === 1 ? '' : 's'}
                            {plan.skipped.length > 0 && (
                                <Muted> · {plan.skipped.length} on other platforms skipped</Muted>
                            )}
                        </div>
                        <Typography.Paragraph>
                            <pre aria-label="Statements" style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>
                                {plan.statements.join(';\n')};
                            </pre>
                        </Typography.Paragraph>
                        {plan.failed && plan.failed.length > 0 && (
                            <Alert
                                type="error"
                                showIcon
                                message="Not published: the warehouse refused some statements"
                                description={plan.failed.map((f) => (
                                    <div key={f.sql}>{f.error}</div>
                                ))}
                            />
                        )}
                        <Space>
                            <Input
                                aria-label="Connection"
                                placeholder="Warehouse connection (empty: I ran them myself)"
                                value={connection}
                                onChange={(e) => setConnection(e.target.value)}
                                style={{ width: 320 }}
                            />
                            <Button
                                type="primary"
                                onClick={() => run(() => client.publishProduct(urn, target, options(), connection))}
                            >
                                {connection ? 'Publish' : 'Record as published'}
                            </Button>
                        </Space>
                    </>
                )}
            </Space>
        </Surface>
    );
}
