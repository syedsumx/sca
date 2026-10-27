// Headless check used by test_20: the partner page (served by the e2e sink) embeds a Cairn card as a
// web component and as an iframe. Loaded from http://localhost:8099 (an allowed embed origin) and
// from http://127.0.0.1:8099 (not allowed). Prints one JSON line: {allowed, blocked, errors}.
const { chromium } = require('playwright');

async function visit(browser, origin, EDGE, URN, TOKEN, SHOTS, name) {
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  const console = [];
  page.on('console', (m) => console.push(m.text()));
  const url = `${origin}/embed.html?edge=${encodeURIComponent(EDGE)}&urn=${encodeURIComponent(URN)}&token=${encodeURIComponent(TOKEN)}`;
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForTimeout(6000);   // let the component fetch and the iframe render (or be refused)
  const component = await page.evaluate(() => {
    const el = document.getElementById('component');
    return ((el && el.shadowRoot ? el.shadowRoot.textContent : el && el.textContent) || '').trim();
  });
  const frame = page.frames().find((f) => f.url().includes('/embed/v1/frame/'));
  let frameText = '';
  if (frame) {
    frameText = await frame.evaluate(() => {
      const el = document.querySelector('cairn-asset-card');
      return ((el && el.shadowRoot ? el.shadowRoot.textContent : document.body.textContent) || '').trim();
    }).catch(() => '');
  }
  const frameBlocked = console.some((c) => /frame-ancestors|Refused to frame/i.test(c)) || !frameText;
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/embed-${name}.png`, fullPage: true });
  await page.close();
  return { component: component.slice(0, 400), frame: frameText.slice(0, 400), frame_blocked: frameBlocked, console: console.slice(0, 10) };
}

(async () => {
  const { EDGE, URN, TOKEN, SHOTS } = process.env;
  const browser = await chromium.launch();
  const errors = [];
  const result = { allowed: null, blocked: null, errors };
  try {
    result.allowed = await visit(browser, 'http://localhost:8099', EDGE, URN, TOKEN, SHOTS, 'allowed');
    result.blocked = await visit(browser, 'http://127.0.0.1:8099', EDGE, URN, TOKEN, SHOTS, 'blocked');
  } catch (e) {
    errors.push(`step failed: ${e.message.split('\n')[0]}`);
  }
  await browser.close();
  console.log(JSON.stringify(result));
})();
