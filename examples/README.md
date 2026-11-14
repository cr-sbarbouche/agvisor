# Examples

Each script is self-contained and prints what it did. They need a docker host with gVisor registered as the `runsc` runtime
(see [Host setup](../README.md#host-setup-linux-vm-or-server)) and `uv`:

```sh
uv run python examples/01_quickstart.py
AGVISOR_HOST=ssh://user@my-vm uv run python examples/01_quickstart.py   # docker on another machine
```

| script | shows |
|---|---|
| [`01_quickstart.py`](01_quickstart.py) | `exec` with stdin and a timeout, a Python session that stays up, `lifetime` |
| [`02_resume_and_snapshot.py`](02_resume_and_snapshot.py) | a named sandbox keeps `/workspace` between sessions; `snapshot()` and `restore()` |
| [`03_limits.py`](03_limits.py) | `Limits`: a full disk is an error, going over memory kills the sandbox (`SandboxGone`) |
| [`04_files_and_jobs.py`](04_files_and_jobs.py) | `put`/`get`, `put_dir`/`get_dir`, a background job you can watch (`spawn`) |
| [`05_egress_allowlist.py`](05_egress_allowlist.py) | `egress=`: `pip` works through the allowlist, everything else is refused |
| [`06_isolated_network.py`](06_isolated_network.py) | `network="isolated"`: sandboxes can't connect to each other, the internet still works |
| [`07_workspace_cap.py`](07_workspace_cap.py) | `Limits(workspace=...)`: a size cap on a named sandbox's `/workspace` (needs xfs with `pquota` on the host) |

CI runs every script against real gVisor (the `gvisor` job), so they are not allowed to rot.
To give a coding agent a sandbox instead of calling the library yourself, see [Coding agents](../README.md#coding-agents-skill--mcp-server).
