"""Throwaway: the real output shown in the 0.1.9 video. One RESULT line per fact."""
import json, subprocess, time, urllib.request

from agvisor import Limits, Pool, create_sandbox

IMAGE = "python:3.12-slim"
URL = "https://api.anthropic.com/v1/models"
FETCH = ("import urllib.request as u, urllib.error as e, json, sys\n"
         "try:\n r = u.urlopen(sys.argv[1], timeout=20); print(r.status)\n"
         "except e.HTTPError as x: print(x.code, json.loads(x.read())['error']['message'])\n")


def out(k, v):
    print("RESULT", k, json.dumps(v), flush=True)


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return {"stdout": r.stdout.strip(), "stderr": r.stderr.strip()[-300:], "rc": r.returncode}


with create_sandbox(IMAGE, egress=["api.anthropic.com"], lifetime=600) as plain:
    plain.put("/tmp/f.py", FETCH.encode())
    out("inject_plain", plain.exec(f"python /tmp/f.py {URL}").stdout.strip())
with create_sandbox(IMAGE, inject={"api.anthropic.com": {"x-api-key": "sk-ant-demo-0000"}}, lifetime=600) as sb:
    sb.put("/tmp/f.py", FETCH.encode())
    out("inject_env", sb.exec("env | grep -c sk-ant").stdout.strip())
    out("inject_with", sb.exec(f"python /tmp/f.py {URL}").stdout.strip())

with create_sandbox(IMAGE, ports=[8000], lifetime=600) as sb:
    sb.spawn("python -m http.server 8000")
    time.sleep(3)
    p = sb.port(8000)
    out("port", p)
    out("port_status", urllib.request.urlopen(f"http://127.0.0.1:{p}/", timeout=10).status)

with create_sandbox(IMAGE, limits=Limits(cpus=2, memory="2g"), lifetime=600) as sb:
    out("stats_idle", sb.stats())
    sb.spawn("python -c 'while True: pass'")
    time.sleep(4)
    out("stats_busy", sb.stats())
    base = sb.stats()["memory"]
    sb.exec("head -c 100M /dev/zero > /var/tmp/a")
    out("stats_rootfs_delta_mib", round((sb.stats()["memory"] - base) / 2**20))
    base = sb.stats()["memory"]
    sb.exec("head -c 100M /dev/zero > /tmp/a")
    out("stats_tmp_delta_mib", round((sb.stats()["memory"] - base) / 2**20))

t = time.time(); sb = create_sandbox(IMAGE, lifetime=600); out("cold_s", round(time.time() - t, 2)); sb.stop()
with Pool(2, IMAGE, lifetime=600) as pool:
    while pool.spares < 2:
        time.sleep(0.5)
    t = time.time(); sb = pool.acquire(); out("warm_s", round(time.time() - t, 3)); sb.stop()

out("run_ok", sh('agvisor run python:3.12-slim -- python -c "print(2 + 2)"'))
out("run_fail", sh("agvisor run python:3.12-slim -- 'exit 7'"))
out("run_egress", sh("agvisor run python:3.12-slim --egress pypi.org -- 'pip install -q six && echo installed'"))
