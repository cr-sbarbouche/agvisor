"""Throwaway: the corrected `agvisor run` commands, and how `memory` reacts to root-fs writes with different writers."""
import json, subprocess, time

from agvisor import create_sandbox

IMAGE = "python:3.12-slim"


def out(k, v):
    print("RESULT", k, json.dumps(v), flush=True)


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {"stdout": r.stdout.strip(), "stderr": r.stderr.strip()[-300:], "rc": r.returncode}


out("run_ok", sh("""agvisor run python:3.12-slim -- 'python -c "print(2 + 2)"'"""))
out("run_fail", sh("agvisor run python:3.12-slim -- 'exit 7'"))
out("run_egress", sh("agvisor run python:3.12-slim --egress pypi.org --egress files.pythonhosted.org -- "
                     "'pip install -q --root-user-action=ignore --disable-pip-version-check six && echo installed'"))
out("run_egress_blocked", sh("agvisor run python:3.12-slim --egress pypi.org -- "
                             "'python -c \"import urllib.request as u; u.urlopen(\\\"https://example.com\\\", timeout=15)\" 2>&1 | tail -1'"))

MiB = 2**20
WRITERS = {
    "dd bs=1M": "dd if=/dev/zero of=/var/tmp/f bs=1M count=100 2>/dev/null",
    "head -c 100M": "head -c 100M /dev/zero > /var/tmp/f",
    "dd bs=4k": "dd if=/dev/zero of=/var/tmp/f bs=4k count=25600 2>/dev/null",
    "python write": "python3 -c \"open('/var/tmp/f','wb').write(bytes(100*2**20))\"",
    "dd bs=1M + sync": "dd if=/dev/zero of=/var/tmp/f bs=1M count=100 2>/dev/null; sync",
}
for label, cmd in WRITERS.items():
    with create_sandbox(IMAGE, lifetime=300) as sb:
        base = sb.stats()["memory"]
        sb.exec(cmd)
        after = sb.stats()["memory"]
        time.sleep(8)
        later = sb.stats()["memory"]
        out("rootfs", {"writer": label, "after_mib": round((after - base) / MiB), "8s_later_mib": round((later - base) / MiB)})
