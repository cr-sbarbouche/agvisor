---
name: agvisor
description: Run untrusted or risky code in a gVisor sandbox instead of on the host: unknown scripts, `pip install` of unvetted packages, anything that needs CPU/memory limits. Use when the user asks to sandbox, isolate, or safely run code.
---

# agvisor

gVisor (`runsc`) sandboxes with CPU/memory/pids/disk limits, driven through the `docker` CLI. Alpha, not security-audited: tell the user that before running code that is actively hostile.

## With the `agvisor` MCP tools (`run`, `reset`)

- Use `run` for every command that executes code you did not write or cannot vouch for. If it fails, tell the user. Don't fall back to the host shell.
- One sandbox lives between calls (files, installed packages) until it has been idle ~10 minutes. Then `run` reports "sandbox was reset" and the files are gone: set up again.
- It can't see the user's files. Put code in with a heredoc (`cat > /tmp/a.py <<'EOF'`; there is no `/workspace` here), print what you need back.
- Network is off unless the user set `AGVISOR_NETWORK`. If `pip install` fails for that reason, ask them. Don't work around it.
- Going over memory or the process limit kills the whole sandbox, not one process. Expect "sandbox was reset".

## Without them: the Python library

Needs a Linux host with docker + gVisor (see "Host setup" in the README). Docker on another machine: `DOCKER_HOST=ssh://user@vm`.

```python
# pip install agvisor
from agvisor import Limits, SandboxGone
from agvisor.sandbox import create_sandbox

with create_sandbox(limits=Limits(cpus=1, memory="512m"), network="none", lifetime=3600) as sb:
    r = sb.exec("python3 -c 'print(1+1)'", timeout=30)   # r.returncode, r.stdout; 124 = timed out
```

- Each shell call is a new process: to keep a sandbox across calls use `name="x"` (files in `/workspace` survive; call `create_sandbox(name="x")` again to resume) and `idle_ttl=600` so it cleans itself up. `SandboxGone` means it was killed or expired: call `create_sandbox(name=...)` again.
- Use Debian/Ubuntu images (`ubuntu:24.04`, `python:3.12-slim`): timeouts need GNU `timeout`. Never set an `ENTRYPOINT`.
- Keep `pids` at the default 512: gVisor counts host threads, and 64-128 made fork storms kill the sandbox.
- `network="none"` for untrusted code. On a cloud VM, `"bridge"` can reach the metadata endpoint and steal the VM's credentials.
- The `/workspace` volume of a named sandbox has no size cap.
