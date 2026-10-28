import { Alert, Button, Drawer, Input, Radio, Result, Space, Steps, Tag } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi } from '../api/hooks';
import type { AccessRequest, AccessStatus, ProductCard as ProductCardData } from '../api/types';
import { actorName, plural, relativeTime } from '../utils/format';
import { focusRing, tokens } from './tokens';
import { TrustBadges } from './TrustBadges';

export function productPath(urn: string): string {
    return `/marketplace/${encodeURIComponent(urn)}`;
}

export function accessLabel(status: AccessStatus, expiresAt?: string | null): { text: string; color: string } {
    switch (status) {
        case 'GRANTED':
            return { text: expiresAt ? `✓ Access until ${new Date(expiresAt).toLocaleDateString()}` : '✓ Access granted', color: 'green' };
        case 'PENDING':
            return { text: '⏳ Request pending', color: 'blue' };
        case 'EXPIRED':
            return { text: 'Access expired', color: 'orange' };
        case 'REJECTED':
            return { text: 'Request declined', color: 'red' };
        default:
            return { text: 'Request access', color: 'default' };
    }
}

const Card = styled.div`
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 16px;
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.lg};
    box-shadow: ${tokens.shadow.card};
    a.title {
        font-size: 16px;
        font-weight: 600;
        color: ${tokens.color.text};
        ${focusRing}
    }
`;

export function ProductCard({ product, onRequest }: { product: ProductCardData; onRequest: (p: ProductCardData) => void }) {
    const access = accessLabel(product.access.status, product.access.expires_at);
    const requestable = ['NONE', 'EXPIRED', 'REJECTED'].includes(product.access.status);
    return (
        <Card aria-label={`Data product ${product.name}`}>
            <Link className="title" to={productPath(product.urn)}>
                {product.name}
            </Link>
            <div style={{ fontSize: 12, color: tokens.color.textMuted }}>
                {product.domain?.name ?? 'No domain'} · {plural(product.assets_count, 'asset')} · {plural(product.consumers, 'consumer')}
            </div>
            <div style={{ fontSize: 13, color: tokens.color.textMuted, minHeight: 36 }}>
                {product.description || 'No description yet.'}
            </div>
            <TrustBadges certification={product.certification} qualityStatus={product.quality_status} contract={product.contract} />
            {(product.sla.freshness || product.sla.availability) && (
                <div style={{ fontSize: 12 }}>
                    {product.sla.availability && <Tag>SLA {product.sla.availability}</Tag>}
                    {product.sla.freshness && <Tag>fresh {product.sla.freshness}</Tag>}
                </div>
            )}
            {requestable ? (
                <Button type="primary" onClick={() => onRequest(product)}>
                    {product.access.status === 'NONE' ? 'Request access' : 'Request again'}
                </Button>
            ) : (
                <Tag color={access.color} style={{ textAlign: 'center', padding: '4px 0' }}>
                    {access.text}
                </Tag>
            )}
        </Card>
    );
}

const DURATIONS = [7, 30, 90, 180, 365];

export interface RequestAccessDrawerProps {
    product: ProductCardData | null;
    open: boolean;
    onClose: () => void;
    onSubmitted?: (request: AccessRequest) => void;
    /** Persona names the user belongs to (for the auto-approve preview copy). */
    myPersonas?: string[];
}

