// Headless check used by test_13b: the workspace sections are entries in the catalog's own left
// navigation, and moving between sections inside the workspace moves the catalog's navigation too.
// Prints one JSON line: {entries, viaShell, viaWorkspace, errors}.
const { chromium } = require('playwright');

(async () => {
  const { EDGE, UI_USER, UI_PASSWORD, SHOTS } = process.env;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const result = { entries: [], viaShell: null, viaWorkspace: null, errors };
  const want = ['Discover', 'Data marketplace', 'Requests', 'Governance studio', 'Context inbox', 'Assistant',
    'Workspace settings'];
  try {
    await page.goto(`${EDGE}/login`, { waitUntil: 'domcontentloaded' });
    await page.fill('#username', UI_USER);
    await page.fill('#password', UI_PASSWORD);
    await page.getByRole('button', { name: /^log ?in$/i }).click();
    await page.waitForURL((u) => !String(u).includes('/login'), { timeout: 60000 });
    await page.goto(`${EDGE}/`, { waitUntil: 'networkidle' });
    // The shell's navigation: a "Workspace" group with one entry per section.
    await page.getByText('Workspace', { exact: true }).first().waitFor({ timeout: 60000 });
    for (const label of want) {
      if (await page.getByText(label, { exact: true }).first().isVisible().catch(() => false)) result.entries.push(label);
    }
    await page.waitForTimeout(2000);   // icons load lazily
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/nav-shell.png` });

    // Shell → workspace: the Requests entry opens the Requests section.
    await page.getByText('Requests', { exact: true }).first().click();
    await page.waitForURL((u) => String(u).includes('/mfe/cairn-requests#/requests'), { timeout: 60000 });
    await page.waitForSelector('nav[aria-label="Workspace sections"] a.active', { timeout: 60000 });
    result.viaShell = { url: page.url().replace(EDGE, ''),
      active: (await page.getAttribute('nav[aria-label="Workspace sections"] a.active', 'href')).replace(/^[^#]*/, '') };

    // Workspace → shell: the Marketplace tab moves the shell to its entry.
    await page.click('nav[aria-label="Workspace sections"] >> text=Marketplace');
    await page.waitForURL((u) => String(u).includes('/mfe/cairn-marketplace#/marketplace'), { timeout: 60000 });
    await page.waitForSelector('nav[aria-label="Workspace sections"] a.active', { timeout: 60000 });
    result.viaWorkspace = { url: page.url().replace(EDGE, ''),
      active: (await page.getAttribute('nav[aria-label="Workspace sections"] a.active', 'href')).replace(/^[^#]*/, '') };
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/nav-marketplace.png` });
  } catch (e) {
    errors.push(`step failed: ${e.message.split('\n')[0]} (at ${page.url()})`);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/nav-failure.png` }).catch(() => {});
  }
  await browser.close();
  result.missing = want.filter((w) => !result.entries.includes(w));
  result.errors = errors.filter((e) => !/ResizeObserver/.test(e));
  console.log(JSON.stringify(result));
})();
