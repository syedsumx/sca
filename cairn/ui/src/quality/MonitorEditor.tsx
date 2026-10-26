import React, { useEffect, useMemo } from 'react';
import { Alert, Form, Input, InputNumber, Modal, Radio, Select, Switch } from 'antd';

import { useDheClient, useMutation } from '../api/hooks';
import type { Monitor, MonitorInput, MonitorType, Sensitivity, Severity } from '../api/types';

type FieldKind = 'text' | 'number' | 'select' | 'boolean' | 'sql';

export interface ConfigField {
    name: string;
    label: string;
    kind: FieldKind;
    required?: boolean;
    options?: Array<{ label: string; value: string }>;
    help?: string;
    min?: number;
    defaultValue?: unknown;
}

const SOURCE_OPTIONS = {
    freshness: [
        { label: 'Cairn Core operations (no warehouse access)', value: 'catalog_operation' },
        { label: 'Warehouse information_schema', value: 'information_schema' },
        { label: 'MAX(timestamp column)', value: 'sql' },
    ],
    volume: [
        { label: 'Cairn Core dataset profile', value: 'catalog_profile' },
        { label: 'Warehouse information_schema', value: 'information_schema' },
        { label: 'COUNT(*) query', value: 'sql' },
    ],
};

/** Per-type configuration schema; keys map 1:1 to the monitor `config` object. */
export const MONITOR_CONFIG_FIELDS: Record<MonitorType, ConfigField[]> = {
    freshness: [
        { name: 'source', label: 'Signal source', kind: 'select', required: true, options: SOURCE_OPTIONS.freshness, defaultValue: 'catalog_operation' },
        { name: 'timestamp_column', label: 'Timestamp column', kind: 'text', help: 'Required when source is MAX(timestamp column)' },
        { name: 'sla_seconds', label: 'Hard SLA (seconds)', kind: 'number', min: 60, help: 'Fail when the dataset is older than this, regardless of learned cadence' },
        { name: 'connection_id', label: 'Warehouse connection', kind: 'text', help: 'Cairn connection id for SQL-based sources' },
    ],
    volume: [
        { name: 'source', label: 'Signal source', kind: 'select', required: true, options: SOURCE_OPTIONS.volume, defaultValue: 'catalog_profile' },
        { name: 'min_rows', label: 'Minimum rows', kind: 'number', min: 0 },
        { name: 'max_rows', label: 'Maximum rows', kind: 'number', min: 0 },
        { name: 'max_change_pct', label: 'Max change vs previous (%)', kind: 'number', min: 0 },
        { name: 'filter_sql', label: 'Row filter (WHERE clause)', kind: 'text' },
        { name: 'connection_id', label: 'Warehouse connection', kind: 'text' },
    ],
    field_metric: [
        { name: 'field', label: 'Field path', kind: 'text', required: true },
        {
            name: 'metric',
            label: 'Metric',
            kind: 'select',
            required: true,
            defaultValue: 'null_pct',
            options: ['null_pct', 'distinct_pct', 'min', 'max', 'mean', 'stddev', 'length_mean', 'negative_pct'].map(
                (v) => ({ label: v, value: v }),
            ),
        },
        { name: 'min', label: 'Static lower bound', kind: 'number' },
        { name: 'max', label: 'Static upper bound', kind: 'number' },
        { name: 'connection_id', label: 'Warehouse connection', kind: 'text', required: true },
    ],
    distribution: [
        { name: 'field', label: 'Field path', kind: 'text', required: true },
        {
            name: 'bucketing',
            label: 'Bucketing',
            kind: 'select',
            required: true,
            defaultValue: 'categorical',
            options: [
                { label: 'Categorical (top values)', value: 'categorical' },
                { label: 'Numeric (quantile buckets)', value: 'numeric' },
            ],
        },
        { name: 'num_buckets', label: 'Buckets', kind: 'number', min: 2, defaultValue: 10 },
        { name: 'psi_warn', label: 'PSI warn threshold', kind: 'number', min: 0, defaultValue: 0.2 },
        { name: 'psi_fail', label: 'PSI fail threshold', kind: 'number', min: 0, defaultValue: 0.3 },
        { name: 'connection_id', label: 'Warehouse connection', kind: 'text', required: true },
    ],
    schema: [
        {
            name: 'fail_on',
            label: 'Fail on',
            kind: 'select',
            required: true,
            defaultValue: 'breaking',
            options: [
                { label: 'Breaking changes (removed / retyped fields)', value: 'breaking' },
                { label: 'Any change', value: 'any' },
            ],
        },
        { name: 'allow_additions', label: 'Allow new fields', kind: 'boolean', defaultValue: true },
    ],
    custom_sql: [
        { name: 'sql', label: 'SQL returning a single number', kind: 'sql', required: true },
        {
            name: 'operator',
            label: 'Operator',
            kind: 'select',
            defaultValue: 'between',
            options: ['between', 'gt', 'gte', 'lt', 'lte', 'eq', 'anomaly'].map((v) => ({ label: v, value: v })),
        },
        { name: 'min', label: 'Lower bound', kind: 'number' },
        { name: 'max', label: 'Upper bound', kind: 'number' },
        { name: 'connection_id', label: 'Warehouse connection', kind: 'text', required: true },
    ],
};

