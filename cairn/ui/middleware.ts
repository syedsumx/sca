// Vercel Routing Middleware for the hosted demo site: nothing is served before signing in. The logic, and what it
// reads from the environment, is in src/siteAuth/siteAuth.ts (tested in src/siteAuth/__tests__).
import { decide } from './src/siteAuth/siteAuth';

export const config = { matcher: '/:path*' };

export default async function middleware(request: Request): Promise<Response> {
    const decision = await decide(request, {
        CAIRN_DEMO_USERS: process.env.CAIRN_DEMO_USERS,
        CAIRN_DEMO_USER: process.env.CAIRN_DEMO_USER,
        CAIRN_DEMO_PASSWORD: process.env.CAIRN_DEMO_PASSWORD,
        CAIRN_DEMO_SESSION_KEY: process.env.CAIRN_DEMO_SESSION_KEY,
    });
    // Serve the site: Vercel's "carry on" answer from middleware (what next() from @vercel/functions returns).
    return decision ?? new Response(null, { headers: { 'x-middleware-next': '1' } });
}

declare const process: { env: Record<string, string | undefined> };
