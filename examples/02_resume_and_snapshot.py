"""A named sandbox keeps /workspace between sessions, and can go back to a saved state.

    uv run python examples/02_resume_and_snapshot.py
"""
import os

from agvisor import Sandbox, create_sandbox

HOST, NAME, IMAGE = os.environ.get("AGVISOR_HOST"), "example-resume", "python:3.12-slim"

Sandbox(name=NAME, host=HOST).delete()  # start clean, in case an earlier run died

with create_sandbox(IMAGE, host=HOST, name=NAME) as sb:  # session 1: cwd is /workspace, which is kept
    sb.exec("echo 'chapter 1' > notes.txt")
    sb.snapshot("good")  # a copy of /workspace
    sb.exec("echo 'oops, wrong' > notes.txt")
    print("before restore:", sb.exec("cat notes.txt").stdout.strip())
    sb.restore("good")  # /workspace as it was; a new container (processes are lost)
    print("after restore: ", sb.exec("cat notes.txt").stdout.strip())
# the container is gone, CPU and RAM are free; the files are not

with create_sandbox(IMAGE, host=HOST, name=NAME) as sb:  # session 2: a fresh container, the same /workspace
    print("next session:  ", sb.exec("cat notes.txt").stdout.strip())
    print("snapshots:     ", sb.list_snapshots())
    sb.delete()  # forget it: erases the files and the snapshots too
