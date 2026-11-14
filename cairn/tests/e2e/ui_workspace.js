// Headless check used by test_13: Cairn Core login → /mfe/cairn (consumer workspace) → asset page.
// Prints one JSON line: {discover, asset, errors}. Uses the pre-installed Playwright + Chromium.
const { chromium } = require('playwright');

(async () => {
  const { EDGE, ASSET_URN, SHOTS, UI_USER, UI_PASSWORD } = process.env;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const result = { discover: false, marketplace: false, asset: false, errors };
  try {
    await page.goto(`${EDGE}/login`, { waitUntil: 'domcontentloaded' });
    await page.fill('#username', UI_USER);
    await page.fill('#password', UI_PASSWORD);
    await page.getByRole('button', { name: /^log ?in$/i }).click();
    await page.waitForURL((u) => !String(u).includes('/login'), { timeout: 60000 });
    await page.goto(`${EDGE}/mfe/cairn#/discover`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('text=Trending', { timeout: 90000 });
    // Real data, not skeletons: the e2e dataset was viewed by two users, so it is trending.
    await page.waitForSelector('text=customers', { timeout: 60000 });
    result.discover = true;
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/workspace-discover.png` });
    // Marketplace lists real data products (the e2e run creates "Customer 360 <run>").
    await page.goto(`${EDGE}/mfe/cairn#/marketplace`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('text=Customer 360', { timeout: 60000 });
    result.marketplace = true;
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/workspace-marketplace.png` });
    await page.goto(`${EDGE}/mfe/cairn#/asset/${encodeURIComponent(ASSET_URN)}`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('text=customers', { timeout: 60000 });
    await page.waitForTimeout(1500);
    result.asset = true;
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/workspace-asset.png` });
  } catch (e) {
    errors.push(`step failed: ${e.message.split('\n')[0]}`);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/workspace-failure.png` }).catch(() => {});
  }
  await browser.close();
  // Benign browser noise from the host shell, not the workspace.
  result.errors = errors.filter((e) => !/ResizeObserver/.test(e));
  console.log(JSON.stringify(result));
})();
