"""MCP server: a coding agent runs shell commands in a gVisor sandbox instead of on the host.

Configured by environment variables only, so the agent can't pick its own image, network or limits:
AGVISOR_HOST (ssh://user@vm; default local docker / $DOCKER_HOST), AGVISOR_IMAGE, AGVISOR_NETWORK (default "none"),
AGVISOR_CPUS (2), AGVISOR_MEMORY (2g), AGVISOR_IDLE_TTL (600 s), AGVISOR_LIFETIME (3600 s).
AGVISOR_EGRESS (unset): comma-separated hosts the sandbox may reach (create_sandbox(egress=)), e.g.
"pypi.org,files.pythonhosted.org"; it replaces AGVISOR_NETWORK.
AGVISOR_NAME (unset) makes the sandbox a named one: /workspace is kept in a docker volume, and the snapshot/restore
tools exist."""
import atexit
import os
import threading

from .limits import Limits
from .sandbox import SandboxGone, create_sandbox

_MAX = 30_000  # characters of output handed back to the agent (the tail)
_CHECKPOINT = "checkpoint"  # ponytail: one snapshot slot, so an agent can't pile up full copies of /workspace
_sb = None  # One sandbox per server process. Anonymous unless AGVISOR_NAME is set: then /workspace is a volume with
_lock = threading.Lock()  # no size cap, shared by every server process using that name.


def _env(key, default=None):
    return os.environ.get(f"AGVISOR_{key}") or default


def _get():
    global _sb
    with _lock:
        if _sb is None:
            egress = [e.strip() for e in (_env("EGRESS") or "").split(",") if e.strip()]
            _sb = create_sandbox(
                _env("IMAGE"), host=_env("HOST"), name=_env("NAME"),
                **({"egress": egress} if egress else {"network": _env("NETWORK", "none")}),
                limits=Limits(cpus=float(_env("CPUS", 2)), memory=_env("MEMORY", "2g")),
                idle_ttl=int(_env("IDLE_TTL", 600)), lifetime=int(_env("LIFETIME", 3600)))
        return _sb


def run(command: str, timeout: int = 120) -> str:
    """Run a shell command (`sh -c`) in an isolated gVisor sandbox. Use this, not the host shell, for code you did not
    write or cannot vouch for. Returns stdout and stderr merged (the last ~30 KB) and the exit code; 124 = timed out.

    The same sandbox is reused between calls (files, installed packages) until it has been idle for a while; after
    that a call reports "sandbox was reset" and the files are gone (with snapshot/restore tools, only /workspace
    is kept). It cannot see the user's files: write code in with
    write_file and print what you need back. Network access is whatever the user configured
    (off by default, or only a few hosts), so `pip install` may fail by design."""
    try:
        sb = _get()
        # exec() refuses a timeout that could outlive the idle watchdog: keep it 7 s under
        r = sb.exec(command, timeout=max(1, min(timeout, sb.idle_ttl - 7)), on_output=lambda _: None)  # streaming bounds memory
    except SandboxGone as e:
        return _gone(e)
    return f"{r.stdout[-_MAX:]}\n[exit {r.returncode}]"


def _gone(e):
    reset()  # no silent retry: the command may have half-run, and the agent should know what is gone
    kept = "Only /workspace is kept" if _env("NAME") else "Its files are gone"
    return f"sandbox was reset ({e}). {kept}; set up again and retry."


def write_file(path: str, content: str) -> str:
    """Write a text file (UTF-8) inside the sandbox: parent directories are created, an existing file is replaced.
    Use it instead of a heredoc, which breaks on quoting and on very long content. Use an absolute path
    (/tmp/a.py, or /workspace/a.py when the sandbox keeps /workspace)."""
    try:
        _get().put(path, content.encode())
    except SandboxGone as e:
        return _gone(e)
    return f"wrote {len(content.encode())} bytes to {path}"


def reset() -> str:
    """Destroy the sandbox and everything in it. The next run() starts a fresh one. With snapshot/restore tools,
    /workspace survives: use restore() to roll that back."""
    global _sb
    with _lock:
        sb, _sb = _sb, None
    if sb:
        sb.stop()
    return "sandbox destroyed; the next run() starts a fresh one"


def snapshot() -> str:
    """Save /workspace as the one checkpoint, replacing the previous one. Costs as much disk as /workspace. Don't
    leave background processes writing to it: a copy taken during writes can be inconsistent."""
    _get().snapshot(_CHECKPOINT)
    return "checkpoint saved"


def restore() -> str:
    """Put /workspace back to the checkpoint: everything in it now is erased, and the sandbox restarts (running
    processes and anything outside /workspace are lost). Fails if there is no checkpoint yet."""
    _get().restore(_CHECKPOINT)
    return "restored /workspace to the checkpoint; the sandbox was restarted"


def main():
    try:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x: v1's FastMCP was renamed
    except ImportError:
        raise SystemExit("agvisor-mcp needs the mcp extra (mcp>=2): pip install 'agvisor[mcp]'")
    app = MCPServer("agvisor")
    app.tool()(run)
    app.tool()(write_file)
    app.tool()(reset)
    if _env("NAME"):  # snapshots need a named sandbox
        app.tool()(snapshot)
        app.tool()(restore)
    atexit.register(reset)  # ponytail: skipped on SIGKILL; idle_ttl/lifetime delete the container anyway
    app.run()  # stdio
