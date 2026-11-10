"""agvisor ls | rm | exec | put | get: look at, clean up, poke and feed the sandboxes on a docker host."""
import argparse
import shlex
import sys
import tarfile
from pathlib import Path
from datetime import datetime, timezone

from .sandbox import SandboxGone, list_sandboxes


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", help="docker host, e.g. ssh://user@vm (default: local docker / $DOCKER_HOST)")
    common.add_argument("--owner", help="only this owner's sandboxes")
    p = argparse.ArgumentParser(prog="agvisor", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ls", parents=[common], help="list sandboxes: name, state, owner, created")
    rm = sub.add_parser("rm", parents=[common], help="remove sandboxes by name and/or age; a named one keeps its files")
    rm.add_argument("names", nargs="*", help="as printed by `ls`")
    rm.add_argument("--older-than", type=int, metavar="SECONDS", help="only those created longer ago than this")
    rm.add_argument("--data", action="store_true", help="also erase the files and snapshots of named sandboxes")
    ex = sub.add_parser("exec", parents=[common], help="run a shell command in a running sandbox")
    ex.add_argument("name")
    ex.add_argument("command", nargs="+", help="joined with spaces and run by `sh -c`")
    ex.add_argument("--timeout", type=int, default=120)
    put = sub.add_parser("put", parents=[common], help="copy a local file or directory into a running sandbox")
    put.add_argument("name")
    put.add_argument("local")
    put.add_argument("remote", help="the file's new path (or the directory to copy into) inside")
    get = sub.add_parser("get", parents=[common], help="copy a file or directory out of a running sandbox")
    get.add_argument("name")
    get.add_argument("remote")
    get.add_argument("local", help="a file or a directory here (replaced / merged into)")
    get.add_argument("--max-bytes", type=int, help="refuse more than this (default 10 MB for a file, 50 MB for a directory)")
    a = p.parse_args(argv)

    boxes = list_sandboxes(a.host, owner=a.owner)
    if a.cmd == "ls":
        for sb in boxes:
            print(sb.name, "running" if sb._running else "stopped", sb.owner or "-", sb.created.isoformat(), sep="\t")
        return 0
    missing = [n for n in (a.names if a.cmd == "rm" else [a.name]) if n not in {sb.name for sb in boxes}]
    if missing:
        p.error(f"no such sandbox: {', '.join(missing)}")
    if a.cmd == "rm":
        if not a.names and a.older_than is None:
            p.error("give names and/or --older-than")  # never a bare "remove everything"
        now = datetime.now(timezone.utc)
        for sb in boxes:
            if (not a.names or sb.name in a.names) and (a.older_than is None or (now - sb.created).total_seconds() > a.older_than):
                sb.delete() if a.data else sb.stop()
                print("removed", sb.name)
        return 0
    sb = next(sb for sb in boxes if sb.name == a.name)
    if not sb._running:
        p.error(f"{sb.name} is not running")
    kw = {"max_bytes": a.max_bytes} if a.cmd == "get" and a.max_bytes else {}
    try:  # ponytail: a handle from list_sandboxes() doesn't know idle_ttl, so none of these count as activity
        if a.cmd == "put":
            if Path(a.local).is_dir():
                sb.put_dir(a.local, a.remote)
            else:
                sb.put(a.remote, Path(a.local).read_bytes())
        elif a.cmd == "get":
            if sb.exec(f"[ -d {shlex.quote(a.remote)} ]").returncode == 0:
                skipped = sb.get_dir(a.remote, a.local, **kw)
                if skipped:
                    print(f"left out {len(skipped)} link(s) that point outside: {', '.join(skipped[:5])}", file=sys.stderr)
            else:
                Path(a.local).write_bytes(sb.get(a.remote, **kw))
        else:
            return sb.exec(" ".join(a.command), timeout=a.timeout, on_output=lambda t: print(t, end="", flush=True)).returncode
    except (SandboxGone, RuntimeError, ValueError, OSError, tarfile.TarError) as e:  # OSError: a local path
        p.error(str(e))
    print("done")
    return 0
