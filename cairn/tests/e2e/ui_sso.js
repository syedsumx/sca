// Headless check used by test_sso: login page → "Sign in with SSO" → test identity provider →
// back in Cairn, signed in. Prints one JSON line: {ssoButton, loginText, signedIn, me, errors}.
// The provider is cairn-oidc on the stack network; the browser reaches it through its published port.
const { chromium } = require('playwright');

(async () => {
  const { EDGE, SSO_USER, SHOTS } = process.env;
  const browser = await chromium.launch({ args: ['--host-resolver-rules=MAP cairn-oidc 127.0.0.1'] });
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  const errors = [];
  const result = { ssoButton: false, loginText: '', signedIn: false, me: null, errors };
  try {
    await page.goto(`${EDGE}/login`, { waitUntil: 'networkidle' });
    const sso = page.getByRole('button', { name: /sso|single sign/i }).or(page.getByRole('link', { name: /sso/i }));
    await sso.first().waitFor({ timeout: 30000 });
    result.ssoButton = true;
    result.loginText = `${await page.title()}\n${await page.evaluate(() => document.body.innerText)}`;
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/sso-login.png` });
    await sso.first().click();
    await page.waitForSelector('#username', { timeout: 30000 });      // the identity provider's form
    await page.fill('#username', SSO_USER);
    await page.click('#signin');
    await page.waitForURL((u) => String(u).startsWith(EDGE) && !/login|callback/.test(String(u)), { timeout: 60000 });
    const me = await page.request.post(`${EDGE}/api/v2/graphql`, { data: {
      query: '{ me { corpUser { urn username properties { displayName email } } } }' } });
    result.me = (await me.json()).data?.me?.corpUser ?? null;
    result.signedIn = Boolean(result.me);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/sso-signed-in.png` });
  } catch (e) {
    errors.push(`step failed: ${e.message.split('\n')[0]} (at ${page.url()})`);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/sso-failure.png` }).catch(() => {});
  }
  await browser.close();
  console.log(JSON.stringify(result));
})();
