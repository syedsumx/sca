import { Button, Input, Space, Table, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi } from '../api/hooks';
import { RESOURCE_SPECS } from '../resources/catalog';
import { DetailPanel } from '../resources/DetailPanel';
import { ResourceView } from '../resources/ResourceView';
import { ErrorNotice, notifyError } from './ErrorNotice';
import { Muted, SectionTitle, Surface } from './tokens';

interface Template { sql: string; params: { name: string; type: string }[]; proposed_by: string; approved_by: string[] }
interface Room { name: string; parties: string[]; min_group: number; tables: Record<string, unknown>; templates: Record<string, Template> }

/** The arguments a run sends: each declared parameter, as its type (numbers as numbers, booleans as booleans). */
export function runArgs(params: { name: string; type: string }[], typed: Record<string, string>): Record<string, unknown> {
    return Object.fromEntries(params.map((p) => {
        const v = (typed[p.name] ?? '').trim();
        if (/int|long|double|float|number|decimal/i.test(p.type)) return [p.name, v === '' ? null : Number(v)];
        if (/bool/i.test(p.type)) return [p.name, /^(true|1|yes)$/i.test(v)];
        return [p.name, v];
    }));
}

/**
 * External sharing › Clean rooms (L149: the API had no page). Make a room, bring tables and propose templates
 * (the area's forms); open a room you are a party of by its name to see its templates, approve them and run them.
 * The API has no list of rooms, so a room is opened by name.
 */
export function CleanRoomsView() {
    const { client } = useConsumerApi();
    const [name, setName] = useState('');
    const [room, setRoom] = useState<Room | null>(null);
    const [error, setError] = useState<unknown>(null);
    const [result, setResult] = useState<Record<string, unknown> | null>(null);
    // What the person typed for each template's parameters (a template with parameters cannot run without them).
    const [args, setArgs] = useState<Record<string, Record<string, string>>>({});
    const open = async (n = name) => {
        setError(null);
        setResult(null);
        try {
            const r = await client.request<Room>('GET', `/v1/clean-rooms/${encodeURIComponent(n.trim())}`);
            setRoom({ ...r, parties: typeof r.parties === 'string' ? JSON.parse(r.parties) : r.parties,
                tables: typeof r.tables === 'string' ? JSON.parse(r.tables) : r.tables,
                templates: typeof r.templates === 'string' ? JSON.parse(r.templates) : r.templates });
        } catch (e) {
            setRoom(null);
            setError(e);
        }
    };
    const act = async (tname: string, what: 'approve' | 'run') => {
        if (!room) return;
        try {
            const out = await client.request<Record<string, unknown>>('POST',
                `/v1/clean-rooms/${encodeURIComponent(room.name)}/templates/${encodeURIComponent(tname)}/${what}`,
                what === 'run' ? { args: runArgs(room.templates[tname]?.params ?? [], args[tname] ?? {}) } : undefined);
            if (what === 'run') {
                setResult(out);                    // a run changes nothing in the room: keep its result on screen
                return;
            }
            message.success(`${tname}: approved by you`);
            await open(room.name);
        } catch (e) {
            notifyError(e);
        }
    };
    return (
        <>
            <ResourceView spec={RESOURCE_SPECS['clean-rooms']} />
            <Surface as="section" aria-label="Open a clean room" style={{ padding: 16, marginTop: 16 }}>
                <SectionTitle style={{ marginTop: 0 }}>Open a clean room</SectionTitle>
                <Space wrap>
                    <Input aria-label="Clean room name" placeholder="ad_overlap" value={name} onChange={(e) => setName(e.target.value)}
                        onPressEnter={() => void open()} style={{ width: 260 }} />
                    <Button type="primary" disabled={!name.trim()} onClick={() => void open()}>Open</Button>
                </Space>
                {error ? <ErrorNotice style={{ marginTop: 12 }} error={error} area="Clean rooms" /> : null}
                {room && (
                    <div style={{ marginTop: 16 }}>
                        <DetailPanel row={{ name: room.name, parties: room.parties, min_group: room.min_group, tables: Object.keys(room.tables ?? {}) }} />
                        <SectionTitle>Templates</SectionTitle>
                        <Table size="small" rowKey="name" pagination={false}
                            dataSource={Object.entries(room.templates ?? {}).map(([k, v]) => ({ name: k, ...v }))}
                            locale={{ emptyText: 'No templates proposed yet' }}
                            columns={[
                                { title: 'Template', dataIndex: 'name' },
                                { title: 'SQL', dataIndex: 'sql', render: (v: string) => <code>{v.slice(0, 120)}</code> },
                                { title: 'Approved by', dataIndex: 'approved_by', render: (v: string[]) => v.map((p) => <Tag key={p}>{p.split(':').pop()}</Tag>) },
                                { title: '', key: 'act', render: (_: unknown, r: { name: string; params?: { name: string; type: string }[] }) => (
                                    <Space wrap>
                                        {(r.params ?? []).map((p) => (
                                            <Input key={p.name} size="small" style={{ width: 150 }} aria-label={`${r.name} ${p.name}`}
                                                placeholder={`${p.name} (${p.type})`} value={args[r.name]?.[p.name] ?? ''}
                                                onChange={(e) => setArgs((a) => ({ ...a, [r.name]: { ...a[r.name], [p.name]: e.target.value } }))} />
                                        ))}
                                        <Button size="small" aria-label={`Approve ${r.name}`} onClick={() => void act(r.name, 'approve')}>Approve</Button>
                                        <Button size="small" aria-label={`Run ${r.name}`} onClick={() => void act(r.name, 'run')}>Run</Button>
                                    </Space>
                                ) },
                            ]} />
                        {result && (
                            <>
                                <SectionTitle>Result</SectionTitle>
                                <Muted>Only groups of at least {room.min_group} are answered.</Muted>
                                <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{JSON.stringify(result, null, 2)}</pre>
                            </>
                        )}
                    </div>
                )}
            </Surface>
        </>
    );
}
