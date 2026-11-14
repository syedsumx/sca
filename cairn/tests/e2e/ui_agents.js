// Headless check used by test_remote_agents_in_workspace_settings: Ingestion → Collectors (formerly Settings → Remote
// agents) shows the pools and runs, and a run's log opens. Prints one JSON line: {tab, pool, run, log, errors}.
const { chromium } = require('playwright');

(async () => {
  const { EDGE, UI_USER, UI_PASSWORD, SHOTS, TASK_ID } = process.env;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const result = { tab: false, pool: false, run: false, log: '', errors };
  try {
    await page.goto(`${EDGE}/login`, { waitUntil: 'domcontentloaded' });
    await page.fill('#username', UI_USER);
    await page.fill('#password', UI_PASSWORD);
    await page.getByRole('button', { name: /^log ?in$/i }).click();
    await page.waitForURL((u) => !String(u).includes('/login'), { timeout: 60000 });
    // L149: remote agents are collectors, and their home is Ingestion › Collectors. The shell's Workspace settings
    // entry still opens the workspace; its earlier Settings tab (?tab=agents) redirects to the new home.
    await page.goto(`${EDGE}/mfe/cairn-settings#/settings?tab=agents`, { waitUntil: 'domcontentloaded' });
    const tab = page.getByRole('tab', { name: 'Collectors', exact: true });
    await tab.waitFor({ timeout: 90000 });
    result.tab = (await tab.getAttribute('aria-selected')) === 'true';
    await page.getByText('finance', { exact: true }).first().waitFor({ timeout: 60000 });
    result.pool = true;
    await page.getByRole('tab', { name: 'Runs' }).click();
    const row = page.locator('tr', { hasText: TASK_ID });
    await row.waitFor({ timeout: 60000 });
    result.run = true;
    await row.getByRole('button', { name: 'Logs' }).click();
    const log = page.getByLabel('Run log');
    await page.waitForFunction(() => /finished: SUCCESS/.test(document.querySelector('pre[aria-label="Run log"]')?.textContent || ''),
      null, { timeout: 60000 });
    result.log = await log.textContent();
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/settings-remote-agents-logs.png` });
  } catch (e) {
    errors.push(`step failed: ${e.message.split('\n')[0]} (at ${page.url()})`);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/settings-remote-agents-failure.png` }).catch(() => {});
  }
  await browser.close();
  result.errors = errors.filter((e) => !/ResizeObserver/.test(e));
  console.log(JSON.stringify(result));
})();
