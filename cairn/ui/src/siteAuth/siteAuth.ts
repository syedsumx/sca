/**
 * Sign-in for the hosted demo site (the Vercel deployment of `npm run build:site`): every page, the docs and the fleet
 * console included, is served only to someone signed in. It runs on the server (Vercel Routing Middleware,
 * `middleware.ts` at the project root), so nothing in the browser can skip it.
 *
 * * **Who may sign in.** `CAIRN_DEMO_USERS` holds one `name:password` per line (or separated by `;`), or
 *   `CAIRN_DEMO_USER` and `CAIRN_DEMO_PASSWORD` hold one. They are set in the Vercel project's environment variables
 *   (Sensitive), never in the repository. With none set the site serves nothing but a page saying sign-in is not
 *   configured: it fails closed.
 * * **The session.** A cookie (HttpOnly, Secure, SameSite=Lax) holding the name and an expiry, signed with HMAC-SHA-256
 *   under a key derived from `CAIRN_DEMO_SESSION_KEY` when set, else from the sign-in list itself, so changing a
 *   password signs everyone out. It lasts 12 hours. `/__logout` ends it.
 * * **Guessing.** A wrong password is answered after a pause; there is no lockout (the middleware keeps no state), so
 *   the passwords must be long and random.
 */

export const COOKIE = 'cairn_demo_session';
export const TTL_S = 12 * 60 * 60;
export const LOGIN = '/__login';
export const LOGOUT = '/__logout';
const FAIL_DELAY_MS = 700;

export interface SiteAuthEnv {
    CAIRN_DEMO_USERS?: string;
    CAIRN_DEMO_USER?: string;
    CAIRN_DEMO_PASSWORD?: string;
    CAIRN_DEMO_SESSION_KEY?: string;
}

export interface SiteAuthOptions {
    now?: () => number;                       // seconds
    sleep?: (ms: number) => Promise<void>;
}

/** What to do with a request: serve the site (`null`), or this response. */
export type Decision = Response | null;

const enc = new TextEncoder();

/** The accounts from the environment: `name -> password`. Malformed entries are ignored; an empty name or password
 * never signs anyone in. */
export function accounts(env: SiteAuthEnv): Map<string, string> {
    const out = new Map<string, string>();
    const add = (entry: string) => {
        const i = entry.indexOf(':');
        if (i <= 0) return;
        const name = entry.slice(0, i).trim();
        const password = entry.slice(i + 1).trim();
        if (name && password && /^[A-Za-z0-9._@-]{1,64}$/.test(name)) out.set(name, password);
    };
    for (const entry of (env.CAIRN_DEMO_USERS ?? '').split(/[\n;]/)) add(entry);
    if (env.CAIRN_DEMO_USER && env.CAIRN_DEMO_PASSWORD) add(`${env.CAIRN_DEMO_USER}:${env.CAIRN_DEMO_PASSWORD}`);
    return out;
}

async function hmacKey(secret: string): Promise<CryptoKey> {
    const raw = await crypto.subtle.digest('SHA-256', enc.encode(`cairn-demo-session\n${secret}`));
    return crypto.subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
}

function sessionSecret(env: SiteAuthEnv, users: Map<string, string>): string {
    if (env.CAIRN_DEMO_SESSION_KEY) return env.CAIRN_DEMO_SESSION_KEY;
    return [...users.entries()].sort().map(([n, p]) => `${n}:${p}`).join('\n');
}

const b64url = (bytes: ArrayBuffer): string =>
    btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

function fromB64url(s: string): Uint8Array | null {
    if (!/^[A-Za-z0-9_-]*$/.test(s)) return null;
    try {
        const bin = atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (s.length % 4)) % 4));
        return Uint8Array.from(bin, (c) => c.charCodeAt(0));
    } catch {
        return null;
    }
}

/** A signed session for `name`, valid until `exp` (seconds). */
export async function issue(env: SiteAuthEnv, name: string, exp: number): Promise<string> {
    const users = accounts(env);
    const body = `v1.${name}.${exp}`;
    const sig = await crypto.subtle.sign('HMAC', await hmacKey(sessionSecret(env, users)), enc.encode(body));
    return `${body}.${b64url(sig)}`;
}

