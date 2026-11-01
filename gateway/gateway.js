// WebSocket <-> guacd bridge for <agvisor-desktop>. Run it with `docker compose up` (see ../README.md),
// or by hand: `npm install && npm start`. Its own package so the browser component stays free of server code.
// Env: AGVISOR_KEY (64 hex chars, same key as Sandbox.desktop_token), HOST (127.0.0.1), PORT (8080), GUACD_PORT (4822)
import { createDecipheriv, createHmac, timingSafeEqual } from 'node:crypto';
import GuacamoleLite from 'guacamole-lite';

const secret = Buffer.from(process.env.AGVISOR_KEY ?? '', 'hex');
if (secret.length !== 32) throw new Error('AGVISOR_KEY must be 64 hex chars (openssl rand -hex 32)');

// One secret, two purposes: derive separate AES and HMAC keys (desktop.py derives them the same way).
const derive = (label) => createHmac('sha256', secret).update(label).digest();
const encKey = derive('agvisor-enc');
const macKey = derive('agvisor-mac');

// Runs at the WebSocket upgrade, before guacamole-lite sees the connection. Tokens are AES-CBC, which does not
// detect tampering, so: verify the HMAC first, only then decrypt and check expiry. Done here and not in
// guacamole-lite's processConnectionSettings callback, which closes the browser socket but still opens guacd
// and sends the RDP credentials.
const valid = (url) => {
  try {
    const raw = Buffer.from(new URL(url, 'http://x').searchParams.get('token'), 'base64'); // null -> throws -> false
    // We decode the outer JSON as UTF-8 but guacamole-lite decodes it as ASCII (dropping each byte's high bit),
    // so non-ASCII bytes could make the two of us see different tokens. Reject them.
    if (raw.some((b) => b > 0x7f)) return false;
    const { iv, value, mac } = JSON.parse(raw);
    const ivBytes = Buffer.from(iv, 'base64');
    const ct = Buffer.from(value, 'base64');
    const want = createHmac('sha256', macKey).update(ivBytes).update(ct).digest();
    const got = Buffer.from(mac, 'base64');
    if (got.length !== want.length || !timingSafeEqual(got, want)) return false;
    const d = createDecipheriv('aes-256-cbc', encKey, ivBytes);
    const { expiration } = JSON.parse(Buffer.concat([d.update(ct), d.final()]));
    return expiration > Date.now(); // also false when missing
  } catch {
    return false; // malformed, or a missing field
  }
};

new GuacamoleLite(
  {
    host: process.env.HOST ?? '127.0.0.1',
    port: Number(process.env.PORT ?? 8080),
    verifyClient: ({ req }) => valid(req.url),
  },
  { host: '127.0.0.1', port: Number(process.env.GUACD_PORT ?? 4822) },
  { crypt: { cypher: 'AES-256-CBC', key: encKey } },
);
// ponytail: no Origin check; the token is the credential. Add one to verifyClient if tokens can leak cross-site.
