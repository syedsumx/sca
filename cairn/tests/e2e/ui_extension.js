// Headless check used by test_27: the browser extension, built with this stack's address, signed in
// with the user's session, shows the catalog card for a Looker dashboard URL and says so when a
// dashboard is not catalogued. Prints {card, unknown, errors}.
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');

(async () => {
  const { EDGE, UI_USER, UI_PASSWORD, SHOTS, DASHBOARD_URL, DASHBOARD_NAME } = process.env;
  const result = { card: null, unknown: null, errors: [] };
  const dist = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-ext-dist-'));
  execFileSync('node', [path.join(__dirname, '..', '..', 'extension', 'build.mjs'), '--cairn-url', EDGE, '--out', dist]);
  const ctx = await chromium.launchPersistentContext(fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-ext-profile-')), {
    channel: 'chromium', headless: true, ignoreHTTPSErrors: true,
    args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`, '--ignore-certificate-errors'],
  });
  try {
    const page = await ctx.newPage();
    page.on('pageerror', (e) => result.errors.push(String(e)));
    await page.goto(`${EDGE}/login`, { waitUntil: 'domcontentloaded' });
    await page.fill('#username', UI_USER);
    await page.fill('#password', UI_PASSWORD);
    await page.getByRole('button', { name: /^log ?in$/i }).click();
    await page.waitForURL((u) => !String(u).includes('/login'), { timeout: 60000 });
    let [worker] = ctx.serviceWorkers();
    if (!worker) worker = await ctx.waitForEvent('serviceworker', { timeout: 30000 });
    const id = worker.url().split('/')[2];
    const panel = await ctx.newPage();
    panel.on('pageerror', (e) => result.errors.push(String(e)));
    await panel.goto(`chrome-extension://${id}/sidepanel.html?url=${encodeURIComponent(DASHBOARD_URL)}`);
    await panel.getByRole('heading', { name: DASHBOARD_NAME }).waitFor({ timeout: 60000 });
    result.card = { text: await panel.locator('main').innerText(), open: await panel.getAttribute('#open', 'href') };
    if (SHOTS) await panel.screenshot({ path: `${SHOTS}/extension-card.png` });
    await panel.goto(`chrome-extension://${id}/sidepanel.html?url=${encodeURIComponent(DASHBOARD_URL.replace(/\d+$/, '987654321'))}`);
    await panel.getByText('not in the catalog yet').waitFor({ timeout: 30000 });
    result.unknown = true;
  } catch (e) {
    result.errors.push(`step failed: ${e.message.split('\n')[0]}`);
  }
  await ctx.close();
  console.log(JSON.stringify(result));
})();
