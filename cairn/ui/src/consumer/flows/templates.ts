import type { FlowDef } from './model';

export interface FlowTemplate {
    id: string;
    title: string;
    blurb: string;
    make: (name: string, me: string) => FlowDef;
}

/** Starting points: each makes a draft (nothing runs until it is published). */
export const TEMPLATES: FlowTemplate[] = [
    {
        id: 'nightly',
        title: 'Nightly refresh, check, report',
        blurb: 'Refresh a derived table at 02:00, stop if the data looks wrong, then send the report.',
        make: (name, me) => ({
            kind: 'flow',
            name,
            description: 'Refresh, check, report',
            triggers: [{ type: 'schedule', cron: '0 2 * * *', timezone: 'UTC' }],
            notify: { on: ['failure'], to: [me] },
            steps: [
                { name: 'refresh', type: 'derived', table: 'revenue' },
                { name: 'not-empty', type: 'check', after: ['refresh'], sql: 'SELECT count(*) AS n FROM marts.revenue',
                    connection: 'lakehouse', measure: 'n', op: '>', value: 0 },
                { name: 'report', type: 'sql', after: ['not-empty'], sql: 'SELECT region, total FROM marts.revenue ORDER BY total DESC',
                    connection: 'lakehouse' },
                { name: 'tell', type: 'notify', after: ['report'], message: 'Revenue refreshed: {{ steps.report.rows }} regions.' },
            ],
        }),
    },
    {
        id: 'ingest',
        title: 'Ingest → transform → notify',
        blurb: 'Load new files when they land, rebuild the clean table, tell the team.',
        make: (name, me) => ({
            kind: 'flow',
            name,
            description: 'Ingest, transform, notify',
            triggers: [{ type: 'dataset', tables: ['raw.orders'] }],
            concurrency: { policy: 'skip' },
            steps: [
                { name: 'load', type: 'pipe', pipe: 'orders-files' },
                { name: 'clean', type: 'derived', after: ['load'], table: 'orders_clean' },
                { name: 'rows', type: 'check', after: ['clean'], sql: 'SELECT count(*) AS n FROM marts.orders_clean',
                    connection: 'lakehouse', measure: 'n', op: '>', value: 0 },
                { name: 'tell', type: 'notify', after: ['rows'], to: [me], message: 'Orders loaded: {{ steps.rows.value }} rows.' },
            ],
        }),
    },
    {
        id: 'triage',
        title: 'Agent triage with approval',
        blurb: 'An agent triages the day’s failed checks; a person approves before anyone is told.',
        make: (name, me) => ({
            kind: 'flow',
            name,
            description: 'Agent triage with approval',
            triggers: [{ type: 'schedule', cron: '30 7 * * 1-5', timezone: 'UTC' }],
            steps: [
                { name: 'failures', type: 'sql', sql: "SELECT dataset, monitor, message FROM ops.check_failures WHERE day = current_date",
                    connection: 'lakehouse' },
                { name: 'any', type: 'branch', after: ['failures'], condition: 'steps.failures.rows > 0', then: ['triage'], else: [] },
                { name: 'triage', type: 'agent', after: ['any'], app: 'triage',
                    input: 'Triage these failed checks and propose owners: {{ steps.failures.preview }}' },
                { name: 'approve', type: 'approval', after: ['triage'], approvers: [me], message: '{{ steps.triage.output }}' },
                { name: 'tell', type: 'notify', after: ['approve'], message: '{{ steps.triage.output }}' },
            ],
        }),
    },
    {
        id: 'blank',
        title: 'Blank flow',
        blurb: 'One step to start from; add the rest from the palette.',
        make: (name) => ({ kind: 'flow', name, steps: [{ name: 'start', type: 'notify', message: 'Hello from {{ flow.name }}' }] }),
    },
];
