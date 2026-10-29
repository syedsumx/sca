import { Alert, Button, Form, Input, InputNumber, Popconfirm, Progress, Select, Table, Tag } from 'antd';
import React from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { ProductSlo } from '../api/types';
import { Muted } from './tokens';

const STATE_COLOR: Record<ProductSlo['state'], string> = {
    met: 'green',
    at_risk: 'orange',
    breached: 'red',
    no_data: 'default',
};

function columns(withProduct: boolean, onDelete?: (id: string) => void) {
    return [
        ...(withProduct
            ? [
                  {
                      title: 'Product',
                      render: (_: unknown, r: ProductSlo) => r.product_urn.replace('urn:li:dataProduct:', ''),
                  },
              ]
            : []),
        { title: 'SLO', render: (_: unknown, r: ProductSlo) => r.name },
        { title: 'Kind', render: (_: unknown, r: ProductSlo) => <Tag>{r.kind}</Tag> },
        {
            title: 'Objective',
            render: (_: unknown, r: ProductSlo) => `${r.objective}% over ${r.window_days} days`,
        },
        {
            title: 'Now',
            render: (_: unknown, r: ProductSlo) =>
                r.sli === null ? <Muted>no checks yet</Muted> : `${r.sli}% (${r.good} of ${r.total})`,
        },
        {
            title: 'Error budget left',
            render: (_: unknown, r: ProductSlo) =>
                r.budget_remaining === null ? (
                    '—'
                ) : (
                    <Progress
                        percent={r.budget_remaining}
                        size="small"
                        status={r.state === 'breached' ? 'exception' : r.state === 'at_risk' ? 'active' : 'normal'}
                        style={{ width: 140 }}
                    />
                ),
        },
        {
            title: 'Burn rate (1 day)',
            render: (_: unknown, r: ProductSlo) => (r.burn_rate === null ? '—' : `${r.burn_rate}×`),
        },
        {
            title: 'State',
            render: (_: unknown, r: ProductSlo) => <Tag color={STATE_COLOR[r.state]}>{r.state.replace('_', ' ')}</Tag>,
        },
        ...(onDelete
            ? [
                  {
                      title: '',
                      render: (_: unknown, r: ProductSlo) => (
                          <Popconfirm title={`Remove ${r.name}?`} onConfirm={() => onDelete(r.id)}>
                              <Button size="small" danger>
                                  Remove
                              </Button>
                          </Popconfirm>
                      ),
                  },
              ]
            : []),
    ];
}

/** A product's SLOs; its owners can add and remove them. */
export function ProductSlos({ urn, canEdit }: { urn: string; canEdit: boolean }) {
    const { client } = useConsumerApi();
    const slos = useDheQuery(`/marketplace/slos?product=${urn}`, (c, signal) => c.productSlos(urn, { signal }));
    const [form] = Form.useForm();
    return (
        <>
            <Muted>
                Objectives on the share of the product&apos;s quality checks that pass, with what is left of the error
                budget.
            </Muted>
            <Table<ProductSlo>
                style={{ marginTop: 8 }}
                size="small"
                rowKey="id"
                pagination={false}
                loading={slos.loading}
                dataSource={slos.data ?? []}
                locale={{ emptyText: 'No SLOs yet.' }}
                columns={columns(
                    false,
                    canEdit
                        ? async (id) => {
                              await client.deleteSlo(id);
                              slos.refresh();
                          }
                        : undefined,
                )}
            />
            {canEdit && (
                <Form
                    form={form}
                    layout="inline"
                    style={{ marginTop: 12 }}
                    initialValues={{ kind: 'freshness', objective: 99, window_days: 28 }}
                    onFinish={async (v) => {
                        const id = `${urn.replace('urn:li:dataProduct:', '')}-${v.kind}`
                            .toLowerCase()
                            .replace(/[^a-z0-9-]/g, '-');
                        await client.putSlo({ ...v, id, product_urn: urn });
                        form.resetFields(['name']);
                        slos.refresh();
                    }}
                >
                    <Form.Item name="name" rules={[{ required: true, message: 'Name it' }]}>
                        <Input aria-label="SLO name" placeholder="Orders land by 06:00" style={{ width: 200 }} />
                    </Form.Item>
                    <Form.Item name="kind">
                        <Select
                            aria-label="SLO kind"
                            style={{ width: 130 }}
                            options={['freshness', 'volume', 'quality', 'all'].map((k) => ({ value: k, label: k }))}
                        />
                    </Form.Item>
                    <Form.Item name="objective" label="Objective %">
                        <InputNumber aria-label="Objective" min={50} max={99.999} step={0.1} />
                    </Form.Item>
                    <Form.Item name="window_days" label="Window (days)">
                        <InputNumber aria-label="Window" min={1} max={90} />
                    </Form.Item>
                    <Button htmlType="submit">Add SLO</Button>
                </Form>
            )}
        </>
    );
}

/** Reports → SLOs: every product SLO, worst first. */
export function SloDashboard() {
    const slos = useDheQuery('/marketplace/slos', (c, signal) => c.allSlos({ signal }));
    const bad = (slos.data ?? []).filter((s) => s.state === 'breached' || s.state === 'at_risk');
    return (
        <>
            {bad.length > 0 && (
                <Alert
                    type="warning"
                    showIcon
                    style={{ marginBottom: 8 }}
                    message={`${bad.length} SLO${bad.length > 1 ? 's are' : ' is'} at risk or breached`}
                />
            )}
            <Table<ProductSlo>
                size="small"
                rowKey="id"
                loading={slos.loading}
                dataSource={slos.data ?? []}
                pagination={{ pageSize: 15, hideOnSinglePage: true }}
                locale={{ emptyText: 'No product has SLOs yet: add them on a product page.' }}
                columns={columns(true)}
            />
        </>
    );
}
