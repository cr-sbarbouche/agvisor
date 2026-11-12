"""Needs a docker host with real gVisor: the disk cap is a runsc feature, which the `gvisor` job in ci.yml has.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=node:22-alpine uv run python tests/test_gvisor_disk.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: any image already on that host (avoids a pull).
"""
import os

from agvisor import Limits, create_sandbox

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")
# /var/tmp, not /tmp: gVisor mounts a RAM-backed tmpfs on /tmp, which never touches the disk cap
write = "dd if=/dev/zero of=/var/tmp/big bs=1M count=300 2>&1; du -m /var/tmp/big"

with create_sandbox(image, limits=Limits(memory="512m", disk="100m"), network="none", host=host) as sb:
    out = sb.exec(write, timeout=60).stdout
    assert "No space left on device" in out, out  # stopped by the cap, not by RAM or the host disk
    assert out.rstrip().endswith("100\t/var/tmp/big"), out  # ...at exactly the cap
print("ok")
