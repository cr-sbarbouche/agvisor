// WebSocket <-> guacd bridge for <agvisor-desktop>. Run from a checkout: `npm install && npm run gateway`.
// (guacamole-lite is a devDependency on purpose: the browser package must not pull in server code.)
// Env: AGVISOR_KEY (64 hex chars, same key as Sandbox.desktop_token), HOST (127.0.0.1), PORT (8080), GUACD_PORT (4822)
import { createDecipheriv } from 'node:crypto';
import GuacamoleLite from 'guacamole-lite';

const key = Buffer.from(process.env.AGVISOR_KEY ?? '', 'hex');
if (key.length !== 32) throw new Error('AGVISOR_KEY must be 64 hex chars (openssl rand -hex 32)');

// Expiry is enforced at the WebSocket upgrade, not in guacamole-lite's processConnectionSettings
// callback: that one closes the browser socket but still opens guacd and sends the RDP credentials.
const valid = (token) => {
  try {
    const { iv, value } = JSON.parse(Buffer.from(token, 'base64'));
    const d = createDecipheriv('aes-256-cbc', key, Buffer.from(iv, 'base64'));
    const { expiration } = JSON.parse(Buffer.concat([d.update(Buffer.from(value, 'base64')), d.final()]));
    return expiration > Date.now(); // also false when missing
  } catch {
    return false; // undecryptable or malformed
  }
};

new GuacamoleLite(
  {
    host: process.env.HOST ?? '127.0.0.1',
    port: Number(process.env.PORT ?? 8080),
    verifyClient: ({ req }) => valid(new URL(req.url, 'http://x').searchParams.get('token') ?? ''),
  },
  { host: '127.0.0.1', port: Number(process.env.GUACD_PORT ?? 4822) },
  { crypt: { cypher: 'AES-256-CBC', key } },
);
// ponytail: no Origin check; the token is the credential. Add one to verifyClient if tokens can leak cross-site.
