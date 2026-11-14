"""Resource limits: a full disk is an error, going over memory kills the sandbox (and says so).

    uv run python examples/03_limits.py
"""
import os

from agvisor import Limits, SandboxGone, create_sandbox

HOST = os.environ.get("AGVISOR_HOST")

SMALL = Limits(cpus=1, memory="256m", disk="100m")  # define once, reuse for any number of sandboxes

with create_sandbox("python:3.12-slim", host=HOST, limits=SMALL, lifetime=600) as sb:
    # disk caps the root filesystem. /var/tmp is on it; /tmp is RAM under gVisor, so it falls under memory instead
    r = sb.exec("dd if=/dev/zero of=/var/tmp/big bs=1M count=300 2>&1 | grep -i 'no space'")
    print("300 MB into a 100m disk:", r.stdout.strip())
    print("the sandbox keeps running:", sb.exec("echo still here").stdout.strip())

with create_sandbox("python:3.12-slim", host=HOST, limits=SMALL, lifetime=600) as sb:
    try:  # over memory: the whole sandbox is killed, not one process. exec() raises instead of returning a bare exit code
        sb.exec("""python3 -c 'x = b"x" * 600_000_000; print(len(x))'""")  # b"x" * n writes every page: bytearray(n) would not
        sb.exec("true")
        print("not killed: the limit did not apply")
    except SandboxGone:
        print("600 MB in a 256m sandbox: the sandbox is gone (SandboxGone)")
