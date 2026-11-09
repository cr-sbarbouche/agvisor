"""Needs a docker host with real gVisor (CI has no runsc): an image with an ENTRYPOINT must still work.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=ubuntu:24.04 uv run python tests/test_gvisor_entrypoint.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: any Debian/Ubuntu image already on that host: a one-line
derived image (`ENTRYPOINT ["echo"]`) is built from it on the host, so nothing is pulled.
"""
import os
import subprocess
import uuid

from agvisor import create_sandbox

host, base = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE", "ubuntu:24.04")
docker = ["docker", *(["-H", host] if host else [])]
tag = f"agvisor-entrypoint-test:{uuid.uuid4().hex[:8]}"
subprocess.run([*docker, "build", "-q", "-t", tag, "-"], input=f'FROM {base}\nENTRYPOINT ["echo"]\n', text=True, check=True,
               capture_output=True)
try:  # with the ENTRYPOINT in place, `sleep infinity` would be echoed and the container would exit at once
    with create_sandbox(tag, host=host, network="none") as sb:
        assert sb.exec("echo alive").stdout == "alive\n"
finally:
    subprocess.run([*docker, "rmi", "-f", tag], capture_output=True)
print("ok")
