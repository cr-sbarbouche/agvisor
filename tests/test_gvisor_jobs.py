"""Needs a docker host with real gVisor (CI has no runsc): spawn() runs a shell script that relies on GNU timeout, setsid-less
process groups and /proc, which only a real container can check.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim uv run python tests/test_gvisor_jobs.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: Debian/Ubuntu-based, already on that host. Takes ~2 minutes (two
idle_ttl waits).
"""
import os
import time

from agvisor import Job, SandboxGone, create_sandbox

kw = dict(image=os.environ.get("AGVISOR_IMAGE"), host=os.environ.get("AGVISOR_HOST"), network="none")


def sleepers(sb):  # processes called `sleep`, counted from /proc (slim images have no ps)
    return int(sb.exec("n=0; for p in /proc/[0-9]*; do [ \"$(cat $p/comm 2>/dev/null)\" = sleep ] && n=$((n+1)); done; echo $n").stdout)


with create_sandbox(lifetime=600, **kw) as sb:
    job = sb.spawn("echo start; echo oops >&2; sleep 2; echo \"it's\"; echo $((1+2)); exit 3")  # quoting, stderr, exit code
    assert job.poll() is None  # spawn() returned at once: the job runs on after the exec() that started it
    assert job.wait(timeout=60) == 3 and job.poll() == 3
    log = job.read()
    assert log == b"start\noops\nit's\n3\n", log
    assert job.read(len(b"start\noops\n")) == b"it's\n3\n" and job.read(len(log)) == b""  # offsets: only what is new
    assert Job(sb, job.id).wait() == 3  # picked up again by id

    big = sb.spawn("yes x | head -c 3000000")
    assert big.wait(timeout=60) == 0
    got, off = b"", 0
    while chunk := big.read(off, max_bytes=1_000_000):
        got, off = got + chunk, off + len(chunk)
    assert len(got) == 3_000_000

    t = sb.spawn("sleep 300", timeout=2)  # its own timeout
    assert t.wait(timeout=60) == 124

    tree = sb.spawn("sleep 300 & sleep 300 & wait")  # kill() reaches what the command started, not just the shell
    time.sleep(1)
    assert sleepers(sb) == 2 and tree.poll() is None
    tree.kill()
    assert tree.wait(timeout=30) == 143 and sleepers(sb) == 0, sleepers(sb)
    tree.kill()  # already over: not an error

    stubborn = sb.spawn("trap '' TERM; while true; do sleep 1; done")  # ignores TERM
    time.sleep(1)
    stubborn.kill()
    try:
        stubborn.wait(timeout=3)
    except TimeoutError:
        pass
    else:
        raise AssertionError("a job that ignores TERM ended")
    stubborn.kill(force=True)
    assert stubborn.wait(timeout=30) == 137 and sleepers(sb) == 0

    for call in (Job(sb, "0123456789ab").poll, Job(sb, "0123456789ab").kill, lambda: Job(sb, "0123456789ab").read()):
        try:
            call()
        except LookupError:
            continue
        raise AssertionError("an unknown job must raise LookupError")

# idle_ttl: polling, reading and waiting count as activity, a running job does not
with create_sandbox(idle_ttl=30, **kw) as sb:
    job = sb.spawn("sleep 40")  # longer than idle_ttl
    assert job.wait(timeout=90) == 0  # wait() is an exec every ~18 s: the sandbox stayed alive for it
    sb.exec("true")
with create_sandbox(idle_ttl=30, **kw) as sb:
    job = sb.spawn("sleep 300")
    time.sleep(45)  # nobody asks: the idle watchdog deletes the sandbox, job and all
    try:
        job.poll()
    except SandboxGone:
        pass
    else:
        raise AssertionError("a running job must not keep a sandbox alive")
print("ok")
