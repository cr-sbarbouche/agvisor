"""Files and directories in and out, and a background job you can watch.

    uv run python examples/04_files_and_jobs.py
"""
import os
import tempfile
import time
from pathlib import Path

from agvisor import create_sandbox

HOST = os.environ.get("AGVISOR_HOST")

with create_sandbox("python:3.12-slim", host=HOST, lifetime=900) as sb:
    sb.put("/tmp/job/input.csv", b"a,b\n1,2\n3,4\n")  # parent directories are created
    sb.exec("awk -F, 'NR>1 {s += $1 + $2} END {print s}' /tmp/job/input.csv > /tmp/job/result.txt")
    print("result:", sb.get("/tmp/job/result.txt").decode().strip())  # bytes; capped at 10 MB, the sandbox is untrusted

    with tempfile.TemporaryDirectory() as tmp:  # a whole directory in, changed inside, and back out
        (Path(tmp) / "hello.txt").write_text("hi\n")
        sb.put_dir(tmp, "/tmp/project")
        sb.exec("echo 'added in the sandbox' >> /tmp/project/hello.txt")
        sb.get_dir("/tmp/project", str(Path(tmp) / "back"))
        print("directory:", (Path(tmp) / "back" / "hello.txt").read_text().split("\n")[:2])

    # spawn(): returns at once, runs on without this exec(), and its output can be read as it grows
    job, offset = sb.spawn("for i in 1 2 3; do echo step $i; sleep 1; done"), 0
    while True:
        finished = job.poll() is not None  # None while it runs, then the exit code
        chunk = job.read(offset)  # the bytes since `offset`, stdout and stderr merged
        offset += len(chunk)
        print(chunk.decode(), end="")
        if finished:
            break
        time.sleep(1)
    print("job exit code:", job.poll())
