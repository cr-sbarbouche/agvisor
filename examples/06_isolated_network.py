"""network="isolated": sandboxes keep the internet but can't connect to each other (files were never shared).

    uv run python examples/06_isolated_network.py

The control is the default network, "bridge", where a sandbox can reach a server running in another one.
"""
import os
import shlex

from agvisor import create_sandbox

HOST, IMAGE = os.environ.get("AGVISOR_HOST"), "python:3.12-slim"


def py(sb, code, timeout=60):
    return sb.exec(f"python3 -c {shlex.quote(code)}", timeout=timeout)


def b_reaches_a(network):
    """Sandbox `a` serves HTTP on port 8000. Can sandbox `b`, on the same network, connect to it?"""
    with create_sandbox(IMAGE, host=HOST, network=network, lifetime=300) as a, \
            create_sandbox(IMAGE, host=HOST, network=network, lifetime=300) as b:
        a.spawn("python3 -m http.server 8000")
        py(a, "import time, urllib.request as u\n"
              "for _ in range(30):\n"
              "    try: u.urlopen('http://127.0.0.1:8000/', timeout=2); break\n"
              "    except OSError: time.sleep(1)")  # wait until the server is up
        ip = py(a, "import socket; print(socket.gethostbyname(socket.gethostname()))").stdout.strip()
        r = py(b, f"import urllib.request as u; print(u.urlopen('http://{ip}:8000/', timeout=5).status)")
        return r.stdout.strip() == "200", "timed out" in r.stderr


for network in ("bridge", "isolated"):
    ok, timed_out = b_reaches_a(network)
    print(f'network="{network}":'.ljust(22), "b reached a's server" if ok else "b could not reach a's server" + (" (timed out)" if timed_out else ""))

with create_sandbox(IMAGE, host=HOST, network="isolated", lifetime=300) as sb:  # isolated from its peers, not from the world
    r = py(sb, "import urllib.request as u; print(u.urlopen('https://example.com/', timeout=20).status)")
    print('network="isolated":'.ljust(22), "example.com ->", r.stdout.strip())
