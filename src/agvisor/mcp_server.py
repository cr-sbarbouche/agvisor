"""MCP server: a coding agent runs shell commands, edits files and runs Python in a gVisor sandbox instead of on the host.

Configured by environment variables only, so the agent can't pick its own image, network or limits:
AGVISOR_HOST (ssh://user@vm; default local docker / $DOCKER_HOST), AGVISOR_IMAGE, AGVISOR_NETWORK (default "none"),
AGVISOR_CPUS (2), AGVISOR_MEMORY (2g), AGVISOR_IDLE_TTL (600 s), AGVISOR_LIFETIME (3600 s).
AGVISOR_EGRESS (unset): comma-separated hosts the sandbox may reach (create_sandbox(egress=)), e.g.
"pypi.org,files.pythonhosted.org"; it replaces AGVISOR_NETWORK.
AGVISOR_NAME (unset) makes the sandbox a named one: /workspace is kept in a docker volume, and the snapshot/restore
tools exist."""
import atexit
import os
import shlex
import threading
from pathlib import Path

from .limits import Limits
from .sandbox import Job, SandboxGone, create_sandbox

_MAX = 30_000  # characters of output handed back to the agent (the tail)
_READ = 100_000  # read_file(): bytes of one file
_EDIT = 1_000_000  # edit_file(): bytes of one file
_PYSESSION = Path(__file__).with_name("_pysession.py").read_text()  # sent along with every python() call
_CHECKPOINT = "checkpoint"  # ponytail: one snapshot slot, so an agent can't pile up full copies of /workspace
_sb = None  # One sandbox per server process. Anonymous unless AGVISOR_NAME is set: then /workspace is a volume with
_lock = threading.Lock()  # no size cap, shared by every server process using that name.


def _env(key, default=None):
    return os.environ.get(f"AGVISOR_{key}") or default


def _t(sb, seconds):  # exec() refuses a timeout that could outlive the idle watchdog: keep it 7 s under
    return max(1, min(seconds, sb.idle_ttl - 7))


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
        r = sb.exec(command, timeout=_t(sb, timeout), on_output=lambda _: None)  # streaming bounds memory
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
        sb = _get()
        sb.put(path, content.encode(), timeout=_t(sb, 120))
    except SandboxGone as e:
        return _gone(e)
    return f"wrote {len(content.encode())} bytes to {path}"


def read_file(path: str) -> str:
    """Read a text file inside the sandbox (up to 100 KB; for more, use run() with head, tail or grep). Use an
    absolute path, or one relative to /workspace when the sandbox keeps /workspace."""
    try:
        sb = _get()
        data = sb.get(path, max_bytes=_READ, timeout=_t(sb, 120))
    except SandboxGone as e:
        return _gone(e)
    except RuntimeError as e:
        return f"cannot read {path}: {e}"
    except ValueError:
        return f"{path} is bigger than {_READ} bytes: use run() with head, tail or grep"
    return f"{path} is binary ({len(data)} bytes)" if b"\0" in data else data.decode(errors="replace")


def edit_file(path: str, old: str, new: str) -> str:
    """Replace one exact piece of text in a UTF-8 file inside the sandbox. Cheaper and safer than sending the whole
    file again with write_file(). `old` must appear exactly once: include the lines around it to make it unique. Use
    an absolute path, or one relative to /workspace when the sandbox keeps /workspace."""
    if not old:
        return "old must not be empty: use write_file() to create a file"
    try:
        sb = _get()
        text = sb.get(path, max_bytes=_EDIT, timeout=_t(sb, 120)).decode()
        n = text.count(old)
        if n != 1:
            return f"old text not found in {path}" if n == 0 else f"old text appears {n} times in {path}: add the lines around it"
        sb.put(path, text.replace(old, new).encode(), timeout=_t(sb, 120))
    except SandboxGone as e:
        return _gone(e)
    except RuntimeError as e:
        return f"cannot edit {path}: {e}"
    except UnicodeDecodeError:  # first: it is a ValueError
        return f"{path} is not UTF-8 text"
    except ValueError:
        return f"{path} is bigger than {_EDIT} bytes: edit it with run()"
    return f"edited {path}"


