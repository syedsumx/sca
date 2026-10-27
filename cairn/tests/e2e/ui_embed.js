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
  // Visible text only (the shadow root starts with its <style>).
  const text = (root) => [...(root ? root.childNodes : [])].filter((n) => n.nodeName !== 'STYLE')
    .map((n) => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
  const component = await page.evaluate((textSrc) => {
    const visible = new Function(`return ${textSrc}`)();
    const el = document.getElementById('component');
    return visible(el && (el.shadowRoot || el));
  }, text.toString());
  const frame = page.frames().find((f) => f.url().includes('/embed/v1/frame/'));
  let frameText = '';
  if (frame) {
    frameText = await frame.evaluate((textSrc) => {
      const visible = new Function(`return ${textSrc}`)();
      const el = document.querySelector('cairn-asset-card');
      return el ? visible(el.shadowRoot || el) : (document.body ? document.body.textContent.trim() : '');
    }, text.toString()).catch(() => '');
  }
  // Refused either by the browser (frame-ancestors) or by Cairn (origin check on the frame request).
  const frameBlocked = console.some((c) => /frame-ancestors|Refused to frame/i.test(c)) || !frameText
    || /not allowed to embed/.test(frameText);
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
