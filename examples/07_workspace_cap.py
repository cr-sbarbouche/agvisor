"""Limits(workspace=...) caps a named sandbox's /workspace, so one runaway process can't fill the host's disk through it.

    uv run python examples/07_workspace_cap.py

Needs a docker host whose /var/lib/docker/volumes is on xfs mounted with `pquota` (README, Host setup). Without it
create_sandbox raises instead of leaving the volume uncapped, and this script says so and stops.
"""
import os

from agvisor import Limits, Sandbox, create_sandbox

HOST, NAME, IMAGE = os.environ.get("AGVISOR_HOST"), "example-cap", "python:3.12-slim"
CAP = Limits(workspace="64m")


def write(sb, name, mib):  # dd fails when the disk is full; keep just that line
    return sb.exec(f"dd if=/dev/zero of={name} bs=1M count={mib} 2>&1 | grep -iE 'no space|quota' || echo ok").stdout.strip()


Sandbox(name=NAME, host=HOST).delete()  # start clean, in case an earlier run died
try:
    sb = create_sandbox(IMAGE, host=HOST, name=NAME, limits=CAP)
except RuntimeError as e:
    raise SystemExit(f"this docker host can't cap /workspace: {e}")

with sb:
    print("32 MiB into a 64m /workspace:  ", write(sb, "a.bin", 32))
    print("100 MiB more:                  ", write(sb, "b.bin", 100))

with create_sandbox(IMAGE, host=HOST, name=NAME, limits=CAP) as sb:  # the volume, and its cap, outlive the container
    print("after stop(), a.bin is        ", sb.exec("stat -c %s a.bin").stdout.strip(), "bytes; 40 MiB more:", write(sb, "c.bin", 40))
    sb.delete()
