"""Run code in a gVisor sandbox: commands, stdin, timeouts, a Python session that stays up.

    uv run python examples/01_quickstart.py
    AGVISOR_HOST=ssh://user@my-vm uv run python examples/01_quickstart.py    # docker on another machine
"""
import os

from agvisor import create_sandbox

HOST = os.environ.get("AGVISOR_HOST")  # None: docker on this machine

# lifetime: the sandbox deletes itself after 10 minutes, even if this script dies
with create_sandbox("python:3.12-slim", host=HOST, lifetime=600) as sb:
    r = sb.exec("uname -s; python3 -c 'print(1 + 1)'")
    print(r.returncode, r.stdout.split())  # 0 ['Linux', '2']: this is gVisor's Linux, not the host's kernel

    print(sb.exec("tr a-z A-Z", input="piped in").stdout)  # stdin

    r = sb.exec("sleep 30", timeout=2)  # too slow: killed, returncode 124
    print("timed out:", r.returncode == 124)

    sb.python("squares = [n * n for n in range(10)]")  # one interpreter, like notebook cells: variables survive
    print(sb.python("sum(squares)").stdout.strip())  # 285
