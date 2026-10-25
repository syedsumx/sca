/** Display helpers shared by consumer surfaces. */

export function shortUrn(urn: string): string {
    // urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.table,PROD) → table
    const m = urn.match(/^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,([^,]+),[A-Z_]+\)$/);
    if (m) return m[1].split('.').pop() || m[1];
    const tail = urn.split(':').slice(3).join(':');
    return (tail || urn).replace(/^\(|\)$/g, '');
}

export function actorName(urn: string): string {
    return urn.replace(/^urn:li:(corpuser|corpGroup):/, '');
}

export function platformName(urnOrName: string | null | undefined): string {
    if (!urnOrName) return '';
    return urnOrName.replace(/^urn:li:dataPlatform:/, '');
}

export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string {
    if (!iso) return '';
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return iso;
    const s = Math.round((now - t) / 1000);
    if (s < 45) return 'just now';
    const units: [number, string][] = [
        [60, 'min'],
        [3600, 'h'],
        [86400, 'd'],
        [2592000, 'mo'],
    ];
    let label = `${Math.round(s / 60)} min`;
    for (let i = units.length - 1; i >= 0; i -= 1) {
        if (s >= units[i][0]) {
            const v = Math.round(s / units[i][0]);
            label = `${v} ${units[i][1]}`;
            break;
        }
    }
    return s >= 0 ? `${label} ago` : `in ${label.replace('-', '')}`;
}

export function entityTypeLabel(type: string | null | undefined): string {
    if (!type) return '';
    const map: Record<string, string> = {
        DATASET: 'Dataset',
        DASHBOARD: 'Dashboard',
        CHART: 'Chart',
        DATA_JOB: 'Pipeline task',
        DATA_FLOW: 'Pipeline',
        DATA_PRODUCT: 'Data product',
        GLOSSARY_TERM: 'Business term',
        DOMAIN: 'Domain',
    };
    return map[type.toUpperCase()] ?? type.charAt(0).toUpperCase() + type.slice(1).toLowerCase().replace(/_/g, ' ');
}

export type TrustTone = 'good' | 'warn' | 'bad' | 'neutral';

export function qualityTone(status: string | null | undefined): TrustTone {
    switch ((status || '').toUpperCase()) {
        case 'PASS':
        case 'HEALTHY':
            return 'good';
        case 'WARN':
        case 'WARNING':
        case 'TRAINING':
        case 'LEARNING':
            return 'warn';
        case 'FAIL':
        case 'FAILING':
        case 'ERROR':
        case 'UNHEALTHY':
            return 'bad';
        default:
            return 'neutral';
    }
}

export function qualityLabel(status: string | null | undefined): string {
    switch (qualityTone(status)) {
        case 'good':
            return 'Healthy';
        case 'warn':
            return ['TRAINING', 'LEARNING'].includes((status || '').toUpperCase()) ? 'Learning' : 'Warning';
        case 'bad':
            return 'Failing';
        default:
            return 'Untested';
    }
}

export const TONE_COLORS: Record<TrustTone, string> = {
    good: '#2e7d32',
    warn: '#b26a00',
    bad: '#c62828',
    neutral: '#6b7280',
};

export function plural(n: number, word: string): string {
    return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
}