def _py(arg):  # the in-sandbox session script (_pysession.py) as a shell command; its output stays under _MAX bytes
    return (f"command -v python3 >/dev/null || {{ echo 'python3 is not in this sandbox image: ask the user to set "
            f"AGVISOR_IMAGE (python:3.12-slim has it)'; exit 127; }}; S={shlex.quote(_PYSESSION)}; export S; "
            f"python3 -c \"$S\" {arg} 2>&1 | tail -c {_MAX}")


def python(code: str, timeout: int = 60, restart: bool = False) -> str:
    """Run Python in the sandbox in a session that keeps variables, imports and functions between calls, like notebook
    cells: load data once, then explore it with small calls. The value of the last expression is printed. Output is
    stdout and stderr merged (the last ~30 KB). At `timeout` seconds the call raises TimeoutError and the session
    survives; `restart=True` starts a fresh session first. The session is lost when the sandbox is reset or
    deleted. Needs python3 in the sandbox image."""
    try:
        sb = _get()
        if restart:
            sb.exec(_py("kill"), timeout=_t(sb, 30))
        wait = _t(sb, timeout + 5)  # the alarm in the session fires 5 s before exec() gives up
        r = sb.exec(_py(max(1, wait - 5)), input=code, timeout=wait)
        if r.returncode == 124:  # the code is stuck where the alarm can't reach it (a C call): only a restart frees us
            sb.exec(_py("kill"), timeout=_t(sb, 30))
            return f"timed out after {timeout} s and the python session was killed: its variables are gone"
    except SandboxGone as e:
        return _gone(e)
    return (r.stdout or "(no output)") + (f"\n[exit {r.returncode}]" if r.returncode else "")


def list_files(path: str = ".") -> str:
    """List a directory inside the sandbox: permissions, size, date, name (a trailing / marks a directory). Hidden files
    included. A relative path starts in /workspace when the sandbox keeps /workspace, else in /."""
    try:
        sb = _get()
        r = sb.exec(f"ls -lAp --time-style=long-iso -- {shlex.quote(path)}", timeout=_t(sb, 30), on_output=lambda _: None)
    except SandboxGone as e:
        return _gone(e)
    return f"{r.stdout[-_MAX:]}\n[exit {r.returncode}]"


def start_job(command: str, timeout: int | None = None) -> str:
    """Start a shell command in the background of the sandbox and return its job id at once: for what takes longer than
    run() allows (a long scan, a build, a server). Read its output with job_output(). `timeout` (seconds) kills it;
    default: runs until it ends or you kill_job() it. The sandbox is deleted after being idle for a while, and a
    running job goes with it: calls to job_output() count as activity, the job itself does not."""
    try:
        return _get().spawn(command, timeout=timeout).id
    except SandboxGone as e:
        return _gone(e)


def job_output(job_id: str, offset: int = 0) -> str:
    """New output of a background job (stdout and stderr merged) from byte `offset` on, then its state: `[running; next
    offset N]` or `[exit CODE; next offset N]` (124 = hit its timeout, 143 = killed). Pass the next offset back to get
    only what is new. When the state is exit and the output was cut at ~30 KB, call again with the next offset."""
    try:
        job = Job(_get(), job_id)
        rc = job.poll()  # first: once it has ended, the log is complete
        data = job.read(offset, _MAX)
    except SandboxGone as e:
        return _gone(e)
    except (LookupError, ValueError) as e:
        return f"{e}. Jobs are lost when the sandbox is reset."
    return f"{data.decode(errors='replace')}\n[{'running' if rc is None else f'exit {rc}'}; next offset {offset + len(data)}]"


def kill_job(job_id: str) -> str:
    """Stop a background job and what it started. Look at job_output() for how it ended."""
    try:
        Job(_get(), job_id).kill()
    except SandboxGone as e:
        return _gone(e)
    except (LookupError, ValueError) as e:
        return f"{e}. Jobs are lost when the sandbox is reset."
    return f"sent TERM to job {job_id}"


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
    app.tool()(read_file)
    app.tool()(edit_file)
    app.tool()(python)
    app.tool()(list_files)
    app.tool()(start_job)
    app.tool()(job_output)
    app.tool()(kill_job)
    app.tool()(reset)
    if _env("NAME"):  # snapshots need a named sandbox
        app.tool()(snapshot)
        app.tool()(restore)
    atexit.register(reset)  # ponytail: skipped on SIGKILL; idle_ttl/lifetime delete the container anyway
    app.run()  # stdio
