// Headless check used by test_21b: Requests → Forms lists the admin's task; the drawer submits an
// answer; Governance studio → Compliance forms shows the form at 100%. Prints {submitted, progress, errors}.
const { chromium } = require('playwright');

(async () => {
  const { EDGE, UI_USER, UI_PASSWORD, SHOTS, FORM_NAME, ANSWER } = process.env;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const result = { submitted: false, progress: false, errors };
  try {
    await page.goto(`${EDGE}/login`, { waitUntil: 'domcontentloaded' });
    await page.fill('#username', UI_USER);
    await page.fill('#password', UI_PASSWORD);
    await page.getByRole('button', { name: /^log ?in$/i }).click();
    await page.waitForURL((u) => !String(u).includes('/login'), { timeout: 60000 });
    await page.goto(`${EDGE}/mfe/cairn-requests#/requests`, { waitUntil: 'domcontentloaded' });
    await page.getByRole('tab', { name: /^Forms/ }).click({ timeout: 90000 });
    const row = page.locator('tr', { hasText: FORM_NAME });
    await row.getByRole('button', { name: 'Complete' }).click({ timeout: 60000 });
    const box = page.getByLabel('What is in it?');
    await box.fill(ANSWER);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/forms-task.png` });
    await page.getByRole('button', { name: 'Submit', exact: true }).click();
    await page.getByText('Answers saved to the catalog').waitFor({ timeout: 30000 });
    result.submitted = true;
    await page.goto(`${EDGE}/mfe/cairn-governance#/governance`, { waitUntil: 'domcontentloaded' });
    await page.getByRole('tab', { name: 'Compliance forms' }).click({ timeout: 90000 });
    const formRow = page.locator('tr', { hasText: FORM_NAME });
    await formRow.waitFor({ timeout: 60000 });
    result.progress = /100%/.test(await formRow.innerText());
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/forms-admin.png` });
  } catch (e) {
    errors.push(`step failed: ${e.message.split('\n')[0]} (at ${page.url()})`);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/forms-failure.png` }).catch(() => {});
  }
  await browser.close();
  result.errors = errors.filter((e) => !/ResizeObserver/.test(e));
  console.log(JSON.stringify(result));
})();
