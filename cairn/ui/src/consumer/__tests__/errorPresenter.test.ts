/**
 * Every page shows API errors through the one presenter (`utils/errors.ts`, `components/ErrorNotice.tsx`):
 * a sentence, what to do, and the API's words under "Details". This scans the UI sources for the ways a page
 * used to put an error's raw text on screen, so a new one fails here rather than in front of a person.
 *
 * A line that shows a local error (a YAML or JSON parse error the person just typed, a field's own check) may
 * opt out with a `presenter-exempt: <reason>` comment on that line or the line above.
 */
import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative } from 'path';

import { describeError, presentError } from '../utils/errors';

const SRC = join(__dirname, '..', '..');

/** Where errors are built or presented, not shown. */
const ALLOWED_FILES = new Set([
    'consumer/utils/errors.ts',
    'consumer/components/ErrorNotice.tsx',
    'api/client.ts',
    'consumer/api/client.ts',
    'embed/api.ts',
]);

const RAW_PATTERNS: [string, RegExp][] = [
    ['(e as Error).message', /\(\s*\w+\s+as\s+(?:Error|[A-Z]\w*Error)\b[^)]*\)\.message\b/],
    ['e instanceof Error ? e.message', /instanceof\s+Error\s*\?\s*\w+\.message\b/],
    ['query.error.message', /\.error\??\.message\b/],
    ['err.message', /(?<![\w.])(?:e|err|error|ex|exc|reason)\??\.message\b/],
    ['String(err)', /\bString\(\s*(?:e|err|error|ex|exc|reason)\s*\)/],
    ['body.detail', /\.body\b[^;\n]*\??\.detail\b|\(\s*\w+\s+as\s+[^)]*detail[^)]*\)/],
];

function sources(dir: string): string[] {
    const out: string[] = [];
    for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) {
            if (name !== '__tests__' && name !== 'node_modules') out.push(...sources(path));
        } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name)) {
            out.push(path);
        }
    }
    return out;
}

export function rawErrorSites(): string[] {
    const found: string[] = [];
    for (const file of sources(SRC)) {
        const rel = relative(SRC, file).split('\\').join('/');
        if (ALLOWED_FILES.has(rel)) continue;
        const lines = readFileSync(file, 'utf8').split('\n');
        lines.forEach((line, i) => {
            if (/^\s*(\/\/|\*)/.test(line)) return;
            if (/presenter-exempt:\s*\S/.test(line) || /presenter-exempt:\s*\S/.test(lines[i - 1] ?? '')) return;
            for (const [what, re] of RAW_PATTERNS) {
                if (re.test(line)) {
                    found.push(`${rel}:${i + 1} ${what}: ${line.trim()}`);
                    break;
                }
            }
        });
    }
    return found;
}

describe('the error presenter is the only way an error reaches the screen', () => {
    it('no page renders an error’s raw text', () => {
        expect(rawErrorSites()).toEqual([]);
    });

    it('the scan catches each raw pattern', () => {
        const samples = [
            'message.error((e as Error).message);',
            'setError(e instanceof Error ? e.message : String(e));',
            '<Alert type="error" message={q.error.message} />',
            '{error && <div>{error.message}</div>}',
            'setErr(String(err));',
            'setError((e as Error & { body?: { detail?: string } }).body?.detail);',
        ];
        for (const s of samples) expect(RAW_PATTERNS.some(([, re]) => re.test(s)), s).toBe(true);
        for (const ok of ['notifyError(e)', '<ErrorNotice error={q.error} />', 'setError(e)', 'ev.message', 'p.message'])
            expect(RAW_PATTERNS.some(([, re]) => re.test(ok)), ok).toBe(false);
    });

    it('describeError gives the presenter’s sentence, never the raw text of a server error', () => {
        const boom = Object.assign(new Error('HTTP 500'), { status: 500, path: '/x', body: { detail: 'Traceback (most recent call last)' } });
        const said = describeError(boom);
        expect(said).not.toContain('Traceback');
        expect(presentError(boom, (k) => k).kind).toBe('server');
        expect(said).toBe('Cairn hit an error. Try again. If it keeps happening, tell an admin what you were doing.');
        const forbidden = Object.assign(new Error('HTTP 403'), { status: 403, body: { detail: 'needs MANAGE_POLICIES' } });
        expect(describeError(forbidden, { action: 'Save' })).toContain('MANAGE_POLICIES');
        expect(describeError(forbidden, { action: 'Save' })).toContain('“Save”');
    });
});
