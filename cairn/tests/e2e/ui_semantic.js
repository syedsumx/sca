// Headless check used by test_18: the workspace search shows "Related by meaning" and an asset page
// shows "Similar assets". Prints one JSON line: {related, similar, errors}.
const { chromium } = require('playwright');

(async () => {
  const { EDGE, UI_USER, UI_PASSWORD, SHOTS, QUERY, ASSET_URN } = process.env;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const result = { related: [], similar: [], errors };
  try {
    await page.goto(`${EDGE}/login`, { waitUntil: 'domcontentloaded' });
    await page.fill('#username', UI_USER);
    await page.fill('#password', UI_PASSWORD);
    await page.getByRole('button', { name: /^log ?in$/i }).click();
    await page.waitForURL((u) => !String(u).includes('/login'), { timeout: 60000 });
    await page.goto(`${EDGE}/mfe/cairn#/search?q=${encodeURIComponent(QUERY)}`, { waitUntil: 'domcontentloaded' });
    const related = page.getByRole('region', { name: 'Related by meaning' });
    await related.waitFor({ timeout: 90000 });
    result.related = await related.locator('a[data-urn]').evaluateAll((els) => els.map((e) => e.getAttribute('data-urn')));
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/search-related-by-meaning.png`, fullPage: true });
    await page.goto(`${EDGE}/mfe/cairn#/asset/${encodeURIComponent(ASSET_URN)}`, { waitUntil: 'domcontentloaded' });
    const similar = page.getByLabel('Similar assets');
    await similar.waitFor({ timeout: 90000 });
    result.similar = await similar.locator('a[data-urn]').evaluateAll((els) => els.map((e) => e.getAttribute('data-urn')));
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/asset-similar.png`, fullPage: true });
  } catch (e) {
    errors.push(`step failed: ${e.message.split('\n')[0]} (at ${page.url()})`);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/semantic-failure.png` }).catch(() => {});
  }
  await browser.close();
  result.errors = errors.filter((e) => !/ResizeObserver/.test(e));
  console.log(JSON.stringify(result));
})();