/** The signed-in name, when the cookie's session is genuine, unexpired and for an account that still exists. */
export async function verify(env: SiteAuthEnv, token: string | null, now: number): Promise<string | null> {
    if (!token) return null;
    const m = /^v1\.([A-Za-z0-9._@-]{1,64})\.(\d{1,12})\.([A-Za-z0-9_-]{43})$/.exec(token);
    if (!m) return null;
    const [, name, expText, sigText] = m;
    const users = accounts(env);
    if (!users.has(name) || Number(expText) <= now) return null;
    const sig = fromB64url(sigText);
    if (!sig || sig.length !== 32 || b64url(sig.buffer as ArrayBuffer) !== sigText) return null;   // one spelling only
    const ok = await crypto.subtle.verify('HMAC', await hmacKey(sessionSecret(env, users)), sig,
        enc.encode(`v1.${name}.${expText}`));
    return ok ? name : null;
}

/** Whether `given` is `expected`, in time that does not depend on where they differ. */
async function same(given: string, expected: string): Promise<boolean> {
    const key = await crypto.subtle.importKey('raw', crypto.getRandomValues(new Uint8Array(32)),
        { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    const [a, b] = await Promise.all([given, expected].map((s) => crypto.subtle.sign('HMAC', key, enc.encode(s))));
    const x = new Uint8Array(a);
    const y = new Uint8Array(b);
    let diff = 0;
    for (let i = 0; i < x.length; i += 1) diff |= x[i] ^ y[i];
    return diff === 0;
}

export function cookieValue(request: Request, name = COOKIE): string | null {
    for (const part of (request.headers.get('cookie') ?? '').split(';')) {
        const i = part.indexOf('=');
        if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
    }
    return null;
}

/** Where to go after signing in: a path on this site only (never another host), with the page's `#/…` route kept. */
export function safeNext(next: string | null, hash: string | null): string {
    let path = next && /^\/(?![/\\])[^\s\\]*$/.test(next) && !next.startsWith(LOGIN) && !next.startsWith(LOGOUT) ? next : '/';
    if (path.length > 2000) path = '/';
    const route = hash && /^#\/[A-Za-z0-9/_\-.~%:=&?,+@]{0,500}$/.test(hash) ? hash : '';
    return path + route;
}

const SECURITY_HEADERS: Record<string, string> = {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'X-Robots-Tag': 'noindex, nofollow',
    'Content-Security-Policy':
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
};

const escapeHtml = (s: string) =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

/** The sign-in page (also the "not configured" page, without the form). */
export function loginPage(opts: { next: string; error?: string; configured: boolean; status?: number }): Response {
    const form = opts.configured
        ? `<form method="post" action="${LOGIN}" autocomplete="on">
    <input type="hidden" name="next" value="${escapeHtml(opts.next)}">
    <input type="hidden" name="hash" id="hash" value="">
    ${opts.error ? `<p class="error" role="alert">${escapeHtml(opts.error)}</p>` : ''}
    <label for="name">User name</label>
    <input id="name" name="name" autocomplete="username" required autofocus maxlength="64">
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required maxlength="512">
    <button type="submit">Sign in</button>
  </form>
  <script>document.getElementById('hash').value = location.hash.slice(0, 500);</script>`
        : `<p role="alert">Sign-in is not configured for this site, so nothing is shown. The site's owner sets
    <code>CAIRN_DEMO_USERS</code> in the hosting project's environment variables and deploys again.</p>`;
    const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Sign in · Cairn</title>
<style>
  :root { color-scheme: light; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #F6F7F9; color: #0f172a;
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
  main { width: min(380px, calc(100vw - 32px)); background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
    padding: 28px 24px; box-shadow: 0 1px 3px rgba(15, 23, 42, .08); }
  h1 { font-size: 22px; margin: 0 0 4px; }
  p.lead { margin: 0 0 20px; color: #475569; font-size: 14px; }
  label { display: block; font-size: 14px; font-weight: 600; margin: 14px 0 6px; }
  input:not([type=hidden]) { box-sizing: border-box; width: 100%; height: 44px; padding: 0 12px; font-size: 16px;
    border: 1px solid #94a3b8; border-radius: 8px; }
  input:focus-visible, button:focus-visible { outline: 3px solid #4f46e5; outline-offset: 2px; }
  button { margin-top: 20px; width: 100%; height: 44px; border: 0; border-radius: 8px; background: #4338ca;
    color: #fff; font-size: 16px; font-weight: 600; cursor: pointer; }
  .error { background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; border-radius: 8px; padding: 8px 12px;
    font-size: 14px; }
  code { font-size: 13px; }
</style>
</head>
<body>
<main>
  <h1>Cairn</h1>
  <p class="lead">Sign in to see the demo workspace.</p>
  ${form}
</main>
</body>
</html>`;
    return new Response(html, {
        status: opts.status ?? (opts.configured ? 200 : 503),
        headers: { ...SECURITY_HEADERS, 'Content-Type': 'text/html; charset=utf-8' },
    });
}

function redirect(location: string, cookie?: string): Response {
    const headers: Record<string, string> = { ...SECURITY_HEADERS, Location: location };
    const res = new Response(null, { status: 303, headers });
    if (cookie) res.headers.append('Set-Cookie', cookie);
    return res;
}

/**
 * Decides one request. `null` means "serve the site"; otherwise the response to send (the sign-in page, a redirect
 * to it, or a refusal).
 */
export async function decide(request: Request, env: SiteAuthEnv, options: SiteAuthOptions = {}): Promise<Decision> {
    const now = (options.now ?? (() => Math.floor(Date.now() / 1000)))();
    const sleep = options.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
    const url = new URL(request.url);
    const users = accounts(env);
    const configured = users.size > 0;

    if (url.pathname === LOGOUT) {
        return redirect(LOGIN, `${COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`);
    }
    if (!configured) return loginPage({ next: '/', configured: false });

    if (url.pathname === LOGIN) {
        if (request.method === 'POST') {
            const type = request.headers.get('content-type') ?? '';
            if (!type.startsWith('application/x-www-form-urlencoded') && !type.startsWith('multipart/form-data')) {
                return new Response('Sign in with the form.', { status: 415, headers: SECURITY_HEADERS });
            }
            let form: FormData;
            try {
                form = await request.formData();
            } catch {
                return new Response('Sign in with the form.', { status: 400, headers: SECURITY_HEADERS });
            }
            const name = String(form.get('name') ?? '').trim();
            const password = String(form.get('password') ?? '');
            const next = safeNext(String(form.get('next') ?? '/'), String(form.get('hash') ?? ''));
            const expected = users.get(name);
            // Compared even when the name is unknown, so the time taken does not say which names exist.
            const ok = (await same(password, expected ?? `\u0000${password}`)) && expected !== undefined;
            if (!ok) {
                await sleep(FAIL_DELAY_MS);
                return loginPage({ next: safeNext(String(form.get('next') ?? '/'), null), configured: true, status: 401,
                    error: 'That user name and password do not match.' });
            }
            const token = await issue(env, name, now + TTL_S);
            return redirect(next, `${COOKIE}=${token}; Path=/; Max-Age=${TTL_S}; HttpOnly; Secure; SameSite=Lax`);
        }
        if (await verify(env, cookieValue(request), now)) return redirect(safeNext(url.searchParams.get('next'), null));
        return loginPage({ next: safeNext(url.searchParams.get('next'), null), configured: true });
    }

    if (await verify(env, cookieValue(request), now)) return null;
    if (request.method === 'GET' || request.method === 'HEAD') {
        const back = safeNext(url.pathname + url.search, null);
        return redirect(back === '/' ? LOGIN : `${LOGIN}?next=${encodeURIComponent(back)}`);
    }
    return new Response('Sign in first.', { status: 401, headers: SECURITY_HEADERS });
}
