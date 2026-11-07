"""MCP server: a coding agent runs shell commands in a gVisor sandbox instead of on the host.

Configured by environment variables only, so the agent can't pick its own image, network or limits:
AGVISOR_HOST (ssh://user@vm; default local docker / $DOCKER_HOST), AGVISOR_IMAGE, AGVISOR_NETWORK (default "none"),
AGVISOR_CPUS (2), AGVISOR_MEMORY (2g), AGVISOR_IDLE_TTL (600 s), AGVISOR_LIFETIME (3600 s)."""
import atexit
import os
import threading

from .limits import Limits
from .sandbox import SandboxGone, create_sandbox

_MAX = 30_000  # characters of output handed back to the agent (the tail)
_sb = None  # ponytail: one anonymous sandbox per server process. No name=, so no /workspace volume (it has no size cap)
_lock = threading.Lock()  # and nothing is shared between projects. Upgrade path: a named sandbox per workspace.


def _env(key, default=None):
    return os.environ.get(f"AGVISOR_{key}") or default


def _get():
    global _sb
    with _lock:
        if _sb is None:
            _sb = create_sandbox(
                _env("IMAGE"), host=_env("HOST"), network=_env("NETWORK", "none"),
                limits=Limits(cpus=float(_env("CPUS", 2)), memory=_env("MEMORY", "2g")),
                idle_ttl=int(_env("IDLE_TTL", 600)), lifetime=int(_env("LIFETIME", 3600)))
        return _sb


def run(command: str, timeout: int = 120) -> str:
    """Run a shell command (`sh -c`) in an isolated gVisor sandbox. Use this, not the host shell, for code you did not
    write or cannot vouch for. Returns stdout and stderr merged (the last ~30 KB) and the exit code; 124 = timed out.

    The same sandbox is reused between calls (files, installed packages) until it has been idle for a while; after
    that a call reports "sandbox was reset" and the files are gone. It cannot see the user's files: write code in with
    a heredoc (`cat > a.py <<'EOF'`) and print what you need back. Network access is whatever the user configured
    (off by default), so `pip install` may fail by design."""
    try:
        sb = _get()
        # exec() refuses a timeout that could outlive the idle watchdog: keep it 7 s under
        r = sb.exec(command, timeout=max(1, min(timeout, sb.idle_ttl - 7)), on_output=lambda _: None)  # streaming bounds memory
    except SandboxGone as e:
        reset()  # no silent retry: the command may have half-run, and the agent should know its files are gone
        return f"sandbox was reset ({e}). Its files are gone; set up again and re-run."
    return f"{r.stdout[-_MAX:]}\n[exit {r.returncode}]"


def reset() -> str:
    """Destroy the sandbox and everything in it. The next run() starts a fresh one."""
    global _sb
    with _lock:
        sb, _sb = _sb, None
    if sb:
        sb.stop()
    return "sandbox destroyed; the next run() starts a fresh one"


def main():
    try:
        from mcp.server.mcpserver import MCPServer  # mcp 2.x: v1's FastMCP was renamed
    except ImportError:
        raise SystemExit("agvisor-mcp needs the mcp extra (mcp>=2): pip install 'agvisor[mcp]'")
    app = MCPServer("agvisor")
    app.tool()(run)
    app.tool()(reset)
    atexit.register(reset)  # ponytail: skipped on SIGKILL; idle_ttl/lifetime delete the container anyway
    app.run()  # stdio