export const MONITOR_TYPE_LABELS: Record<MonitorType, string> = {
    freshness: 'Freshness',
    volume: 'Volume',
    field_metric: 'Field metric',
    distribution: 'Distribution drift',
    schema: 'Schema change',
    custom_sql: 'Custom SQL',
};

interface FormValues {
    name: string;
    dataset_urn: string;
    type: MonitorType;
    schedule_mode: 'cron' | 'interval';
    cron?: string;
    interval_seconds?: number;
    sensitivity: Sensitivity;
    severity: Severity;
    enabled: boolean;
    config: Record<string, unknown>;
}

/** Drops empty config values so the backend applies its own defaults. */
export function cleanConfig(config: Record<string, unknown> | undefined): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    Object.entries(config ?? {}).forEach(([k, v]) => {
        if (v !== undefined && v !== null && v !== '') out[k] = v;
    });
    return out;
}

export function toMonitorInput(values: FormValues): MonitorInput {
    return {
        name: values.name.trim(),
        dataset_urn: values.dataset_urn.trim(),
        type: values.type,
        schedule:
            values.schedule_mode === 'cron'
                ? { cron: values.cron?.trim() }
                : { interval_seconds: values.interval_seconds },
        config: cleanConfig(values.config),
        sensitivity: values.sensitivity,
        severity: values.severity,
        enabled: values.enabled,
    };
}

function defaultsFor(type: MonitorType): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    MONITOR_CONFIG_FIELDS[type].forEach((f) => {
        if (f.defaultValue !== undefined) out[f.name] = f.defaultValue;
    });
    return out;
}

export interface MonitorEditorProps {
    open: boolean;
    /** When provided the editor updates this monitor; otherwise it creates a new one. */
    monitor?: Monitor | null;
    defaultDatasetUrn?: string;
    onClose: () => void;
    onSaved: (monitor: Monitor) => void;
}

