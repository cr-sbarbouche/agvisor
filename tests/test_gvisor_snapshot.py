"""Needs a docker host with real gVisor (the `gvisor` job in ci.yml has it): snapshot() and restore() copy volumes in a runsc container.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=node:22-alpine uv run python tests/test_gvisor_snapshot.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: any image already on that host (needs sh, rm, cp).
"""
import os
import uuid

from agvisor import create_sandbox

host, image = os.environ.get("AGVISOR_HOST"), os.environ.get("AGVISOR_IMAGE")
kw = dict(image=image, name=f"snaptest-{uuid.uuid4().hex[:8]}", host=host, network="none")  # unique: never meets someone's sandbox

sb = create_sandbox(**kw)
try:
    sb.exec("mkdir -p venv/bin && echo good > venv/bin/tool && echo cfg > .hidden && ln -s venv/bin/tool link")
    sb.snapshot("clean")
    sb.exec("echo evil > venv/bin/tool && rm .hidden link && echo junk > extra")
    sb.restore("clean")
    out = sb.exec("cat venv/bin/tool .hidden; readlink link; ls -A").stdout
    assert out.split() == ["good", "cfg", "venv/bin/tool", ".hidden", "link", "venv"], out  # evil and extra are gone
    try:
        sb.restore("nope")
    except ValueError:
        pass
    else:
        raise AssertionError("restored a snapshot that does not exist")
    assert sb.exec("cat venv/bin/tool").stdout.strip() == "good"  # the failed restore wiped nothing
    sb.snapshot("other")
    assert sb.list_snapshots() == ["clean", "other"]
    sb.delete_snapshot("other")
    sb.delete_snapshot("other")  # already gone: fine
    assert sb.list_snapshots() == ["clean"]
finally:
    sb.delete()  # volumes too, even when an assert failed
print("ok")
