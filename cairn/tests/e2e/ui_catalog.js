// Headless check used by test_ui_catalog_live: the Catalog section (L147) against the real Cairn Core.
// Prints one JSON line: {catalog, products, contracts, glossary, domain, lineage, errors}.
const { chromium } = require('playwright');

(async () => {
  const env = process.env;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const result = { catalog: {}, products: {}, contracts: {}, glossary: {}, domain: {}, lineage: {}, errors };
  const shot = async (name) => { if (env.SHOTS) await page.screenshot({ path: `${env.SHOTS}/${name}.png` }).catch(() => {}); };
  const seen = (locator, timeout = 60000) => locator.first().waitFor({ timeout }).then(() => true, () => false);
  const step = async (name, fn) => {
    try {
      await fn();
    } catch (e) {
      errors.push(`${name} failed: ${e.message.split('\n')[0]} (at ${page.url()})`);
      await shot(`catalog-${name}-failure`);
    }
  };
  try {
    await page.goto(`${env.EDGE}/login`, { waitUntil: 'domcontentloaded' });
    await page.fill('#username', env.UI_USER);
    await page.fill('#password', env.UI_PASSWORD);
    await page.getByRole('button', { name: /^log ?in$/i }).click();
    await page.waitForURL((u) => !String(u).includes('/login'), { timeout: 60000 });

    await step('catalog', async () => {
      // The shell's own Catalog entry opens the section.
      await page.goto(`${env.EDGE}/mfe/cairn-catalog`, { waitUntil: 'domcontentloaded' });
      const notNow = page.getByRole('dialog').getByRole('button', { name: 'Not now' });
      if (await notNow.waitFor({ state: 'visible', timeout: 15000 }).then(() => true, () => false)) {
        await notNow.click();
        await notNow.waitFor({ state: 'hidden', timeout: 15000 });
      }
      const types = page.getByRole('group', { name: 'Entity type' });
      const datasets = types.getByRole('button', { name: /^Dataset\s*[\d,]+$/ });
      await datasets.waitFor({ timeout: 90000 });
      result.catalog.datasets = Number((await datasets.innerText()).replace(/[^\d]/g, ''));
      await datasets.click();
      await page.getByRole('searchbox', { name: 'Search the catalog' }).or(page.getByLabel('Search the catalog')).first().fill(env.DS_NAME);
      await page.keyboard.press('Enter');
      const hit = page.getByRole('link', { name: new RegExp(`^${env.DS_NAME}`) }).first();
      await hit.waitFor({ timeout: 60000 });
      await hit.click();
      const h1 = page.locator('[data-page-header] h1').first();
      await h1.filter({ hasText: env.DS_NAME }).waitFor({ timeout: 60000 });
      result.catalog.opened = (await h1.innerText()).trim();
      await shot('catalog-browse');
    });

    await step('products', async () => {
      await page.goto(`${env.EDGE}/mfe/cairn-products`, { waitUntil: 'domcontentloaded' });
      const row = page.getByRole('row', { name: new RegExp(env.PRODUCT_NAME) });
      result.products.row = await seen(row, 90000);
      result.products.domain = (await row.getByRole('link').nth(1).innerText()).trim();
      await row.getByRole('link', { name: env.PRODUCT_NAME }).click();
      await page.locator('[data-page-header] h1', { hasText: env.PRODUCT_NAME }).waitFor({ timeout: 60000 });
      result.products.port = await seen(page.getByRole('tabpanel').getByRole('row', { name: new RegExp(env.DS_NAME) })
        .getByText('Output port'));
      await shot('catalog-product');
    });

    await step('contracts', async () => {
      await page.goto(`${env.EDGE}/mfe/cairn-contracts`, { waitUntil: 'domcontentloaded' });
      const row = page.getByRole('row', { name: new RegExp(env.DS_NAME) });
      result.contracts.row = await seen(row, 90000);
      result.contracts.status = (await row.locator('.cairn-status .label').first().innerText()).trim();
      await shot('catalog-contracts');
    });

    await step('glossary', async () => {
      await page.goto(`${env.EDGE}/mfe/cairn#/glossary`, { waitUntil: 'domcontentloaded' });
      const group = page.getByRole('link', { name: new RegExp(env.GROUP_NAME) });
      result.glossary.group = await seen(group, 90000);
      await group.first().click();
      await page.getByRole('link', { name: env.TERM_NAME }).click({ timeout: 60000 });
      await page.locator('[data-page-header] h1', { hasText: env.TERM_NAME }).waitFor({ timeout: 60000 });
      result.glossary.definition = await seen(page.getByText('A booking made by a customer.'));
      result.glossary.steward = await seen(page.getByRole('region', { name: 'Definition' }).getByText('admin'));
      result.glossary.asset = await seen(page.getByRole('region', { name: 'Assets using it' })
        .getByRole('link', { name: new RegExp(`^${env.DS_NAME}`) }));
      await shot('catalog-term');
    });

    await step('domains', async () => {
      await page.goto(`${env.EDGE}/mfe/cairn#/domains`, { waitUntil: 'domcontentloaded' });
      await page.getByRole('link', { name: new RegExp(env.DOMAIN_NAME) }).first().click({ timeout: 90000 });
      await page.locator('[data-page-header] h1', { hasText: env.DOMAIN_NAME }).waitFor({ timeout: 60000 });
      result.domain.product = await seen(page.getByRole('link', { name: env.PRODUCT_NAME }));
      result.domain.asset = await seen(page.getByRole('region', { name: 'Assets' }).getByRole('link', { name: new RegExp(`^${env.DS_NAME}`) }));
      await shot('catalog-domain');
    });

    await step('lineage', async () => {
      await page.goto(`${env.EDGE}/mfe/cairn#/lineage?urn=${encodeURIComponent(env.DS_URN)}`, { waitUntil: 'domcontentloaded' });
      result.lineage.graph = await seen(page.getByTestId('lineage-graph'), 90000);
      const columns = page.getByRole('region', { name: 'Column-level lineage' });
      result.lineage.column = await seen(columns.getByRole('link', { name: env.RAW_NAME }));
      await shot('catalog-lineage');
    });
  } catch (e) {
    errors.push(`sign-in failed: ${e.message.split('\n')[0]} (at ${page.url()})`);
  }
  await browser.close();
  result.errors = errors.filter((e) => !/ResizeObserver/.test(e));
  console.log(JSON.stringify(result));
})();
