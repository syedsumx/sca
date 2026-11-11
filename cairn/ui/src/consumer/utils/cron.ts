/** Cron expressions: checked and put in words (the forms' preview, and schedules across the workspace). */
const CRON_RANGES: [number, number][] = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]];
const NAMES: Record<number, string[]> = {
    3: ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'],
    4: ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'],
};

function cronNumber(tok: string, i: number): number | null {
    const names = NAMES[i];
    if (names) {
        const at = names.indexOf(tok.toUpperCase());
        if (at >= 0) return i === 3 ? at + 1 : at;
    }
    if (!/^\d+$/.test(tok)) return null;
    const n = Number(tok);
    const [lo, hi] = CRON_RANGES[i];
    return n >= lo && n <= hi ? n : null;
}

/** Five fields (minute hour day-of-month month day-of-week), each `*`, numbers, ranges, lists and steps. */
export function validCron(expr: string): boolean {
    const parts = expr.trim().split(/\s+/);
    if (parts.length !== 5) return false;
    return parts.every((p, i) => p.split(',').every((item) => {
        const [base, step] = item.split('/');
        if (step !== undefined && !/^\d+$/.test(step)) return false;
        if (step !== undefined && Number(step) === 0) return false;
        if (base === '*') return true;
        const [a, b] = base.split('-');
        if (cronNumber(a, i) === null) return false;
        return b === undefined || cronNumber(b, i) !== null;
    }));
}

export type CronWords =
    | { key: 'cron.everyMinute' }
    | { key: 'cron.everyNMinutes'; n: number }
    | { key: 'cron.hourly'; minute: string }
    | { key: 'cron.everyNHours'; n: number; minute: string }
    | { key: 'cron.daily'; time: string }
    | { key: 'cron.weekdays'; time: string }
    | { key: 'cron.weekly'; days: number[]; time: string }
    | { key: 'cron.monthly'; day: number; time: string }
    | { key: 'cron.custom' };

/** A cron schedule in words (UTC), for the common shapes; anything else is "custom". */
export function describeCron(expr: string): CronWords | null {
    if (!validCron(expr)) return null;
    const [mi, h, dom, mon, dow] = expr.trim().split(/\s+/);
    const num = (s: string) => /^\d+$/.test(s);
    const time = () => `${h.padStart(2, '0')}:${mi.padStart(2, '0')}`;
    if (mon !== '*') return { key: 'cron.custom' };
    if (mi === '*' && h === '*' && dom === '*' && dow === '*') return { key: 'cron.everyMinute' };
    let m = /^\*\/(\d+)$/.exec(mi);
    if (m && h === '*' && dom === '*' && dow === '*') return { key: 'cron.everyNMinutes', n: Number(m[1]) };
    if (num(mi) && h === '*' && dom === '*' && dow === '*') return { key: 'cron.hourly', minute: mi.padStart(2, '0') };
    m = /^\*\/(\d+)$/.exec(h);
    if (num(mi) && m && dom === '*' && dow === '*') return { key: 'cron.everyNHours', n: Number(m[1]), minute: mi.padStart(2, '0') };
    if (!num(mi) || !num(h)) return { key: 'cron.custom' };
    if (dom === '*' && dow === '*') return { key: 'cron.daily', time: time() };
    if (dom === '*') {
        if (dow === '1-5' || dow.toUpperCase() === 'MON-FRI') return { key: 'cron.weekdays', time: time() };
        const days: number[] = [];
        for (const item of dow.split(',')) {
            const [a, b] = item.split('-');
            const x = cronNumber(a, 4);
            const y = b === undefined ? x : cronNumber(b, 4);
            if (x === null || y === null || item.includes('/')) return { key: 'cron.custom' };
            for (let d = x; d <= y; d++) days.push(d % 7);
        }
        return { key: 'cron.weekly', days, time: time() };
    }
    if (num(dom) && dow === '*') return { key: 'cron.monthly', day: Number(dom), time: time() };
    return { key: 'cron.custom' };
}

