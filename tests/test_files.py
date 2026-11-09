"""Needs a docker host (the shell commands behind put() and get() run in a real container; CI has no daemon):

    AGVISOR_HOST=ssh://user@vm uv run python tests/test_files.py
    AGVISOR_RUNTIME=runc uv run python tests/test_files.py      # e.g. Docker Desktop, where there is no runsc

AGVISOR_HOST unset -> docker on this machine. AGVISOR_RUNTIME defaults to runsc. The image needs GNU coreutils
(AGVISOR_IMAGE, default ubuntu:24.04).
"""
import os

from agvisor import create_sandbox

with create_sandbox(os.environ.get("AGVISOR_IMAGE"), host=os.environ.get("AGVISOR_HOST"), network="none", lifetime=300,
                    runtime=os.environ.get("AGVISOR_RUNTIME", "runsc")) as sb:
    blob = bytes(range(256)) * 4096  # 1 MB with every byte value, newlines and NULs included
    for path in ("/tmp/new/dir/blob.bin", "/tmp/it's a \"file\"; $(id)"):  # nested dirs are created; the shell sees nothing special
        sb.put(path, blob)
        assert sb.get(path) == blob, path
    sb.put("/tmp/new/dir/blob.bin", b"short")  # replaced, not appended
    assert sb.get("/tmp/new/dir/blob.bin") == b"short"
    sb.put("/tmp/empty", b"")
    assert sb.get("/tmp/empty") == b""

    for bad in ("/tmp/missing", "/tmp"):  # no such file, a directory
        try:
            sb.get(bad)
        except RuntimeError as e:
            assert "not a file" in str(e), e
        else:
            raise AssertionError(f"read {bad}")
    sb.exec("head -c 5000000 /dev/zero > /tmp/big")
    try:
        sb.get("/tmp/big", max_bytes=1000)
    except ValueError:
        pass
    else:
        raise AssertionError("read a file over max_bytes")
    try:
        sb.put("/proc/nope/x", b"1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("a failed write must raise")
print("ok")
