"""Needs a docker host with real gVisor and internet (the `gvisor` job in ci.yml has both): network="isolated" must stop
sandboxes connecting to each other (network="bridge", the control, must not), while names still resolve and the internet works.

    AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_isolated.py

Takes ~1 minute. Leaves the `agvisor-isolated` network behind (it is shared on purpose).
"""
import os
import shlex

from agvisor import create_sandbox
from agvisor.sandbox import Sandbox

image = os.environ.get("AGVISOR_IMAGE")
admin = Sandbox()  # only for its _docker()


def py(sb, code, timeout=60):
    return sb.exec(f"python3 -c {shlex.quote(code)}", timeout=timeout)


def peer_reachable(network):  # can a sandbox connect to a server that runs in another sandbox on `network`?
    with create_sandbox(image, network=network, lifetime=300) as a, create_sandbox(image, network=network, lifetime=300) as b:
        a.spawn("python3 -m http.server 8000")
        up = py(a, "import time, urllib.request as u\nfor _ in range(30):\n    try: u.urlopen('http://127.0.0.1:8000/', timeout=2); break\n"
                   "    except OSError: time.sleep(1)\nelse: raise SystemExit('the server did not start')")
        assert up.returncode == 0, (up.stdout, up.stderr)
        ip = admin._docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", a.name).stdout.strip()
        r = py(b, f"import urllib.request as u; print(u.urlopen('http://{ip}:8000/', timeout=5).status)")
        return r.stdout.strip() == "200", r


assert peer_reachable("bridge")[0]  # the control: same setup on the default network, and it connects
ok, r = peer_reachable("isolated")
assert not ok and "timed out" in r.stderr, (r.stdout, r.stderr)

with create_sandbox(image, network="isolated", lifetime=300) as sb:  # isolated from its peers, not from the world
    r = py(sb, "import urllib.request as u; print(u.urlopen('https://example.com/', timeout=20).status)")  # a name: needs the DNS fix
    assert r.stdout.strip() == "200", (r.stdout, r.stderr)
print("ok")
