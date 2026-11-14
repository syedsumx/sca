// @vitest-environment node
import { COOKIE, LOGIN, LOGOUT, accounts, cookieValue, decide, issue, safeNext, verify } from '../siteAuth';

const ENV = { CAIRN_DEMO_USERS: 'ana:correct-horse-battery\nbob:another-long-one' };
const NOW = 1_800_000_000;
const opts = { now: () => NOW, sleep: async () => undefined };
const SITE = 'https://cairn-demo.example';

function get(path: string, cookie?: string): Request {
    return new Request(SITE + path, { headers: cookie ? { cookie } : {} });
}

function post(fields: Record<string, string>, type = 'application/x-www-form-urlencoded'): Request {
    return new Request(SITE + LOGIN, { method: 'POST', headers: { 'content-type': type }, body: new URLSearchParams(fields).toString() });
}

function sessionFrom(res: Response): string {
    const set = res.headers.get('set-cookie') ?? '';
    const m = new RegExp(`${COOKIE}=([^;]+)`).exec(set);
    if (!m) throw new Error(`no session cookie in ${set}`);
    return `${COOKIE}=${m[1]}`;
}

async function signIn(name = 'ana', password = 'correct-horse-battery', extra: Record<string, string> = {}): Promise<Response> {
    const res = await decide(post({ name, password, next: '/', ...extra }), ENV, opts);
    if (!res) throw new Error('the sign-in post was served as the site');
    return res;
}

