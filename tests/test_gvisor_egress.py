"""Needs a docker host with real gVisor and internet (CI has neither): egress= builds a private network and a proxy container.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_egress.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: python 3.12+, with pip, already on that host: it is the sandbox
image here and the proxy image (python:3.12-slim) must be on the host too. Takes ~3 minutes (an expiry and a prune).
"""
import os
import time

from agvisor import create_sandbox
from agvisor.sandbox import Sandbox

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")
admin = Sandbox(host=host)  # only for its _docker()


def mine():  # proxies and networks that egress= made
    names = admin._docker("ps", "-a", "--filter", "label=agvisor.egress", "--format", "{{.Names}}").stdout.split()
    nets = admin._docker("network", "ls", "--filter", "label=agvisor.egress", "--format", "{{.Name}}").stdout.split()
    return names, nets


def py(sb, code, timeout=60):
    return sb.exec(f"python3 -c {__import__('shlex').quote(code)}", timeout=timeout)


FETCH = "import urllib.request as u; r = u.urlopen({!r}, timeout=20); print(r.status)"
before = mine()
with create_sandbox(image, host=host, egress=["pypi.org", "*.pythonhosted.org", "files.pythonhosted.org"], lifetime=600) as sb:
    r = py(sb, FETCH.format("https://pypi.org/simple/six/"))  # allowed: a CONNECT tunnel, and it is the first request
    assert r.stdout.strip() == "200", (r.stdout, r.stderr)
    assert py(sb, FETCH.format("http://pypi.org/simple/six/")).stdout.strip() == "200"  # plain http (through a redirect to https)
    r = py(sb, FETCH.format("https://example.com/"))  # not on the list
    assert r.returncode and "403" in r.stderr, r.stderr
    r = py(sb, FETCH.format("http://example.com/"))
    assert r.returncode and "403" in r.stderr and "egress allowlist" in r.stderr, r.stderr
    r = py(sb, FETCH.format("http://localhost:80/"))  # not even listed; by name a private address is never reachable
    assert r.returncode and "403" in r.stderr
    # no way round the proxy: the network has no route out
    r = py(sb, "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)")
    assert r.returncode and ("unreachable" in r.stderr or "timed out" in r.stderr), r.stderr
    r = py(sb, "import socket; print(socket.gethostbyname('example.com'))", timeout=30)
    print("DNS for an unlisted name from inside:", "resolved " + r.stdout.strip() if not r.returncode else "fails")
    r = sb.exec("pip install --quiet --no-cache-dir --target /tmp/t six && ls /tmp/t | head -3", timeout=120)  # pip honours HTTPS_PROXY
    assert r.returncode == 0 and "six" in r.stdout, (r.stdout, r.stderr)
    names, nets = mine()
    assert len(names) == len(before[0]) + 1 and len(nets) == len(before[1]) + 1, (names, nets)
assert mine() == before, mine()  # stop() took the proxy and the network with it

# a sandbox that expires by itself: the proxy goes within ~15 s (it notices), its network at the next egress start
sb = create_sandbox(image, host=host, egress=["pypi.org"], lifetime=20)
proxy = sb._proxy
deadline = time.time() + 90
while time.time() < deadline and proxy in mine()[0] + admin._docker("ps", "-a", "--format", "{{.Names}}").stdout.split():
    time.sleep(3)
assert proxy not in admin._docker("ps", "-a", "--format", "{{.Names}}").stdout.split(), "the proxy outlived its sandbox"
assert len(mine()[1]) == len(before[1]) + 1  # the empty network is still there...
time.sleep(60)  # ...until it is old enough to be pruned (a network being set up must not be)
with create_sandbox(image, host=host, egress=["pypi.org"], lifetime=60):
    assert len(mine()[1]) == len(before[1]) + 1  # only the new one
assert mine() == before
print("ok")
