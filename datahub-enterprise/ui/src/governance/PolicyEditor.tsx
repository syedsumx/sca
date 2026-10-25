import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Input, List, Popconfirm, Space, Table, Tag, message } from 'antd';

import { useAsync, useDheClient, useMutation } from '../api/hooks';
import type { Policy } from '../api/types';
import { ErrorState, LoadingState, Mono, PageContainer, PageHeader } from '../common/States';

export interface LocalLintResult {
    policies: Policy[] | null;
    errors: string[];
}

/**
 * Client-side structural lint run before the server-side validator. Accepts either a bare array of
 * policies or `{ "policies": [...] }`.
 */
export function lintPolicyText(text: string): LocalLintResult {
    let parsed: unknown;
    try {
        parsed = JSON.parse(text);
    } catch (e) {
        return { policies: null, errors: [`Invalid JSON: ${(e as Error).message}`] };
    }
    const list =
        parsed && typeof parsed === 'object' && !Array.isArray(parsed) && 'policies' in parsed
            ? (parsed as { policies: unknown }).policies
            : parsed;
    if (!Array.isArray(list)) return { policies: null, errors: ['Expected an array of policies or {"policies": [...]}'] };

    const errors: string[] = [];
    const ids = new Set<string>();
    list.forEach((p, i) => {
        const where = `policies[${i}]`;
        if (!p || typeof p !== 'object') {
            errors.push(`${where}: must be an object`);
            return;
        }
        const pol = p as Partial<Policy>;
        if (!pol.id || typeof pol.id !== 'string') errors.push(`${where}: "id" is required`);
        else if (ids.has(pol.id)) errors.push(`${where}: duplicate id "${pol.id}"`);
        else ids.add(pol.id);
        if (pol.effect !== 'allow' && pol.effect !== 'deny') errors.push(`${where}: "effect" must be "allow" or "deny"`);
        if (!Array.isArray(pol.actions) || pol.actions.length === 0) {
            errors.push(`${where}: "actions" must be a non-empty array`);
        }
        if (pol.priority !== undefined && typeof pol.priority !== 'number') {
            errors.push(`${where}: "priority" must be a number`);
        }
    });
    return { policies: errors.length ? null : (list as Policy[]), errors };
}

export function PolicyEditor() {
    const client = useDheClient();
    const { data, error, loading, reload } = useAsync((signal) => client.getPolicies({ signal }), [client]);
    const [text, setText] = useState('');
    const [dirty, setDirty] = useState(false);
    const [errors, setErrors] = useState<string[]>([]);
    const [validated, setValidated] = useState(false);
    const validate = useMutation((policies: Policy[]) => client.validatePolicies(policies));
    const save = useMutation((policies: Policy[]) => client.putPolicies(policies));

    useEffect(() => {
        if (data && !dirty) setText(JSON.stringify(data.policies, null, 2));
    }, [data, dirty]);

    const preview = useMemo(() => lintPolicyText(text), [text]);

    const runValidation = async (): Promise<Policy[] | null> => {
        const local = lintPolicyText(text);
        if (!local.policies) {
            setErrors(local.errors);
            setValidated(false);
            return null;
        }
        try {
            const res = await validate.mutate(local.policies);
            setErrors(res.errors);
            setValidated(res.valid);
            return res.valid ? local.policies : null;
        } catch (e) {
            setErrors([(e as Error).message]);
            setValidated(false);
            return null;
        }
    };

    const onSave = async () => {
        const policies = await runValidation();
        if (!policies) return;
        try {
            const res = await save.mutate(policies);
            setText(JSON.stringify(res.policies, null, 2));
            setDirty(false);
            message.success(`Saved ${res.policies.length} policies`);
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    if (loading && !data) return <LoadingState label="Loading policies…" />;
    if (error && !data) return <ErrorState error={error} onRetry={reload} />;

    return (
        <PageContainer>
            <PageHeader>
                <h1>ABAC policies</h1>
                <Space>
                    <Button
                        disabled={!dirty}
                        onClick={() => {
                            setDirty(false);
                            setErrors([]);
                            setValidated(false);
                            if (data) setText(JSON.stringify(data.policies, null, 2));
                        }}
                    >
                        Discard changes
                    </Button>
                    <Button onClick={runValidation} loading={validate.pending}>
                        Validate
                    </Button>
                    <Popconfirm
                        title="Publish this policy bundle? It takes effect for all authorization decisions."
                        onConfirm={onSave}
                        disabled={!dirty}
                    >
                        <Button type="primary" disabled={!dirty} loading={save.pending}>
                            Publish
                        </Button>
                    </Popconfirm>
                </Space>
            </PageHeader>
            <Alert
                type="info"
                showIcon
                message="Deny-overrides: any matching deny policy wins over allow policies. Use the Access Simulator to test changes before publishing."
            />
            {errors.length > 0 && (
                <Alert
                    type="error"
                    showIcon
                    message={`${errors.length} validation error${errors.length > 1 ? 's' : ''}`}
                    description={<List size="small" dataSource={errors} renderItem={(e) => <List.Item>{e}</List.Item>} />}
                />
            )}
            {validated && errors.length === 0 && <Alert type="success" showIcon message="Policy bundle is valid" />}
            <Input.TextArea
                aria-label="Policy bundle JSON"
                value={text}
                onChange={(e) => {
                    setText(e.target.value);
                    setDirty(true);
                    setValidated(false);
                }}
                autoSize={{ minRows: 16, maxRows: 40 }}
                spellCheck={false}
                style={{ fontFamily: 'SFMono-Regular, Menlo, Consolas, monospace', fontSize: 12 }}
            />
            <Table<Policy>
                size="small"
                title={() => 'Preview'}
                rowKey="id"
                dataSource={preview.policies ?? []}
                locale={{ emptyText: preview.errors[0] ?? 'No policies' }}
                pagination={false}
                columns={[
                    { title: 'Id', dataIndex: 'id', render: (v: string) => <Mono>{v}</Mono> },
                    {
                        title: 'Effect',
                        dataIndex: 'effect',
                        width: 90,
                        render: (e: Policy['effect']) => <Tag color={e === 'deny' ? 'red' : 'green'}>{e}</Tag>,
                    },
                    { title: 'Priority', dataIndex: 'priority', width: 90 },
                    {
                        title: 'Actions',
                        dataIndex: 'actions',
                        render: (a: string[]) => a.map((x) => <Tag key={x}>{x}</Tag>),
                    },
                    { title: 'Description', dataIndex: 'description' },
                ]}
            />
        </PageContainer>
    );
}

export default PolicyEditor;
