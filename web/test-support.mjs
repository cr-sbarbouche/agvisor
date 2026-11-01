// Shared by e2e.mjs and browser.mjs: a fake guacd, a token minter and a gateway launcher.
import { execFileSync, spawn } from 'node:child_process';
import net from 'node:net';
import { fileURLToPath } from 'node:url';

// Guacamole wire format: `<len>.<value>,<len>.<value>;`. Instructions can be split across TCP chunks.
export const encode = (...els) => els.map((e) => `${String(e).length}.${e}`).join(',') + ';';

const parser = () => {
  let buf = '';
  let cur = [];
  return (chunk) => {
    buf += chunk;
    const out = [];
    for (;;) {
      const dot = buf.indexOf('.');
      if (dot < 0) break;
      const n = Number(buf.slice(0, dot));
      if (buf.length < dot + n + 2) break; // element or its terminator has not fully arrived
      cur.push(buf.slice(dot + 1, dot + 1 + n));
      const end = buf[dot + 1 + n];
      buf = buf.slice(dot + n + 2);
      if (end === ';') {
        out.push(cur);
        cur = [];
      }
    }
    return out;
  };
};

// Answers the handshake, records every instruction it receives in `got`, and calls onConnect(socket) so a
// test can draw. Not a real RDP bridge: just enough protocol for the gateway and the browser client.
export function fakeGuacd(port, { onConnect } = {}) {
  const got = [];
  const sockets = new Set();
  const server = net.createServer((sock) => {
    sockets.add(sock);
    sock.on('close', () => sockets.delete(sock));
    sock.on('error', () => {});
    const feed = parser();
    sock.on('data', (d) =>
      feed(d.toString()).forEach((ins) => {
        got.push(ins);
        if (ins[0] === 'select') sock.write(encode('args', 'hostname', 'port', 'username', 'password', 'security', 'ignore-cert'));
        if (ins[0] === 'connect') onConnect?.(sock, ins);
      }),
    );
  }).listen(port, '127.0.0.1');
  return { got, sockets, close: () => { sockets.forEach((s) => s.destroy()); server.close(); } };
}

// Mint a token with the real Python code (needs `pip install agvisor[web]` in $PYTHON, default python3).
export const mint = (key, ttl) =>
  execFileSync(process.env.PYTHON ?? 'python3', ['-c',
    `from agvisor import Sandbox; s = Sandbox(gui=True); s.rdp_port = 49153; s.password = 'pw'
print(s.desktop_token(bytes.fromhex('${key}'), ttl=${ttl}), end='')`],
  { env: { ...process.env, PYTHONPATH: new URL('../src', import.meta.url).pathname } }).toString();

export const startGateway = (env) =>
  spawn('node', [fileURLToPath(new URL('../gateway/gateway.js', import.meta.url))], {
    stdio: 'inherit',
    env: { ...process.env, ...env },
  });

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Poll until fn() is truthy (or time out): tests wait for effects, not for fixed delays.
export async function until(fn, what, ms = 8000) {
  for (const end = Date.now() + ms; Date.now() < end; await sleep(50)) {
    const v = await fn();
    if (v) return v;
  }
  throw new Error(`timed out waiting for ${what}`);
}
