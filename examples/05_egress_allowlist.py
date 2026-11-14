"""An outbound allowlist instead of all-or-nothing network: pip works, everything else is refused.

    uv run python examples/05_egress_allowlist.py

Needs the python:3.12-slim image on the docker host (the allowlist proxy runs it).
"""
import os
import shlex

from agvisor import create_sandbox

HOST = os.environ.get("AGVISOR_HOST")

with create_sandbox("python:3.12-slim", host=HOST, egress=["pypi.org", "*.pythonhosted.org"], lifetime=600) as sb:
    r = sb.exec("pip install --quiet --no-cache-dir --target /tmp/t six && ls /tmp/t | head -2", timeout=120)
    print("pip install through the allowlist:", "ok" if r.returncode == 0 else r.stdout + r.stderr)

    # a host that is not on the list: the proxy answers with the rule that refused it
    refused = ("import urllib.request as u, urllib.error as e\n"
               "try: u.urlopen('http://example.org/', timeout=20)\n"
               "except e.HTTPError as x: print(x.code, x.read().decode())")
    print("example.org:", sb.exec(f"python3 -c {shlex.quote(refused)}").stdout.strip())
