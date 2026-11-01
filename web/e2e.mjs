// Checks the token contract end to end, no docker/RDP needed:
// Python mints a token -> real gateway.js decrypts it -> a fake guacd receives the RDP settings.
// Run: `npm test` (needs `pip install agvisor[web]` for $PYTHON, default python3).
import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import net from 'node:net';
import { randomBytes } from 'node:crypto';

const key = randomBytes(32).toString('hex');
const mint = (ttl) => execFileSync(process.env.PYTHON ?? 'python3', ['-c',
  `from agvisor import Sandbox; s = Sandbox(gui=True); s.rdp_port = 49153; s.password = 'pw'
print(s.desktop_token(bytes.fromhex('${key}'), ttl=${ttl}), end='')`],
  { env: { ...process.env, PYTHONPATH: new URL('../src', import.meta.url).pathname } }).toString();

// Guacamole wire format: `<len>.<value>,<len>.<value>;`, may arrive several instructions per chunk.
const parse = (buf) => {
  const ins = []; let cur = [], i = 0;
  while (i < buf.length) {
    const dot = buf.indexOf('.', i), n = Number(buf.slice(i, dot));
    cur.push(buf.slice(dot + 1, dot + 1 + n));
    const end = buf[dot + 1 + n]; i = dot + n + 2;
    if (end === ';') { ins.push(cur); cur = []; }
  }
  return ins;
};

const got = [];  // instructions the fake guacd received
const guacd = net.createServer((sock) => {
  sock.on('data', (d) => parse(d.toString()).forEach((ins) => {
    got.push(ins);
    if (ins[0] === 'select') sock.write('4.args,8.hostname,4.port,8.username,8.password,8.security,11.ignore-cert;');
  }));
}).listen(14822, '127.0.0.1');

const gw = spawn('node', ['gateway.js'], { stdio: 'inherit',
  env: { ...process.env, AGVISOR_KEY: key, PORT: '18080', GUACD_PORT: '14822' } });
setTimeout(() => { console.error('e2e timed out'); gw.kill(); process.exit(1); }, 20000).unref();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const open = (token) => new WebSocket(`ws://127.0.0.1:18080/?token=${encodeURIComponent(token)}&width=800&height=600&dpi=96`);

try {
  await sleep(700); // let the gateway start listening

  const ws = open(mint(60)); // valid token: gateway must reach guacd with our RDP settings
  for (let i = 0; i < 40 && !got.some((x) => x[0] === 'connect'); i++) await sleep(100);
  const connect = got.find((x) => x[0] === 'connect');
  assert.ok(connect, 'valid token never reached guacd');
  assert.deepEqual(connect.slice(1, 7), ['127.0.0.1', '49153', 'gnome', 'pw', 'any', 'true']);
  assert.deepEqual(got.find((x) => x[0] === 'size').slice(1), ['800', '600', '96']);
  ws.close();

  got.length = 0;
  open(mint(-10)); // expired token: gateway must not even open a guacd connection
  await sleep(800);
  assert.equal(got.length, 0, 'expired token reached guacd');
  console.log('ok');
} catch (e) {
  console.error(e);
  process.exitCode = 1; // without this, process.exit() below would report success
} finally {
  gw.kill();
  guacd.close();
  process.exit();
}
