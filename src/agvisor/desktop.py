"""GUI sandbox specifics: the GNOME-over-RDP image details and the token for web/gateway.js."""
import base64
import json
import os
import time

GUI_IMAGE = "systemd-debian-gnome"  # built from the gVisor repo, see README
GUI_USER = "gnome"
GUI_HOME = f"/home/{GUI_USER}"  # persisted for named gui sandboxes (assumed home of GUI_USER: verify on the image)
RDP_PORT = 3389


def make_token(key: bytes, rdp_port: int, password: str, ttl: int = 300) -> str:
    """Encrypted token for <agvisor-desktop> (needs `pip install agvisor[web]`).
    `key` is the 32-byte AES key web/gateway.js also has. Valid for `ttl` seconds to *open* a session."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    msg = json.dumps({
        "expiration": int((time.time() + ttl) * 1000),  # checked by gateway.js
        "connection": {"type": "rdp", "settings": {
            "hostname": "127.0.0.1", "port": str(rdp_port),  # guacd runs on the docker host
            "username": GUI_USER, "password": password,
            "security": "any", "ignore-cert": True, "resize-method": "display-update"}},
    }).encode()
    iv, pad = os.urandom(16), padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(pad.update(msg) + pad.finalize()) + enc.finalize()
    b64 = lambda b: base64.b64encode(b).decode()  # guacamole-lite token format
    return b64(json.dumps({"iv": b64(iv), "value": b64(ct)}).encode())
