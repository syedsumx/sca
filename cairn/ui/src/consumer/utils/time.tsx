import { Tooltip } from 'antd';
import React from 'react';

import { MessageKey, useI18n } from '../i18n';
import { describeCron } from './cron';

type T = (key: MessageKey, vars?: Record<string, string | number>) => string;

/** A time as the APIs give it (ISO text, epoch milliseconds, or epoch seconds below 1e11) in milliseconds. */
export function toMs(v: string | number | Date | null | undefined): number | null {
    if (v === null || v === undefined || v === '') return null;
    if (v instanceof Date) return v.getTime();
    if (typeof v === 'number') return Number.isFinite(v) ? (v < 1e11 ? v * 1000 : v) : null;
    const n = Date.parse(v);
    return Number.isNaN(n) ? null : n;
}

/**
 * How long ago (or how soon) a time is, in the person's language: "just now", "5 min ago", "5 h ago", "3 d ago",
 * "in 2 h". The one way the workspace says when something happened; the exact time is `exactTime`.
 */
export function relativeText(v: string | number | Date | null | undefined, t: T, now: number = Date.now()): string {
    const ms = toMs(v);
    if (ms === null) return '';
    const s = Math.round((now - ms) / 1000);
    const a = Math.abs(s);
    if (a < 45) return t('time.justNow');
    const steps: [number, MessageKey, MessageKey][] = [
        [31_536_000, 'time.yearsAgo', 'time.inDays'],
        [2_592_000, 'time.monthsAgo', 'time.inDays'],
        [86_400, 'time.daysAgo', 'time.inDays'],
        [3_600, 'time.hoursAgo', 'time.inHours'],
        [60, 'time.minutesAgo', 'time.inMinutes'],
    ];
    for (const [unit, past, future] of steps) {
        if (a >= unit || unit === 60) {
            if (s >= 0) return t(past, { n: Math.max(1, Math.round(a / unit)) });
            // The future is said in days at most ("in 40 d"): schedules are rarely further out.
            const u = unit > 86_400 ? 86_400 : unit;
            return t(future, { n: Math.max(1, Math.round(a / u)) });
        }
    }
    return t('time.justNow');
}

/** The exact time in the person's language and time zone ("11 Nov 2026, 14:05:09"). */
export function exactTime(v: string | number | Date | null | undefined, locale?: string): string {
    const ms = toMs(v);
    if (ms === null) return '';
    return new Date(ms).toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'medium' });
}

/** A time shown relatively ("5 h ago") with the exact time on hover and focus, and machine-readable for assistive tech. */
export function When({ value, empty = '—', now }: { value: string | number | Date | null | undefined; empty?: React.ReactNode;
    now?: number }) {
    const { t, locale } = useI18n();
    const ms = toMs(value);
    if (ms === null) return <>{empty}</>;
    const exact = exactTime(ms, locale);
    return (
        <time dateTime={new Date(ms).toISOString()} title={exact} className="cairn-when" style={{ whiteSpace: 'nowrap' }}>
            {relativeText(ms, t, now)}
        </time>
    );
}

const CRON_SHAPE = /^\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s*$/;

/** True when a value reads as a five-field cron expression. */
export function looksLikeCron(v: unknown): v is string {
    return typeof v === 'string' && CRON_SHAPE.test(v) && /^[\d*/,\-\sA-Za-z?]+$/.test(v) && /[*\d]/.test(v);
}

/**
 * A cron schedule in words for the common shapes ("Every 6 hours at minute 00 (UTC)", "Every day at 02:00
 * Europe/London"); anything else is the cron itself. `tz` is the schedule's time zone (UTC when not given).
 */
export function cronText(expr: string, t: T, locale = 'en', tz?: string | null): string {
    const words = describeCron(expr);
    if (!words || words.key === 'cron.custom') return expr.trim();
    let text: string;
    if (words.key === 'cron.weekly') {
        const fmt = new Intl.DateTimeFormat(locale, { weekday: 'long', timeZone: 'UTC' });
        const days = words.days.map((d) => fmt.format(new Date(Date.UTC(2023, 0, 1 + d)))).join(', ');
        text = t('cron.weekly', { days, time: words.time });
    } else {
        const { key, ...vars } = words;
        text = t(key, vars as Record<string, string | number>);
    }
    if (tz && tz.toUpperCase() !== 'UTC') {
        text = /\bUTC\b/.test(text) ? text.replace(/\bUTC\b/, tz) : `${text} (${tz})`;
    }
    return text;
}

/** A schedule in words, with the cron itself on hover and focus. */
export function Schedule({ cron, tz, empty = '—' }: { cron: string | null | undefined; tz?: string | null; empty?: React.ReactNode }) {
    const { t, locale } = useI18n();
    if (!cron || !cron.trim()) return <>{empty}</>;
    const text = cronText(cron, t, locale, tz);
    const raw = t('cron.raw', { cron: cron.trim() });
    return (
        <Tooltip title={raw}>
            <span className="cairn-schedule" data-cron={cron.trim()}>{text}</span>
        </Tooltip>
    );
}
