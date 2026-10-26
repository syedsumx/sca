import { enrollmentCommand, runStatusColor } from '../agents/AgentFleet';
import { planChips } from '../ai/AskCairnPanel';
import { draftFromPayload } from '../ai/DocDraftReview';
import { datasetNameFromUrn, formatNumber, formatRelative, platformFromUrn } from '../common/format';
import { parseJsonObject } from '../governance/AccessSimulator';
import { actionsForState } from '../governance/CertificationInbox';
import { lintPolicyText } from '../governance/PolicyEditor';
import { cleanConfig, toMonitorInput } from '../quality/MonitorEditor';
import { summarize } from '../quality/QualityDashboard';
import { visibleRoutes } from '../routes';

describe('format', () => {
    const urn = 'urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.orders,PROD)';
    it('parses dataset urns', () => {
        expect(datasetNameFromUrn(urn)).toBe('db.schema.orders');
        expect(platformFromUrn(urn)).toBe('snowflake');
        expect(datasetNameFromUrn('urn:li:chart:x')).toBe('urn:li:chart:x');
    });
    it('formats numbers and relative times', () => {
        expect(formatNumber(1234567)).toBe('1.23M');
        expect(formatNumber(null)).toBe('—');
        expect(formatNumber(42)).toBe('42');
        const now = Date.parse('2026-01-01T01:00:00Z');
        expect(formatRelative('2026-01-01T00:59:30Z', now)).toBe('30s ago');
        expect(formatRelative('2026-01-01T00:00:00Z', now)).toBe('1h ago');
        expect(formatRelative(null, now)).toBe('never');
    });
});

describe('policy lint', () => {
    it('accepts a bundle object', () => {
        const r = lintPolicyText(JSON.stringify({ policies: [{ id: 'a', effect: 'deny', actions: ['*'], priority: 1 }] }));
        expect(r.errors).toEqual([]);
        expect(r.policies).toHaveLength(1);
    });
    it('reports structural errors', () => {
        const r = lintPolicyText(JSON.stringify([{ id: 'a', effect: 'maybe', actions: [] }, { id: 'a', effect: 'allow', actions: ['X'] }]));
        expect(r.policies).toBeNull();
        expect(r.errors).toEqual([
            'policies[0]: "effect" must be "allow" or "deny"',
            'policies[0]: "actions" must be a non-empty array',
            'policies[1]: duplicate id "a"',
        ]);
        expect(lintPolicyText('{').errors[0]).toMatch(/^Invalid JSON/);
    });
    it('parseJsonObject', () => {
        expect(parseJsonObject('x', '  ')).toBeUndefined();
        expect(parseJsonObject('x', '{"a":1}')).toEqual({ a: 1 });
        expect(() => parseJsonObject('Env', '[1]')).toThrow('Env: must be a JSON object');
    });
});

describe('workflow + monitor helpers', () => {
    it('actionsForState', () => {
        expect(actionsForState('IN_REVIEW')).toEqual(['approve', 'request-changes', 'reject']);
        expect(actionsForState('CERTIFIED')).toEqual(['revoke']);
        expect(actionsForState('REJECTED')).toEqual([]);
    });
    it('toMonitorInput builds schedule and cleans config', () => {
        expect(cleanConfig({ a: '', b: 0, c: null, d: 'x' })).toEqual({ b: 0, d: 'x' });
        const input = toMonitorInput({
            name: ' n ',
            dataset_urn: 'urn:li:dataset:(a,b,PROD)',
            type: 'volume',
            schedule_mode: 'cron',
            cron: ' 0 * * * * ',
            interval_seconds: 60,
            sensitivity: 'high',
            severity: 'LOW',
            enabled: true,
            config: { min_rows: 10, filter_sql: '' },
        });
        expect(input.name).toBe('n');
        expect(input.schedule).toEqual({ cron: '0 * * * *' });
        expect(input.config).toEqual({ min_rows: 10 });
    });
    it('summarize counts last statuses', () => {
        const base = { last_evaluation: null } as never;
        const s = summarize([
            { ...(base as object), last_evaluation: { status: 'FAIL' } },
            { ...(base as object), last_evaluation: { status: 'PASS' } },
            { ...(base as object) },
        ] as never);
        expect(s).toMatchObject({ total: 3, FAIL: 1, PASS: 1 });
    });
});

describe('ai + agents helpers', () => {
    it('planChips', () => {
        expect(
            planChips({
                query: 'revenue',
                entity_types: ['DATASET'],
                filters: [{ field: 'platform', condition: 'EQUAL', values: ['snowflake', 'bigquery'] }],
                lineage: { direction: 'UPSTREAM', urn: 'urn:li:dashboard:x', max_hops: 3 },
                sort: null,
            }),
        ).toEqual([
            'query: revenue',
            'type: DATASET',
            'platform equal snowflake | bigquery',
            'upstream of urn:li:dashboard:x (≤3 hops)',
        ]);
    });
    it('draftFromPayload accepts nested or flat drafts and rejects junk', () => {
        const d = { description: 'x', columns: [] };
        expect(draftFromPayload({ draft: d })?.description).toBe('x');
        expect(draftFromPayload(d)?.confidence).toBe(0);
        expect(draftFromPayload({ foo: 1 })).toBeNull();
    });
    it('enrollmentCommand and runStatusColor', () => {
        const cmd = enrollmentCommand('https://gw', 'finance', 'tok');
        expect(cmd).toContain('CAIRN_ENROLLMENT_TOKEN=tok');
        expect(cmd).toContain('CAIRN_AGENT_POOL=finance');
        expect(runStatusColor('succeeded')).toBe('green');
        expect(runStatusColor('FAILED')).toBe('red');
    });
    it('visibleRoutes hides privileged pages', () => {
        const paths = visibleRoutes([]).map((r) => r.path);
        expect(paths).toContain('/enterprise/quality');
        expect(paths).not.toContain('/enterprise/agents');
        expect(visibleRoutes(['MANAGE_INGESTION']).map((r) => r.path)).toContain('/enterprise/agents');
    });
});