describe('the demo site sign-in', () => {
    it('fails closed: with no accounts configured nothing is served, not even with a cookie', async () => {
        for (const env of [{}, { CAIRN_DEMO_USERS: '' }, { CAIRN_DEMO_USERS: 'nopassword:' }, { CAIRN_DEMO_USER: 'ana' }]) {
            const forged = `${COOKIE}=${await issue({ CAIRN_DEMO_USERS: 'ana:x' }, 'ana', NOW + 100)}`;
            const res = await decide(get('/index.html', forged), env, opts);
            expect(res?.status).toBe(503);
            expect(await res!.text()).toContain('not configured');
        }
    });

    it('sends someone not signed in to the sign-in page, keeping where they were going', async () => {
        const res = await decide(get('/docs/governance.html?x=1'), ENV, opts);
        expect(res?.status).toBe(303);
        expect(res!.headers.get('location')).toBe(`${LOGIN}?next=${encodeURIComponent('/docs/governance.html?x=1')}`);
        const page = await decide(get(`${LOGIN}?next=%2Fdocs%2Fgovernance.html`), ENV, opts);
        expect(page?.status).toBe(200);
        const html = await page!.text();
        expect(html).toContain('autocomplete="current-password"');
        expect(html).toContain('value="/docs/governance.html"');
        expect(page!.headers.get('cache-control')).toBe('no-store');
        expect(page!.headers.get('x-frame-options')).toBe('DENY');
    });

    it('refuses anything but reading before sign-in', async () => {
        const res = await decide(new Request(SITE + '/api/x', { method: 'POST' }), ENV, opts);
        expect(res?.status).toBe(401);
    });

    it('signs in with a right password and then serves the site, to the page asked for', async () => {
        const res = await signIn('ana', 'correct-horse-battery', { next: '/fleet/', hash: '#/catalog?q=orders' });
        expect(res.status).toBe(303);
        expect(res.headers.get('location')).toBe('/fleet/#/catalog?q=orders');
        const set = res.headers.get('set-cookie')!;
        for (const flag of ['HttpOnly', 'Secure', 'SameSite=Lax', 'Path=/', 'Max-Age=43200']) expect(set).toContain(flag);
        const cookie = sessionFrom(res);
        expect(await decide(get('/index.html', cookie), ENV, opts)).toBeNull();
        expect(await decide(get('/docs/', cookie), ENV, opts)).toBeNull();
    });

    it('refuses a wrong password and an unknown name the same way, after a pause, with no cookie', async () => {
        let paused = 0;
        const slow = { now: () => NOW, sleep: async () => { paused += 1; } };
        for (const [name, password] of [['ana', 'wrong'], ['nobody', 'correct-horse-battery'], ['ana', ''], ['', '']]) {
            const res = await decide(post({ name, password, next: '/' }), ENV, slow);
            expect(res?.status).toBe(401);
            expect(res!.headers.get('set-cookie')).toBeNull();
            expect(await res!.text()).toContain('do not match');
        }
        expect(paused).toBe(4);
        // A password of another account does not sign in as this one.
        expect((await decide(post({ name: 'ana', password: 'another-long-one' }), ENV, opts))?.status).toBe(401);
    });

    it('does not take a sign-in that is not the form', async () => {
        const res = await decide(new Request(SITE + LOGIN, { method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ name: 'ana', password: 'correct-horse-battery' }) }), ENV, opts);
        expect(res?.status).toBe(415);
    });

    it('never sends someone to another site after signing in', async () => {
        for (const next of ['//evil.example/x', 'https://evil.example', '/\\evil.example', 'javascript:alert(1)', '/ok\nSet-Cookie: x=1',
            `${LOGIN}?next=/x`, `${LOGOUT}`]) {
            expect(safeNext(next, null)).toBe('/');
        }
        expect(safeNext('/docs/', '#/x"><script>')).toBe('/docs/');
        expect(safeNext('/', '#/discover')).toBe('/#/discover');
        const res = await signIn('ana', 'correct-horse-battery', { next: '//evil.example' });
        expect(res.headers.get('location')).toBe('/');
    });

    it('does not believe a forged, altered, expired or orphaned session', async () => {
        const good = await issue(ENV, 'ana', NOW + 60);
        expect(await verify(ENV, good, NOW)).toBe('ana');
        expect(await verify(ENV, good, NOW + 61)).toBeNull();                                  // expired
        expect(await verify(ENV, good.replace('v1.ana.', 'v1.bob.'), NOW)).toBeNull();       // another name, same signature
        const [body, sig] = [good.slice(0, good.lastIndexOf('.')), good.slice(good.lastIndexOf('.') + 1)];
        expect(await verify(ENV, `${body.replace(/\d+$/, String(NOW + 999_999))}.${sig}`, NOW)).toBeNull();  // longer life
        expect(await verify(ENV, `${body}.${sig.startsWith('A') ? 'B' : 'A'}${sig.slice(1)}`, NOW)).toBeNull();
        // The same signature spelt with different unused bits in its last character is not taken either.
        const last = sig[sig.length - 1];
        const alt = 'AEIMQUYcgkosw048'.includes(last) ? String.fromCharCode(last.charCodeAt(0) + 1) : last;
        if (alt !== last) expect(await verify(ENV, `${body}.${sig.slice(0, -1)}${alt}`, NOW)).toBeNull();
        expect(await verify(ENV, 'v1.ana.9999999999.x', NOW)).toBeNull();
        expect(await verify(ENV, await issue({ CAIRN_DEMO_USERS: 'ana:other' }, 'ana', NOW + 60), NOW)).toBeNull();   // other key
        // Removing an account, or changing any password, ends the sessions signed before.
        expect(await verify({ CAIRN_DEMO_USERS: 'bob:another-long-one' }, good, NOW)).toBeNull();
        expect(await verify({ CAIRN_DEMO_USERS: 'ana:changed-password\nbob:another-long-one' }, good, NOW)).toBeNull();
        // With a session key set, sessions survive a password change.
        const keyed = { ...ENV, CAIRN_DEMO_SESSION_KEY: 'k'.repeat(40) };
        const kept = await issue(keyed, 'ana', NOW + 60);
        expect(await verify({ ...keyed, CAIRN_DEMO_USERS: 'ana:changed-password' }, kept, NOW)).toBe('ana');
    });

    it('signs out', async () => {
        const res = await decide(get(LOGOUT, sessionFrom(await signIn())), ENV, opts);
        expect(res?.status).toBe(303);
        expect(res!.headers.get('location')).toBe(LOGIN);
        expect(res!.headers.get('set-cookie')).toContain('Max-Age=0');
    });

    it('reads accounts from either form, ignoring malformed ones', () => {
        expect([...accounts({ CAIRN_DEMO_USERS: ' ana : pw1 ;bob:pw:with:colons\n\nbad name:x\n:nopw\nnoseparator' }).keys()])
            .toEqual(['ana', 'bob']);
        expect(accounts({ CAIRN_DEMO_USERS: 'bob:pw:with:colons' }).get('bob')).toBe('pw:with:colons');
        expect([...accounts({ CAIRN_DEMO_USER: 'demo', CAIRN_DEMO_PASSWORD: 'pw' }).entries()]).toEqual([['demo', 'pw']]);
        expect(cookieValue(get('/', `a=1; ${COOKIE}=tok; b=2`))).toBe('tok');
    });

    it('escapes what it writes back into the page', async () => {
        const page = await decide(get(`${LOGIN}?next=${encodeURIComponent('/"><img src=x onerror=alert(1)>')}`), ENV, opts);
        const html = await page!.text();
        expect(html).not.toContain('<img src=x');
    });
});
