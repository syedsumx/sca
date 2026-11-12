/* The fleet console's sample data: fictional customers' installs, answered in the browser. Changes last until reload. */
import type { FleetApi, FleetInstall, FleetSummary } from './FleetConsole';
import { ATTENTION } from './FleetConsole';

const ago = (h: number) => new Date(Date.now() - h * 3600_000).toISOString().replace(/\.\d+Z$/, 'Z');
const inDays = (d: number) => new Date(Date.now() + d * 86400_000).toISOString().slice(0, 10);
const ok = (...names: string[]) => Object.fromEntries(names.map((n) => [n, 'ok']));
const last = (version: string, o: Record<string, unknown> = {}) => ({
    version, channel: 'stable',
    licence: { id: 'lic_x', edition: 'enterprise', status: 'valid', expires_at: inDays(220), seats_used: 140, seats_limit: 250 },
    updates: { target: null, severity: null, checked_at: ago(1), last_plan: { target: version, status: 'succeeded', finished_at: ago(240) } },
    health: { components: ok('api', 'pdp', 'gateway', 'core') }, platform: { runtime: 'kubernetes' }, ...o,
});

let seq = 0;
const installs: FleetInstall[] = [
    { id: 'inst_8c1f2a0e9b7d4c61', name: 'acme-prod', customer: 'Acme Retail', channel: 'stable', licence_id: 'lic_acme',
        created_at: ago(2400), last_seen_at: ago(0.4), version: '2.14.0', attention: [], last: last('2.14.0') },
    { id: 'inst_1b6d0f3c5a2e8d77', name: 'acme-staging', customer: 'Acme Retail', channel: 'early', licence_id: 'lic_acme',
        created_at: ago(2400), last_seen_at: ago(0.7), version: '2.15.0-rc.1', attention: [],
        last: last('2.15.0-rc.1', { channel: 'early' }) },
    { id: 'inst_4e9a7c2b1d0f6e33', name: 'northwind-eu', customer: 'Northwind Logistics', channel: 'stable', licence_id: 'lic_nw',
        created_at: ago(1900), last_seen_at: ago(0.2), version: '2.12.0', attention: [],
        last: last('2.12.0', { updates: { target: '2.14.0', severity: 'security', checked_at: ago(0.2),
            last_plan: { target: '2.12.0', status: 'succeeded', finished_at: ago(600) } } }) },
    { id: 'inst_7d3b5e1a9c4f2b88', name: 'contoso-dw', customer: 'Contoso Health', channel: 'lts', licence_id: 'lic_ct',
        created_at: ago(3100), last_seen_at: ago(0.9), version: '2.10.4', attention: [],
        last: last('2.10.4', { channel: 'lts', updates: { target: '2.10.5', severity: 'normal', checked_at: ago(0.9),
            last_plan: { target: '2.10.5', status: 'rolled_back', finished_at: ago(30) } },
            licence: { id: 'lic_ct', edition: 'enterprise', status: 'valid', expires_at: inDays(19), seats_used: 61, seats_limit: 75 } }) },
    { id: 'inst_2f8c6a4d0b1e9c55', name: 'fabrikam-prod', customer: 'Fabrikam Energy', channel: 'stable', licence_id: 'lic_fb',
        created_at: ago(1500), last_seen_at: ago(27), version: '2.13.1', attention: [], last: last('2.13.1') },
    { id: 'inst_9a1e4c7b3d5f0a22', name: 'globex-analytics', customer: 'Globex', channel: 'stable', licence_id: 'lic_gx',
        created_at: ago(900), last_seen_at: ago(0.3), version: '2.14.0', attention: [],
        last: last('2.14.0', { health: { components: { api: 'ok', pdp: 'ok', gateway: 'down', core: 'ok' } },
            licence: { id: 'lic_gx', edition: 'team', status: 'valid', expires_at: inDays(300), seats_used: 31, seats_limit: 25, over_seats: true } }) },
];

const ORDER = Object.keys(ATTENTION);
function attentionOf(i: FleetInstall): string[] {
    const out: string[] = [];
    const l = i.last || {};
    if (!i.last_seen_at || Date.now() - Date.parse(i.last_seen_at) > 3 * 3600_000) out.push('silent');
    if (Object.values(l.health?.components ?? {}).some((s) => s !== 'ok')) out.push('unhealthy');
    if (['failed', 'rolled_back'].includes(l.updates?.last_plan?.status)) out.push('update_failed');
    if (l.updates?.target) out.push(['security', 'critical'].includes(l.updates.severity) ? 'security_update' : 'update');
    if (l.licence?.expires_at && (Date.parse(l.licence.expires_at) - Date.now()) / 86400_000 <= 30) out.push('licence_expiring');
    if (l.licence?.over_seats) out.push('over_seats');
    return out.sort((a, b) => ORDER.indexOf(a) - ORDER.indexOf(b));
}
installs.forEach((i) => { i.attention = attentionOf(i); });
const rank = (i: FleetInstall) => Math.min(...i.attention.map((a) => ORDER.indexOf(a)), 99);

function key(): string {
    const b = new Uint8Array(32);
    crypto.getRandomValues(b);
    return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}

export const demoFleet: FleetApi = {
    async summary(): Promise<FleetSummary> {
        const attention: Record<string, number> = {};
        const versions: Record<string, number> = {};
        for (const i of installs) {
            i.attention.forEach((a) => { attention[a] = (attention[a] ?? 0) + 1; });
            versions[i.version ?? 'unknown'] = (versions[i.version ?? 'unknown'] ?? 0) + 1;
        }
        return { installs: installs.length, healthy: installs.filter((i) => !i.attention.length).length, attention, versions };
    },
    async installs() {
        return [...installs].sort((a, b) => rank(a) - rank(b) || a.customer.localeCompare(b.customer));
    },
    async install(id) {
        const i = installs.find((x) => x.id === id);
        if (!i) throw new Error(`no install ${id}`);
        const history = Array.from({ length: 6 }, (_, n) => ({ received_at: ago((n + 1) * 1.0 + (i.last_seen_at ? 0 : 50)),
            version: i.version, updates: i.last?.updates }));
        return { ...i, history };
    },
    async register(body) {
        const id = `inst_demo${(seq++).toString(16).padStart(10, '0')}`;
        const made: FleetInstall = { id, name: body.name, customer: body.customer, channel: body.channel || 'stable',
            licence_id: null, created_at: ago(0), last_seen_at: null, version: null, attention: ['silent'], last: {} };
        installs.push(made);
        return { ...made, key: key() };
    },
    async rotate(id) { return { id, key: key() }; },
    async remove(id) { const n = installs.findIndex((x) => x.id === id); if (n >= 0) installs.splice(n, 1); },
};