export function MonitorEditor({ open, monitor, defaultDatasetUrn, onClose, onSaved }: MonitorEditorProps) {
    const client = useDheClient();
    const [form] = Form.useForm<FormValues>();
    const type = Form.useWatch('type', form) as MonitorType | undefined;
    const scheduleMode = Form.useWatch('schedule_mode', form) as FormValues['schedule_mode'] | undefined;
    const { mutate, pending, error } = useMutation((input: MonitorInput) =>
        monitor ? client.updateMonitor(monitor.id, input) : client.createMonitor(input),
    );

    const initialValues = useMemo<FormValues>(
        () =>
            monitor
                ? {
                      name: monitor.name,
                      dataset_urn: monitor.dataset_urn,
                      type: monitor.type,
                      schedule_mode: monitor.schedule.cron ? 'cron' : 'interval',
                      cron: monitor.schedule.cron,
                      interval_seconds: monitor.schedule.interval_seconds ?? 3600,
                      sensitivity: monitor.sensitivity,
                      severity: monitor.severity,
                      enabled: monitor.enabled,
                      config: { ...monitor.config },
                  }
                : {
                      name: '',
                      dataset_urn: defaultDatasetUrn ?? '',
                      type: 'freshness',
                      schedule_mode: 'interval',
                      interval_seconds: 3600,
                      sensitivity: 'medium',
                      severity: 'MEDIUM',
                      enabled: true,
                      config: defaultsFor('freshness'),
                  },
        [monitor, defaultDatasetUrn],
    );

    useEffect(() => {
        if (open) form.setFieldsValue(initialValues);
        else form.resetFields();
    }, [open, initialValues, form]);

    const onTypeChange = (next: MonitorType) => {
        form.setFieldsValue({ config: defaultsFor(next) });
    };

    const submit = async () => {
        const values = await form.validateFields();
        const saved = await mutate(toMonitorInput(values));
        onSaved(saved);
    };

    const fields = MONITOR_CONFIG_FIELDS[type ?? 'freshness'];

    return (
        <Modal
            open={open}
            title={monitor ? `Edit monitor: ${monitor.name}` : 'New data quality monitor'}
            okText={monitor ? 'Save' : 'Create'}
            onOk={() => {
                submit().catch(() => undefined);
            }}
            confirmLoading={pending}
            onCancel={onClose}
            width={680}
            destroyOnClose
        >
            {error && <Alert type="error" showIcon message={error.message} style={{ marginBottom: 16 }} />}
            <Form form={form} layout="vertical" initialValues={initialValues} requiredMark="optional">
                <Form.Item name="name" label="Name" rules={[{ required: true, whitespace: true }]}>
                    <Input placeholder="orders freshness" />
                </Form.Item>
                <Form.Item
                    name="dataset_urn"
                    label="Dataset URN"
                    rules={[
                        { required: true },
                        { pattern: /^urn:li:dataset:\(.+\)$/, message: 'Must be a dataset URN' },
                    ]}
                >
                    <Input placeholder="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.table,PROD)" />
                </Form.Item>
                <Form.Item name="type" label="Monitor type" rules={[{ required: true }]}>
                    <Select
                        onChange={onTypeChange}
                        disabled={!!monitor}
                        options={(Object.keys(MONITOR_TYPE_LABELS) as MonitorType[]).map((t) => ({
                            value: t,
                            label: MONITOR_TYPE_LABELS[t],
                        }))}
                    />
                </Form.Item>

                <Form.Item name="schedule_mode" label="Schedule">
                    <Radio.Group>
                        <Radio.Button value="interval">Every N seconds</Radio.Button>
                        <Radio.Button value="cron">Cron</Radio.Button>
                    </Radio.Group>
                </Form.Item>
                {scheduleMode === 'cron' ? (
                    <Form.Item
                        name="cron"
                        label="Cron expression (UTC)"
                        rules={[
                            { required: true },
                            { pattern: /^(\S+\s+){4}\S+$/, message: 'Expected 5 fields, e.g. "0 * * * *"' },
                        ]}
                    >
                        <Input placeholder="0 * * * *" />
                    </Form.Item>
                ) : (
                    <Form.Item name="interval_seconds" label="Interval (seconds)" rules={[{ required: true }]}>
                        <InputNumber min={60} step={60} style={{ width: '100%' }} />
                    </Form.Item>
                )}

                {fields.map((f) => (
                    <Form.Item
                        key={`${type}-${f.name}`}
                        name={['config', f.name]}
                        label={f.label}
                        tooltip={f.help}
                        valuePropName={f.kind === 'boolean' ? 'checked' : 'value'}
                        rules={f.required ? [{ required: true, message: `${f.label} is required` }] : undefined}
                    >
                        {f.kind === 'number' && <InputNumber min={f.min} style={{ width: '100%' }} />}
                        {f.kind === 'text' && <Input />}
                        {f.kind === 'select' && <Select options={f.options} />}
                        {f.kind === 'boolean' && <Switch />}
                        {f.kind === 'sql' && (
                            <Input.TextArea
                                rows={4}
                                spellCheck={false}
                                style={{ fontFamily: 'monospace' }}
                                placeholder="SELECT COUNT(*) FROM db.schema.table WHERE status = 'error'"
                            />
                        )}
                    </Form.Item>
                ))}

                <Form.Item
                    name="sensitivity"
                    label="Anomaly sensitivity"
                    tooltip="Higher sensitivity flags smaller deviations from the learned baseline"
                >
                    <Radio.Group>
                        <Radio.Button value="low">Low</Radio.Button>
                        <Radio.Button value="medium">Medium</Radio.Button>
                        <Radio.Button value="high">High</Radio.Button>
                    </Radio.Group>
                </Form.Item>
                <Form.Item name="severity" label="Severity">
                    <Select options={['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((s) => ({ value: s, label: s }))} />
                </Form.Item>
                <Form.Item name="enabled" label="Enabled" valuePropName="checked">
                    <Switch />
                </Form.Item>
            </Form>
        </Modal>
    );
}

export default MonitorEditor;