/** Three-step, ≤30 s ticket-free access request (spec §4.4). */
export function RequestAccessDrawer({ product, open, onClose, onSubmitted, myPersonas }: RequestAccessDrawerProps) {
    const { client, cache } = useConsumerApi();
    const [step, setStep] = useState(0);
    const [purpose, setPurpose] = useState<string>('');
    const [otherPurpose, setOtherPurpose] = useState('');
    const [duration, setDuration] = useState(30);
    const [justification, setJustification] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [result, setResult] = useState<AccessRequest | null>(null);

    const policy = product?.request_policy;
    const durations = useMemo(
        () => DURATIONS.filter((d) => !policy || d <= policy.max_duration_days),
        [policy],
    );
    const effectivePurpose = purpose === '__other' ? otherPurpose.trim() : purpose;
    const purposeKnown = !!policy?.purposes.includes(effectivePurpose);
    const willAutoApprove = !!policy?.auto_approve && (purposeKnown || !policy?.purposes.length);

    const reset = () => {
        setStep(0);
        setPurpose('');
        setOtherPurpose('');
        setDuration(30);
        setJustification('');
        setError(null);
        setResult(null);
    };

    const close = () => {
        reset();
        onClose();
    };

    const submit = async () => {
        if (!product) return;
        setBusy(true);
        setError(null);
        try {
            const created = await client.createAccessRequest({
                product_urn: product.urn,
                purpose: effectivePurpose,
                justification: justification.trim(),
                duration_days: duration,
            });
            setResult(created);
            cache.invalidate('/marketplace');
            onSubmitted?.(created);
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const canNext = [!!effectivePurpose, duration > 0, justification.trim().length >= 10][step];

    return (
        <Drawer
            title={product ? `Request access · ${product.name}` : 'Request access'}
            open={open}
            onClose={close}
            width={480}
            destroyOnClose
        >
            {result ? (
                <Result
                    status={result.state === 'APPROVED' ? 'success' : 'info'}
                    title={result.state === 'APPROVED' ? 'Access granted' : 'Request submitted'}
                    subTitle={
                        result.state === 'APPROVED'
                            ? `Auto-approved${result.expires_at ? ` until ${new Date(result.expires_at).toLocaleDateString()}` : ''}.`
                            : `Routed to ${result.approvers.map(actorName).join(', ') || 'the product owners'}. You'll be notified in Slack and here.`
                    }
                    extra={[
                        <Button key="done" type="primary" onClick={close}>
                            Done
                        </Button>,
                        <Link key="track" to="/requests" onClick={close}>
                            Track my requests
                        </Link>,
                    ]}
                />
            ) : (
                <>
                    <Steps
                        size="small"
                        current={step}
                        items={[{ title: 'Purpose' }, { title: 'Duration' }, { title: 'Justification' }]}
                        style={{ marginBottom: 24 }}
                    />
                    {step === 0 && (
                        <Radio.Group
                            aria-label="Purpose of use"
                            value={purpose}
                            onChange={(e) => setPurpose(e.target.value)}
                        >
                            <Space direction="vertical">
                                {(policy?.purposes ?? []).map((p) => (
                                    <Radio key={p} value={p}>
                                        {p}
                                    </Radio>
                                ))}
                                <Radio value="__other">Other…</Radio>
                                {purpose === '__other' && (
                                    <Input
                                        aria-label="Describe the purpose"
                                        placeholder="Describe the purpose"
                                        value={otherPurpose}
                                        onChange={(e) => setOtherPurpose(e.target.value)}
                                    />
                                )}
                            </Space>
                        </Radio.Group>
                    )}
                    {step === 1 && (
                        <>
                            <Radio.Group
                                aria-label="Access duration"
                                value={duration}
                                onChange={(e) => setDuration(e.target.value)}
                                optionType="button"
                                options={durations.map((d) => ({ label: `${d} days`, value: d }))}
                            />
                            <div style={{ marginTop: 8, color: tokens.color.textMuted, fontSize: 12 }}>
                                Max allowed: {policy?.max_duration_days ?? 90} days. Access ends automatically.
                            </div>
                        </>
                    )}
                    {step === 2 && (
                        <Input.TextArea
                            aria-label="Justification"
                            placeholder="What will you use this data for? (min. 10 characters)"
                            autoSize={{ minRows: 3, maxRows: 6 }}
                            value={justification}
                            onChange={(e) => setJustification(e.target.value)}
                        />
                    )}
                    <Alert
                        style={{ marginTop: 20 }}
                        type={willAutoApprove ? 'success' : 'info'}
                        showIcon
                        message={
                            willAutoApprove
                                ? `Will be auto-approved${myPersonas?.length ? ` (you are in ${myPersonas.join(', ')})` : ''} once submitted.`
                                : `Routed to ${policy?.approvers.map(actorName).join(', ') || 'the product owners'} — you'll get a Slack/Teams notification.`
                        }
                    />
                    {error && <Alert style={{ marginTop: 12 }} type="error" message={error} role="alert" />}
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 24 }}>
                        <Button onClick={step === 0 ? close : () => setStep(step - 1)}>{step === 0 ? 'Cancel' : 'Back'}</Button>
                        {step < 2 ? (
                            <Button type="primary" disabled={!canNext} onClick={() => setStep(step + 1)}>
                                Next
                            </Button>
                        ) : (
                            <Button type="primary" disabled={!canNext} loading={busy} onClick={() => void submit()}>
                                Submit request
                            </Button>
                        )}
                    </div>
                </>
            )}
        </Drawer>
    );
}

export function AccessRequestSummary({ request }: { request: AccessRequest }) {
    return (
        <span>
            <strong>{request.product_name}</strong> · {request.purpose} · {request.duration_days} days ·{' '}
            {relativeTime(request.created_at)}
        </span>
    );
}
