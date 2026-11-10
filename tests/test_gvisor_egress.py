"""Needs a docker host with real gVisor and internet (CI has neither): egress= builds a private network and a proxy container.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_egress.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: python 3.12+, with pip, already on that host: it is the sandbox
image here and the proxy image (python:3.12-slim) must be on the host too. Takes ~2 minutes (an expiry and a prune).
"""
import os
import time

from agvisor import create_sandbox, prune_egress, sandbox
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
with create_sandbox(image, host=host, egress=["pypi.org", "*.pythonhosted.org", "example.com"], lifetime=600) as sb:
    r = py(sb, FETCH.format("https://pypi.org/simple/six/"))  # allowed: a CONNECT tunnel, and it is the first request
    assert r.stdout.strip() == "200", (r.stdout, r.stderr)
    assert py(sb, FETCH.format("http://example.com/")).stdout.strip() == "200"  # plain http: the request is forwarded
    r = py(sb, FETCH.format("https://example.org/"))  # not on the list
    assert r.returncode and "403" in r.stderr, r.stderr
    r = py(sb, "import urllib.request as u, urllib.error as e\ntry: u.urlopen('http://example.org/', timeout=20)\n"
               "except e.HTTPError as x: print(x.code, x.read().decode())")  # the proxy's own answer says which rule it was
    assert r.stdout.strip() == "403 agvisor egress: example.org:80 is not in this sandbox's egress allowlist", (r.stdout, r.stderr)
    r = py(sb, FETCH.format("http://10.255.255.254/"))  # an address that is not listed (here: WSL's resolver): refused too
    assert r.returncode and "403" in r.stderr, r.stderr
    # no way round the proxy: the network has no route out
    r = py(sb, "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)")
    assert r.returncode and ("unreachable" in r.stderr or "timed out" in r.stderr), r.stderr
    r = py(sb, "import socket; print(socket.gethostbyname('example.com'))", timeout=30)
    print("DNS for an unlisted name from inside:", "resolved " + r.stdout.strip() if not r.returncode else "fails")
    r = sb.exec("pip install --quiet --no-cache-dir --target /tmp/t six && ls /tmp/t | head -3", timeout=120)  # pip honours HTTPS_PROXY
    assert r.returncode == 0 and "six" in r.stdout, (r.stdout, r.stderr)
    names, nets = mine()
    assert len(names) == len(before[0]) + 2 and len(nets) == len(before[1]) + 1, (names, nets)  # sandbox + proxy, one network
assert mine() == before, mine()  # stop() took the proxy and the network with it

# a sandbox that expires by itself (--rm) can't remove its proxy and network: prune_egress() does, once the proxy is old enough,
# and never touches the proxy of a sandbox that is still there
with create_sandbox(image, host=host, egress=["pypi.org"], lifetime=300) as live:
    sb = create_sandbox(image, host=host, egress=["pypi.org"], lifetime=15)
    time.sleep(25)
    all_containers = admin._docker("ps", "-a", "--format", "{{.Names}}").stdout.split()
    assert sb.name not in all_containers and sb._proxy in all_containers, "--rm should have taken the sandbox only"
    prune_egress(host)  # too young: its sandbox's image could still be downloading
    assert sb._proxy in admin._docker("ps", "-a", "--format", "{{.Names}}").stdout.split()
    sandbox._GRACE = 0  # (the real grace is 10 minutes)
    time.sleep(45)  # the network must be a minute old too: one that another start is setting up is left alone
    prune_egress(host)
    names, nets = mine()
    assert sb._proxy not in names and sb._net not in nets, (names, nets)
    assert live._proxy in names and live._net in nets and live.exec("true").returncode == 0  # the live one is untouched
assert mine() == before, mine()
print("ok")
