"""Needs a docker host with real gVisor, passwordless `sudo iptables`, and something answering as the EC2 metadata service at
169.254.169.254 from OFF the host, so a sandbox's traffic to it is forwarded and the DOCKER-USER rule can catch it: the real service
on an EC2 instance (launched with IMDSv1 on or a hop limit of 2: the defaults hide it from containers), or the fake that the
`gvisor` job in ci.yml sets up (tests/fake_imds.py in a network namespace).

    AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_imds.py

Run it ON that host: it reads the metadata service itself, and adds then removes an iptables rule.
"""
import os
import shlex
import subprocess

from agvisor import create_sandbox

image = os.environ.get("AGVISOR_IMAGE")
RULE = ["DOCKER-USER", "-d", "169.254.169.254", "-j", "DROP"]  # the rule from the README
# the instance id: IMDSv2 token if the service hands one out, plain IMDSv1 otherwise. Runs on the host and in the sandbox.
PROBE = """
import urllib.request as u
U = 'http://169.254.169.254/latest/'
try:
    t = u.urlopen(u.Request(U + 'api/token', method='PUT', headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'}), timeout=3).read().decode()
except Exception:
    t = None
print(u.urlopen(u.Request(U + 'meta-data/instance-id', headers={'X-aws-ec2-metadata-token': t} if t else {}), timeout=3).read().decode())
"""


def iptables(flag):
    return subprocess.run(["sudo", "-n", "iptables", flag, *RULE], capture_output=True).returncode


def seen(run):  # the instance id as `run` (host or sandbox) reads it from the metadata service, or None
    r = run(f"python3 -c {shlex.quote(PROBE)}")
    return r.stdout.strip() if r.returncode == 0 else None


me = seen(lambda c: subprocess.run(c, shell=True, capture_output=True, text=True))
assert me and me.startswith("i-"), f"no EC2 metadata service answers on this host (got {me!r})"
print("metadata service says this is", me)

with create_sandbox(image, network="none", lifetime=300) as sb:
    assert seen(lambda c: sb.exec(c, timeout=60)) is None  # no network, no metadata

with create_sandbox(image, lifetime=300) as sb:  # bridge, the default
    def sandbox_seen():
        return seen(lambda c: sb.exec(c, timeout=60))

    if iptables("-C") == 0:  # the host already blocks it: check that it works, and leave the rule alone
        assert sandbox_seen() is None, "the DOCKER-USER rule is there and the sandbox still reads the metadata service"
    else:
        assert sandbox_seen() == me, "a sandbox on bridge can't read the metadata service, so the rule proves nothing (hop limit 1?)"
        assert iptables("-I") == 0, "iptables -I DOCKER-USER failed (no passwordless sudo?)"
        try:
            assert sandbox_seen() is None, "the DOCKER-USER rule did not stop the sandbox"
        finally:
            iptables("-D")
        assert iptables("-C") != 0, "the rule is still there"  # a test must not leave the host changed
print("ok")
