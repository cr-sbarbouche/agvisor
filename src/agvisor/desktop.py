"""GUI sandbox specifics: the GNOME-over-RDP image details and the token for gateway/gateway.js."""
import base64
import hmac
import json
import os
import time

GUI_IMAGE = "systemd-debian-gnome"  # built from the gVisor repo, see README
GUI_USER = "gnome"
GUI_HOME = f"/home/{GUI_USER}"  # persisted for named gui sandboxes (assumed home of GUI_USER: verify on the image)
RDP_PORT = 3389


def make_token(key: bytes, rdp_port: int, password: str, ttl: int = 300) -> str:
    """Token for <agvisor-desktop> (needs `pip install agvisor[web]`). Valid for `ttl` seconds to *open* a session.

    `key` is the 32-byte secret gateway/gateway.js also has. guacamole-lite can only decrypt AES-CBC, which does
    not detect tampering: a client could flip IV bits and turn `{"expiration": 1...` into `9...`, making an
    expired token last for centuries. So the token is encrypt-then-MAC: gateway.js checks the HMAC first.
    """
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(key) != 32:
        raise ValueError("key must be exactly 32 bytes (same as AGVISOR_KEY, hex-decoded)")
    # one secret, two purposes: derive separate AES and HMAC keys (gateway.js derives them the same way)
    enc_key, mac_key = (hmac.digest(key, label, "sha256") for label in (b"agvisor-enc", b"agvisor-mac"))

    msg = json.dumps({
        "expiration": int((time.time() + ttl) * 1000),  # checked by gateway.js
        "connection": {"type": "rdp", "settings": {
            "hostname": "127.0.0.1", "port": str(rdp_port),  # guacd runs on the docker host
            "username": GUI_USER, "password": password,
            "security": "any", "ignore-cert": True, "resize-method": "display-update"}},
    }).encode()
    iv, pad = os.urandom(16), padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
    ct = enc.update(pad.update(msg) + pad.finalize()) + enc.finalize()
    mac = hmac.HMAC(mac_key, iv + ct, "sha256").digest()  # over the exact bytes gateway.js will verify
    b64 = lambda b: base64.b64encode(b).decode()  # guacamole-lite token format, plus `mac` (it ignores it)
    return b64(json.dumps({"iv": b64(iv), "value": b64(ct), "mac": b64(mac)}).encode())
