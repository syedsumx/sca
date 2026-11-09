// Headless check used by test_ui_guided_live: the workspace against the real Cairn Core.
//  - the command palette finds a dataset in Core's catalog search and opens its asset page;
//  - the asset page's shared header comes from Core (name, platform, domain) with its help and docs anchor;
//  - a data product's page shows its header from Core;
//  - a form's dataset picker finds the dataset through Core's search, and the form sends its urn;
//  - a glossary term from Core's glossary is added to the dataset with bulk edit.
// Prints one JSON line: {palette, asset, product, picker, bulk, errors}.
const { chromium } = require('playwright');

(async () => {
  const { EDGE, UI_USER, UI_PASSWORD, SHOTS, DS_NAME, DS_URN, PRODUCT_URN, TERM_ID } = process.env;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const result = { palette: null, asset: null, product: null, picker: null, bulk: false, errors };
  const header = () => page.locator('[data-page-header]').first();
  const shot = async (name) => { if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}.png` }).catch(() => {}); };
  try {
    await page.goto(`${EDGE}/login`, { waitUntil: 'domcontentloaded' });
    await page.fill('#username', UI_USER);
    await page.fill('#password', UI_PASSWORD);
    await page.getByRole('button', { name: /^log ?in$/i }).click();
    await page.waitForURL((u) => !String(u).includes('/login'), { timeout: 60000 });

    // The palette: the dataset from Core's search, opened from the keyboard.
    await page.goto(`${EDGE}/mfe/cairn#/discover`, { waitUntil: 'domcontentloaded' });
    await header().waitFor({ timeout: 90000 });
    await page.keyboard.press('Control+k');
    const box = page.getByRole('combobox', { name: 'Type a page, an action or a dataset…' });
    await box.waitFor({ timeout: 30000 });
    await box.fill(DS_NAME);
    // The catalog's result (its detail says "dataset · <platform>"), not "Ask: …" or a page.
    const hit = page.getByRole('option').filter({ hasText: DS_NAME }).filter({ hasText: /dataset/i }).first();
    await hit.waitFor({ timeout: 30000 });
    result.palette = (await hit.innerText()).replace(/\s+/g, ' ').trim();
    await hit.click();
    await page.waitForFunction((urn) => decodeURIComponent(location.hash).includes(urn), DS_URN, { timeout: 30000 });

    // The asset page's header, from Core.
    await header().locator('h1', { hasText: DS_NAME }).waitFor({ timeout: 60000 });
    await page.waitForTimeout(1000);
    result.asset = {
      title: (await header().locator('h1').innerText()).trim(),
      description: (await header().locator('p').first().innerText()).trim(),
      help: await header().getAttribute('data-help-anchor'),
      helpButton: await header().getByRole('button', { name: /help/i }).count(),
    };
    await shot('guided-asset-header');

    // A data product's page, from Core.
    await page.goto(`${EDGE}/mfe/cairn-marketplace#/marketplace/${encodeURIComponent(PRODUCT_URN)}`, { waitUntil: 'domcontentloaded' });
    await header().waitFor({ timeout: 90000 });
    await header().locator('h1').filter({ hasText: /\S/ }).waitFor({ timeout: 60000 });
    result.product = {
      title: (await header().locator('h1').innerText()).trim(),
      description: (await header().locator('p').first().innerText()).trim(),
    };
    await shot('guided-product-header');

    // A form's dataset picker searching Core's catalog: Lakehouse → Quality monitors → New monitor.
    await page.goto(`${EDGE}/mfe/cairn#/lakehouse?tab=quality-monitors`, { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: 'New monitor' }).first().click({ timeout: 90000 });
    const dialog = page.getByRole('dialog').last();
    const picker = dialog.getByLabel(/^Dataset URN/);
    await picker.click();
    await picker.fill(DS_NAME);
    const option = page.locator('.ant-select-dropdown:visible .ant-select-item-option', { hasText: DS_NAME }).first();
    await option.waitFor({ timeout: 30000 });
    const label = (await option.innerText()).trim();
    await option.click();
    await dialog.getByLabel('Raw JSON').click();
    const raw = await dialog.getByLabel('Request body (JSON)').inputValue();
    result.picker = { option: label, body: JSON.parse(raw) };
    await shot('guided-picker');
    await dialog.getByRole('button', { name: 'Cancel' }).click();

    // A glossary term (Core's) on the dataset, with bulk edit.
    await page.goto(`${EDGE}/mfe/cairn#/search?q=${encodeURIComponent(DS_NAME)}`, { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: 'Bulk edit…' }).click({ timeout: 90000 });
    const bulk = page.getByRole('dialog').last();
    await bulk.getByLabel('CSV text').fill(`urn,glossary_terms\n"${DS_URN}",${TERM_ID}\n`);
    await bulk.getByRole('button', { name: 'Preview changes' }).click();
    await bulk.getByRole('button', { name: /(^|loading )Apply 1 change/ }).click({ timeout: 60000 });
    await page.getByRole('status').filter({ hasText: '1 change(s) applied' }).waitFor({ timeout: 60000 });
    result.bulk = true;
    await shot('guided-bulk-term');
  } catch (e) {
    errors.push(`step failed: ${e.message.split('\n')[0]} (at ${page.url()})`);
    await shot('guided-failure');
  }
  await browser.close();
  result.errors = errors.filter((e) => !/ResizeObserver/.test(e));
  console.log(JSON.stringify(result));
})();
