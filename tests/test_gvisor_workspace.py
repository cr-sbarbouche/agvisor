"""Needs a docker host with real gVisor whose /var/lib/docker/volumes is on xfs mounted with pquota (the `gvisor` job in ci.yml
sets that up): Limits(workspace=) must cap a named sandbox's /workspace, and keep capping it after a stop.

    AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_workspace.py

Takes ~30 seconds. Deletes the sandbox it makes (`wscap-test`).
"""
import os

from agvisor import Limits, Sandbox, create_sandbox

image = os.environ.get("AGVISOR_IMAGE")
CAP, NAME = Limits(workspace="64m"), "wscap-test"


def megabytes(sb, path):  # size of a file in /workspace in MiB, 0 if it isn't there
    return int(sb.exec(f"stat -c %s {path} 2>/dev/null || echo 0").stdout.strip()) // 2**20


def write(sb, name, mib):  # dd exits non-zero when the disk is full
    return sb.exec(f"dd if=/dev/zero of={name} bs=1M count={mib} 2>&1; echo rc=$?")


def full(r):
    return "rc=0" not in r.stdout and ("No space left" in r.stdout or "quota" in r.stdout), r.stdout


Sandbox(name=NAME).delete()  # a clean start: a volume that exists with another cap would be refused
with create_sandbox(image, name=NAME, limits=CAP) as sb:
    assert "rc=0" in write(sb, "a", 32).stdout  # inside the cap
    ok, out = full(write(sb, "b", 100))  # past it: stops when the 64m are used, instead of filling the host's disk
    assert ok, out
    assert megabytes(sb, "a") == 32 and megabytes(sb, "b") <= 32, (megabytes(sb, "a"), megabytes(sb, "b"))

with create_sandbox(image, name=NAME, limits=CAP) as sb:  # stop() keeps the volume, and its cap
    assert megabytes(sb, "a") == 32
    ok, out = full(write(sb, "c", 10))
    assert ok, out

try:  # the cap is fixed when the volume is created: another one is refused, not ignored
    create_sandbox(image, name=NAME, limits=Limits(workspace="128m"))
except ValueError as e:
    assert "delete() it first" in str(e), e
else:
    raise AssertionError("a different cap was accepted")
with create_sandbox(image, name=NAME) as sb:  # asking for no cap doesn't remove the one the volume has
    ok, out = full(write(sb, "d", 10))
    assert ok, out

Sandbox(name=NAME).delete()
with create_sandbox(image, name=NAME):  # a volume made without a cap...
    pass
try:  # ...can't be capped by asking later: that would look like a cap and be none
    create_sandbox(image, name=NAME, limits=CAP)
except ValueError as e:
    assert "no workspace cap" in str(e), e
else:
    raise AssertionError("an uncapped volume passed for a capped one")
Sandbox(name=NAME).delete()
print("ok")
