"""Needs a docker host (CI has none): the MCP file and job tools and the CLI put/get, called as functions against a real sandbox.

    AGVISOR_HOST=ssh://user@vm AGVISOR_IMAGE=python:3.12-slim AGVISOR_RUNTIME=runsc uv run python tests/test_gvisor_mcp.py

AGVISOR_HOST unset -> docker on this machine. AGVISOR_IMAGE: Debian/Ubuntu-based. (agvisor-mcp itself always uses runsc.)
"""
import os
import tempfile
import time
from pathlib import Path

os.environ.update(AGVISOR_NETWORK="none", AGVISOR_IDLE_TTL="120", AGVISOR_LIFETIME="600")  # host and image come from the caller

from agvisor import cli, mcp_server as m  # noqa: E402


def until(job, marker, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        out = m.job_output(job)
        if marker in out:
            return out
        time.sleep(1)
    raise AssertionError(f"{marker!r} never showed up: {out!r}")


try:
    assert m.write_file("/tmp/a b.txt", "café\n").startswith("wrote 6 bytes")
    assert m.read_file("/tmp/a b.txt") == "café\n"
    assert "a b.txt" in m.list_files("/tmp") and m.list_files("/tmp").endswith("[exit 0]")
    assert "No such file" in m.list_files("/nope") and "cannot read" in m.read_file("/nope")
    m.run("head -c 100 /dev/zero > /tmp/z")
    assert "is binary" in m.read_file("/tmp/z")

    job = m.start_job("echo one; sleep 2; echo \"t'wo\"")
    out = until(job, "[exit 0")
    assert out == "one\nt'wo\n\n[exit 0; next offset 10]", out
    assert m.job_output(job, 4) == "t'wo\n\n[exit 0; next offset 10]"  # only what is new
    long = m.start_job("sleep 300")
    assert "[running; next offset 0]" in m.job_output(long)
    assert m.kill_job(long).startswith("sent TERM")
    assert "[exit 143" in until(long, "[exit 143")
    assert "invalid job id" in m.job_output("../x") and "no job" in m.job_output("0123456789ab")

    name = m._sb.name  # the CLI finds the same sandbox through its label
    host = ["--host", os.environ["AGVISOR_HOST"]] if os.environ.get("AGVISOR_HOST") else []
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "in.txt")
        src.write_bytes(bytes(range(256)))
        assert cli.main(["put", name, str(src), "/tmp/cli/in.txt", *host]) == 0
        back = Path(d, "back.bin")
        assert cli.main(["get", name, "/tmp/cli/in.txt", str(back), *host]) == 0 and back.read_bytes() == bytes(range(256))
        tree = Path(d, "tree")
        (tree / "x").mkdir(parents=True)
        (tree / "x" / "f").write_text("f")
        assert cli.main(["put", name, str(tree), "/tmp/cli/tree", *host]) == 0
        assert m.run("cat /tmp/cli/tree/x/f").startswith("f\n")
        assert cli.main(["get", name, "/tmp/cli/tree", str(Path(d, "out")), *host]) == 0 and Path(d, "out", "x", "f").read_text() == "f"
        try:
            cli.main(["get", name, "/tmp/cli/missing", str(Path(d, "m")), *host])
        except SystemExit as e:
            assert e.code == 2  # a usage error, not a traceback
        else:
            raise AssertionError("a missing file must fail")
finally:
    m.reset()
print("ok")
