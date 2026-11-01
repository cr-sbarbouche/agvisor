// Runs <agvisor-desktop> in a real Chrome against the real gateway.js and a fake guacd that paints a frame.
// Checks: rendering, mouse + keyboard + resize forwarding, the error event, reconnect without leaking, cleanup.
// Run: `npm run test:browser` (needs Google Chrome; Playwright drives the installed one, no browser download).
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { readFileSync } from 'node:fs';
import http from 'node:http';
import { chromium } from 'playwright';
import { encode, fakeGuacd, mint as mintToken, sleep, startGateway, until } from './test-support.mjs';

const key = randomBytes(32).toString('hex');
const mint = (ttl) => mintToken(key, ttl);

// Left half red, right half blue, then `sync` (the client only counts as connected after its first sync).
// Mask 14 = OVER, what guacd uses for fills; 12 (SRC/copy) would clear everything outside each rect.
const paint = (sock) =>
  sock.write(
    encode('ready', 'fake') + encode('size', 0, 800, 600) +
    encode('rect', 0, 0, 0, 400, 600) + encode('cfill', 14, 0, 255, 0, 0, 255) +
    encode('rect', 0, 400, 0, 400, 600) + encode('cfill', 14, 0, 0, 0, 255, 255) +
    encode('sync', Date.now()),
  );
const guacd = fakeGuacd(14823, { onConnect: paint });
const { got } = guacd; // instructions that reached guacd, i.e. what the browser sent through the gateway
const gw = startGateway({ AGVISOR_KEY: key, PORT: '18081', GUACD_PORT: '14823' });

const ORIGIN = 'http://127.0.0.1:18090';
const read = (p) => readFileSync(new URL(p, import.meta.url));
const files = {
  '/': ['text/html', `<!doctype html><body style="margin:0">
<script type="importmap">{"imports":{"guacamole-common-js":"/guacamole-common.js"}}</script>
<agvisor-desktop style="width:800px;height:600px"></agvisor-desktop>
<script type="module">
  import '/agvisor-desktop.js';
  const el = document.querySelector('agvisor-desktop');
  window.errors = [];
  el.addEventListener('error', (e) => window.errors.push(e.detail));
  const q = new URLSearchParams(location.search);
  el.setAttribute('gateway', q.get('gateway'));
  el.setAttribute('token', q.get('token'));
</script>`],
  '/favicon.ico': ['image/x-icon', ''], // Chrome asks for it; keeps the console clean
  '/agvisor-desktop.js': ['text/javascript', read('./agvisor-desktop.js')],
  '/guacamole-common.js': ['text/javascript', read('./node_modules/guacamole-common-js/dist/esm/guacamole-common.js')],
};

// A real server (only these three paths), not Playwright's request interception: Chrome treats an intercepted
// page as public and blocks it from reaching a loopback gateway (ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS).
const site = http.createServer((req, res) => {
  const f = files[new URL(req.url, ORIGIN).pathname];
  res.writeHead(f ? 200 : 404, f && { 'content-type': f[0] }).end(f?.[1]);
}).listen(18090, '127.0.0.1');

let browser, page;
const seen = []; // what the page logged, for the failure report
setTimeout(() => { console.error('browser test timed out'); gw.kill(); process.exit(1); }, 60000).unref();

try {
  await sleep(700); // let the gateway start listening
  browser = await chromium.launch({ channel: 'chrome' });
  page = await browser.newPage({ viewport: { width: 1000, height: 800 } });
  const pageErrors = [];
  page.on('pageerror', (e) => { pageErrors.push(e.message); seen.push(`pageerror: ${e.message}`); });
  page.on('console', (m) => seen.push(`console.${m.type()}: ${m.text()}`));
  page.on('requestfailed', (r) => seen.push(`request failed: ${r.url()} ${r.failure()?.errorText}`));
  const has = (pred) => got.some(pred);
  const set = (attr, value) => page.evaluate(([a, v]) => document.querySelector('agvisor-desktop').setAttribute(a, v), [attr, value]);
  // The desktop layer is the biggest canvas (guacamole rounds layer sizes up to multiples of 64: 800x600 -> 832x640).
  const pixel = (x, y) => page.evaluate(([x, y]) => {
    const [c] = [...document.querySelectorAll('agvisor-desktop canvas')].sort((a, b) => b.width * b.height - a.width * a.height);
    return c?.width ? [...c.getContext('2d').getImageData(x, y, 1, 1).data] : null;
  }, [x, y]);

  await page.goto(`${ORIGIN}/?gateway=${encodeURIComponent('ws://127.0.0.1:18081')}&token=${encodeURIComponent(mint(60))}`);

  // 1. The frame guacd painted shows up on a canvas, at the right place.
  await until(async () => (await pixel(100, 100))?.join() === '255,0,0,255', 'the red half of the frame to render');
  assert.deepEqual(await pixel(700, 100), [0, 0, 255, 255], 'right half should be blue');

  // 2. A click reaches guacd as a mouse press at the same coordinates.
  await page.locator('agvisor-desktop').click({ position: { x: 100, y: 100 } });
  const press = await until(() => got.find((i) => i[0] === 'mouse' && i[3] === '1'), 'a mouse press to reach guacd');
  assert.ok(Math.abs(press[1] - 100) <= 2 && Math.abs(press[2] - 100) <= 2, `press at ${press[1]},${press[2]}, expected ~100,100`);

  // 3. Typing reaches guacd as key down + key up (keysym 97 = 'a').
  await page.keyboard.press('a');
  await until(() => has((i) => i[0] === 'key' && i[1] === '97' && i[2] === '1'), 'key down to reach guacd');
  await until(() => has((i) => i[0] === 'key' && i[1] === '97' && i[2] === '0'), 'key up to reach guacd');

  // 4. Resizing the element sends the new size (the handshake size was 800x600).
  await page.evaluate(() => Object.assign(document.querySelector('agvisor-desktop').style, { width: '640px', height: '480px' }));
  await until(() => has((i) => i[0] === 'size' && i[1] === '640' && i[2] === '480'), 'the resize to reach guacd');

  // 5. A rejected token fires `error`, and the previous session is torn down (no leaked connection).
  await set('token', 'garbage');
  await until(() => page.evaluate(() => window.errors.length), 'an error event for the rejected token');
  await until(() => guacd.sockets.size === 0, 'the old session to close');

  // 6. A new valid token reconnects, and removing the element closes its connection.
  await set('token', mint(60));
  await until(() => guacd.sockets.size === 1, 'the reconnect');
  await page.evaluate(() => document.querySelector('agvisor-desktop').remove());
  await until(() => guacd.sockets.size === 0, 'the connection to close when the element is removed');

  assert.deepEqual(pageErrors, [], 'uncaught errors in the page');
  console.log('ok');
} catch (e) {
  console.error(e);
  console.error('--- page log:', seen.length ? `\n  ${seen.join('\n  ')}` : '(nothing)');
  console.error('--- guacd saw:', got.map((i) => i[0]).join(' ') || '(nothing)');
  const dom = await page?.evaluate(() => ({
    elementDefined: !!customElements.get('agvisor-desktop'),
    attrs: Object.fromEntries([...document.querySelector('agvisor-desktop')?.attributes ?? []].map((a) => [a.name, a.value.slice(0, 20)])),
    canvases: [...document.querySelectorAll('canvas')].map((c) => {
      const px = (x, y) => c.width ? [...c.getContext('2d').getImageData(x, y, 1, 1).data].join(',') : '-';
      return `${c.width}x${c.height} @(100,100)=${px(100, 100)} @(700,100)=${px(700, 100)}`;
    }),
    errors: window.errors,
  })).catch((err) => `(page not inspectable: ${err.message})`);
  console.error('--- dom:', JSON.stringify(dom));
  process.exitCode = 1; // without this, process.exit() below would report success
} finally {
  await browser?.close();
  gw.kill();
  guacd.close();
  site.close();
  process.exit();
}
