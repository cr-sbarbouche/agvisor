import base64
import codecs
import collections
import io
import math
import os
import re
import secrets
import shlex
import subprocess
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import egress_proxy
from .desktop import GUI_HOME, GUI_IMAGE, GUI_USER, RDP_PORT, make_token
from .limits import Limits

_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}"  # name= and owner=: they end up in --name, --volume and --label
_TAG = r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}"  # snapshot tag: no '.', so a snapshot's volume name splits unambiguously into name.tag
_KEEP = 100_000  # exec(on_output=) keeps only this many trailing characters: the rest already went to on_output
_MARK = "/tmp/.agvisor_active"  # idle_ttl: every exec() touches it; PID 1 exits once it is older than idle_ttl
# gui: RDP checks GNOME Remote Desktop's own credentials file, which the image bakes as gnome:gnome, NOT the unix
# password. Run as GUI_USER with the file on stdin and its path as $1: store ours, wait for the desktop to boot
# (up to 120 s), restart the RDP service (it reads the file only at startup), wait until it listens on 3389 (0D3D).
_RDP_CREDS = f"{GUI_HOME}/.local/share/gnome-remote-desktop/credentials.ini"
_RDP_SETUP = r"""set -e
umask 077
cat > "$1"
export XDG_RUNTIME_DIR=/run/user/$(id -u)
S=gnome-remote-desktop-headless.service
wait_for() { n=0; until "$@"; do n=$((n+1)); [ $n -le 120 ] || { echo "desktop not ready after 120s: $*" >&2; exit 1; }; sleep 1; done; }
wait_for systemctl --user is-active --quiet $S
systemctl --user restart $S
wait_for grep -qE ':0D3D [0-9A-F:]+ 0A' /proc/net/tcp /proc/net/tcp6
"""
_PROXY_IMAGE = "python:3.12-slim"  # egress=: runs egress_proxy.py; any image with python 3.12+ will do
_PROXY_RUNTIME = "runc"  # not gVisor: there, docker's embedded DNS (127.0.0.11) doesn't work, and the proxy couldn't resolve names
_ISOLATED = "agvisor-isolated"  # network="isolated": one docker bridge, shared, on which containers can't reach each other
_ICC = "com.docker.network.bridge.enable_icc"
_GRACE = 600  # seconds a proxy may exist before it must have a sandbox: its sandbox's image may still be downloading
# spawn(): a job lives in _JOBS/<id>/{log,pid,rc}. The rootfs, so `disk` caps what a job logs (/tmp is RAM under gVisor).
# _JOB runs as `sh -c _JOB DIR COMMAND TIMEOUT` (0 = none): GNU timeout starts a process group of its own whose id is its
# pid, so `kill -- -PID` reaches the command and whatever it started; it also writes the exit code when it ends.
_JOBS = "/var/tmp/.agvisor-jobs"
_JOB = 'timeout -k 5 "$2" sh -c "$1" >"$0/log" 2>&1 </dev/null & echo $! >"$0/pid"; wait $!; echo $? >"$0/rc"'
_RUNC = ("runc", "io.containerd.runc.v2")  # --privileged (gui) is only simulated under gVisor, real under these
_PYSESSION = Path(__file__).with_name("_pysession.py").read_text()  # python(): sent along with every call


def _py(arg, max_bytes):
    """_pysession.py as a shell command (`arg`: the call's seconds, or "kill"). Its output is cut to max_bytes in the
    sandbox: it is untrusted, and exec(input=) holds everything it prints in memory."""
    return (f"command -v python3 >/dev/null || {{ echo 'python3 is not in this sandbox image (python:3.12-slim has it)'; exit 127; }}; "
            f"S={shlex.quote(_PYSESSION)}; export S; python3 -c \"$S\" {arg} 2>&1 | tail -c {int(max_bytes)}")


class SandboxGone(RuntimeError):
    """exec() found the container dead or deleted: killed for going over memory/pids, expired (lifetime/idle_ttl), or
    removed. It is not a failed command. Named sandboxes: create_sandbox(name=...) again gives a fresh container on
    the same volume."""


class Job:
    """A command running in the background of a sandbox, from Sandbox.spawn(). It outlives your process, and ends when
    it finishes, is kill()ed, or the sandbox goes (idle_ttl and lifetime too: poll(), read() and wait() count as
    activity for idle_ttl, the job itself does not). `Job(sandbox, id)` picks one up again later."""

    def __init__(self, sandbox, id):
        if not re.fullmatch(r"[0-9a-f]{12}", id):
            raise ValueError(f"invalid job id {id!r}")
        self.sandbox, self.id, self.returncode = sandbox, id, None
        self._d = f"{_JOBS}/{id}"

    def _check(self, wait=0):
        """One exec: wait up to `wait` seconds for the job to end. Its exit code, or None while it runs."""
        d = self._d
        r = self.sandbox.exec(f"[ -d {d} ] || exit 2; for i in $(seq {wait}); do [ -e {d}/rc ] && break; sleep 1; done; "
                              f"cat {d}/rc 2>/dev/null; true", timeout=wait + 5)
        if r.returncode == 2:
            raise LookupError(f"no job {self.id} in {self.sandbox.name}")
        if r.returncode:
            raise RuntimeError(f"checking job {self.id} failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")
        if r.stdout.strip():
            self.returncode = int(r.stdout)
        return self.returncode

    def poll(self):
        """The exit code, or None while it still runs. 143 = killed with TERM, 137 = KILL, 124 = hit its timeout."""
        return self.returncode if self.returncode is not None else self._check()

    def wait(self, timeout=None):
        """Block until it ends and return the exit code; TimeoutError after `timeout` seconds (it keeps running).
        One exec() per ~25 s: cheap over ssh."""
        end = None if timeout is None else time.monotonic() + timeout
        ttl = self.sandbox.idle_ttl
        cap = min(25, ttl - 12) if ttl else 25  # exec(timeout=wait+5) must stay under idle_ttl - 6
        if cap < 1:
            raise ValueError(f"idle_ttl={ttl}s is too short to wait() in")
        while self.returncode is None:
            left = None if end is None else end - time.monotonic()
            if left is not None and left <= 0:
                raise TimeoutError(f"job {self.id} is still running")
            self._check(cap if left is None else max(1, min(cap, math.ceil(left))))
        return self.returncode

    def read(self, offset=0, max_bytes=100_000):
        """Its output (stdout and stderr merged) from byte `offset` on, at most `max_bytes`, as bytes. The next read
        starts at offset + len(result); empty = nothing new yet. A job's log lives in the sandbox's disk, uncapped
        apart from `Limits.disk`."""
        d = self._d
        try:
            data = self.sandbox._b64(f"[ -f {d}/log ] || {{ echo 'no such job' >&2; exit 2; }}; "
                                     f"tail -c +{int(offset) + 1} {d}/log | head -c {int(max_bytes)} | base64 -w0",
                                     int(max_bytes), self.sandbox._short())
        except RuntimeError as e:
            raise LookupError(f"no job {self.id} in {self.sandbox.name}: {e}") from None
        if data is None:
            raise RuntimeError("the sandbox sent more output than asked for")
        return data

    def kill(self, force=False):
        """Stop it and what it started (TERM; `force` = KILL). Not an error if it already ended: wait() gives the code."""
        d = self._d
        r = self.sandbox.exec(f"[ -d {d} ] || exit 2; kill -s {'KILL' if force else 'TERM'} -- -$(cat {d}/pid) 2>/dev/null; true",
                              timeout=self.sandbox._short())
        if r.returncode == 2:
            raise LookupError(f"no job {self.id} in {self.sandbox.name}")


class Sandbox:
    """host=None -> local docker; host="ssh://user@vm" -> docker on a remote VM/server."""

    def __init__(self, image=None, *, gui=False, limits=Limits(), network="bridge", lifetime=None,
                 idle_ttl=None, name=None, owner=None, host=None, runtime="runsc", egress=None):
        self.gui, self.limits, self.network, self.lifetime, self.host = gui, limits, network, lifetime, host
        self.idle_ttl, self.runtime = idle_ttl, runtime
        # strict like `name`: it ends up in --runtime=. And never plain runc for a gui: it is run --privileged
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", runtime) or (gui and runtime in _RUNC):
            raise ValueError(f"invalid runtime {runtime!r}: must be a gVisor (runsc) runtime registered in docker")
        self.image = image or (GUI_IMAGE if gui else "ubuntu:24.04")
        if self.image.startswith("-"):  # `docker run ... IMAGE` would parse it as a flag, e.g. --privileged
            raise ValueError(f"invalid image name: {self.image!r}")
        if lifetime is not None and (gui or lifetime <= 0):
            raise ValueError("lifetime must be > 0 seconds and is only supported for headless sandboxes")
        if idle_ttl is not None and (gui or idle_ttl <= 0):
            raise ValueError("idle_ttl must be > 0 seconds and is only supported for headless sandboxes")
        # strict: `name` ends up in --name and --volume, where a ',' or ':' would inject mount options
        if name is not None and not re.fullmatch(_ID, name):
            raise ValueError(f"invalid name {name!r}: letters, digits, '_', '.', '-' only (max 63)")
        if owner is not None and not re.fullmatch(_ID, owner):
            raise ValueError(f"invalid owner {owner!r}: letters, digits, '_', '.', '-' only (max 63)")
        if egress is not None:
            if gui or network != "bridge":
                raise ValueError("egress= makes its own private network: headless only, and don't pass network=")
            egress = list(egress)
            if not egress:
                raise ValueError("egress is empty: that is network='none'")
            egress_proxy.parse_rules(egress)  # raises ValueError on an entry it can't use
        self.egress, self._has_egress = egress, egress is not None  # _has_egress: also true for handles from list_sandboxes()
        self.owner = owner
        self.created = None  # a datetime, set by list_sandboxes(); handles from create_sandbox() don't know it
        self.persistent = name is not None
        self.name = f"agvisor-{name or uuid.uuid4().hex[:8]}"  # also the name of the data volume
        self.data_dir = (GUI_HOME if gui else "/workspace") if self.persistent else None  # survives stop()
        self.rdp_port = self.password = None  # set by start() when gui; user is GUI_USER
        self._running = False

    def _docker(self, *args, input=None, check=True):
        cmd = ["docker", *(["-H", self.host] if self.host else []), *args]
        r = subprocess.run(cmd, input=input, capture_output=True, text=True)
        if check and r.returncode:
            raise RuntimeError(r.stderr.strip())
        return r

    def _stream(self, *args, on_output):
        """Like _docker, but calls on_output(text) as each chunk arrives. stderr is merged into stdout."""
        cmd = ["docker", *(["-H", self.host] if self.host else []), *args]
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        dec, tail, kept = codecs.getincrementaldecoder("utf-8")("replace"), collections.deque(), 0  # a chunk can end mid-character
        while chunk := os.read(p.stdout.fileno(), 4096):
            if text := dec.decode(chunk):
                tail.append(text)
                kept += len(text)
                while kept - len(tail[0]) >= _KEEP:  # a runaway `yes` must not fill our memory
                    kept -= len(tail.popleft())
                on_output(text)
        p.wait()
        return subprocess.CompletedProcess(cmd, p.returncode, "".join(tail), "")

    def run_args(self):
        l = self.limits
        a = ["run", "-d", f"--runtime={self.runtime}", f"--name={self.name}",
             f"--cpus={l.cpus}", f"--memory={l.memory}", f"--memory-swap={l.memory}",
             f"--pids-limit={l.pids}", f"--network={self._net if self.egress else _ISOLATED if self.network == 'isolated' else self.network}",
             # gVisor caps the rootfs overlay (a file on the host disk): writes past it get ENOSPC. Per-container,
             # no daemon setting needed. `root:` only: `all:` would also overlay /workspace and stop it persisting.
             f"--annotation=dev.gvisor.flag.overlay2=root:self,size={l.disk}"]
        if self.lifetime or self.idle_ttl:  # PID 1 ends on its own: the container exits, then --rm deletes it, even if we crash
            a.append("--rm")
        a.append("--label=agvisor=1")  # what list_sandboxes() finds
        if self.owner:
            a.append(f"--label=agvisor.owner={self.owner}")
        if self.persistent:
            a.append(f"--label=agvisor.name={self.name.removeprefix('agvisor-')}")
        if self.egress:
            a += ["--label=agvisor.egress=1", "--env-file=/dev/stdin"]  # the proxy password rides on stdin, not on a command line
        if self.data_dir:  # a docker volume is a host directory: it is outside gVisor's discarded rootfs overlay
            a.append(f"--volume={self.name}:{self.data_dir}")
            if not self.gui:
                a.append(f"--workdir={self.data_dir}")  # relative paths in exec() land on persistent storage
        if self.gui:  # --privileged only grants simulated caps inside gVisor, not host access
            a += ["--privileged", f"--shm-size={l.shm or '1g'}", f"--publish=127.0.0.1::{RDP_PORT}"]
        elif l.shm:
            a.append(f"--shm-size={l.shm}")
        if not self.gui:  # PID 1 is `sleep`, which never reaps orphans: they stay zombies and eat the pids limit
            a.append("--init")  # (gui: systemd is PID 1 and reaps)
            # an image ENTRYPOINT would swallow our PID 1 command as its arguments. exec() bypasses it anyway, so
            # clearing it loses nothing but the entrypoint's own setup script
            a.append("--entrypoint=")
        return a + [self.image] + self._pid1()

    def _pid1(self):
        if self.gui:
            return []
        if not self.idle_ttl:
            return ["sleep", str(self.lifetime or "infinity")]
        # exits when the marker is older than idle_ttl (checked every 5s); a missing marker counts as expired.
        # `timeout` adds lifetime as a hard cap on top.
        watch = (f"touch {_MARK}; while [ $(( $(date +%s) - $(stat -c %Y {_MARK} 2>/dev/null || echo 0) )) "
                 f"-lt {self.idle_ttl} ]; do sleep 5; done")
        return (["timeout", str(self.lifetime)] if self.lifetime else []) + ["sh", "-c", watch]

    def _left_running(self):
        """A named container an earlier session never stopped: resume it, processes and all."""
        if not self.persistent:
            return False
        r = self._docker("inspect", "-f", "{{.State.Running}}", self.name, check=False)
        return r.stdout.strip() == "true"

    def _claim_volume(self):
        """The data volume of a named sandbox remembers its owner: the first create_sandbox(name=, owner=) labels it,
        and a different owner (or none) is refused. `volume create` on an existing volume returns it unchanged, so two
        racing creators can't both win."""
        self._docker("volume", "create", *([f"--label=agvisor.owner={self.owner}"] if self.owner else []), self.name)
        have = self._docker("volume", "inspect", "-f", '{{index .Labels "agvisor.owner"}}', self.name).stdout.strip()
        if have != (self.owner or ""):
            raise PermissionError(f"sandbox {self.name!r} belongs to another owner")

    @property
    def _net(self):  # '_' after 'agvisor': no sandbox container is called like this, whatever `name=` is
        return f"agvisor_net_{self.name.removeprefix('agvisor-')}"

    @property
    def _proxy(self):
        return f"agvisor_proxy_{self.name.removeprefix('agvisor-')}"

    def _egress_up(self):
        """egress=: a private network with no route out, and on it a proxy that only lets the allowlist through. The
        proxy is the one thing the sandbox can reach, and also on the default bridge. Returns the sandbox's env-file.
        The sandbox gets the proxy's IP: docker's embedded DNS (127.0.0.11) does not work under gVisor."""
        self._gc_egress()
        if self.persistent:  # leftovers of an earlier session of this name
            self._docker("rm", "-f", self._proxy, check=False)
            self._docker("network", "rm", self._net, check=False)
        token = secrets.token_urlsafe(18)  # the proxy password: any other container on the bridge can reach its port
        env = "".join(f"{k}={v}\n" for k, v in [("AGVISOR_ALLOW", ",".join(self.egress)), ("AGVISOR_TOKEN", token)])
        self._docker("run", "-d", f"--runtime={_PROXY_RUNTIME}", f"--name={self._proxy}", "--env-file=/dev/stdin",
                     "--user=65534:65534", "--memory=256m", "--memory-swap=256m", "--cpus=1", "--pids-limit=512",
                     "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
                     f"--label=agvisor.egress={self.name}",
                     _PROXY_IMAGE, "python", "-u", "-c", Path(egress_proxy.__file__).read_text(), input=env)
        self._docker("network", "create", "--internal", "--label=agvisor.egress=1", self._net)
        self._docker("network", "connect", self._net, self._proxy)
        ip = self._docker("inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{self._net}").IPAddress}}}}', self._proxy).stdout.strip()
        url = f"http://agvisor:{token}@{ip}:{egress_proxy.PORT}"
        skip = "localhost,127.0.0.1,::1"
        return "".join(f"{k}={v}\n" for k, v in [("HTTP_PROXY", url), ("HTTPS_PROXY", url), ("http_proxy", url),
                                                 ("https_proxy", url), ("NO_PROXY", skip), ("no_proxy", skip)])

    def _gc_egress(self):
        """A sandbox that expires by itself (--rm) can't remove its proxy and network. So any egress start, and
        prune_egress(), remove the proxies that have no sandbox, then the networks nothing is attached to."""
        rows = [r.split("\t") for r in self._docker(
            "ps", "-a", "--filter", "label=agvisor.egress", "--format",
            '{{.Names}}\t{{.Label "agvisor.egress"}}\t{{.CreatedAt}}').stdout.splitlines()]
        boxes = {name for name, label, _ in rows if label == "1"}  # sandboxes carry "1", a proxy the name of its sandbox
        now = datetime.now(timezone.utc)
        dead = [name for name, label, created in rows
                if label != "1" and label not in boxes and (now - _parse_created(created)).total_seconds() > _GRACE]
        if dead:
            self._docker("rm", "-f", *dead, check=False)
        # `until`: not a network that another start is setting up right now
        self._docker("network", "prune", "-f", "--filter", "label=agvisor.egress", "--filter", "until=60s", check=False)

    def _isolated_up(self):
        """network="isolated": make sure the shared bridge exists, and really has inter-container traffic off. `create`
        on an existing network fails, which is fine: the check below is what matters (a network of that name that
        someone else made, with traffic on, must not pass for an isolated one)."""
        made = self._docker("network", "create", "-o", f"{_ICC}=false", "--label=agvisor.isolated=1", _ISOLATED, check=False)
        icc = self._docker("network", "inspect", "-f", f'{{{{index .Options "{_ICC}"}}}}', _ISOLATED, check=False)
        if icc.returncode:
            raise RuntimeError(made.stderr.strip())
        if icc.stdout.strip() != "false":
            raise RuntimeError(f"docker network {_ISOLATED!r} exists but lets containers talk to each other: remove it (docker network rm {_ISOLATED})")

    def _isolated_dns(self):
        """A user-defined network hands containers docker's embedded DNS (127.0.0.11), which doesn't work under gVisor:
        no name resolves, not even with --dns. So the sandbox gets the resolver config that a container on the default
        bridge gets (the host's real upstream servers), read from a throwaway container on that bridge."""
        r = self._docker("run", "--rm", f"--runtime={self.runtime}", "--network=bridge", "--entrypoint=cat", self.image,
                         "/etc/resolv.conf")
        self._docker("exec", "-i", "-u", "root", self.name, "sh", "-c", "cat > /etc/resolv.conf", input=r.stdout)

    def _egress_down(self):
        self._docker("rm", "-f", self._proxy, check=False)
        self._docker("network", "rm", self._net, check=False)

    def start(self):
        if self._running:  # create_sandbox() already started it, then `with` calls start() again
            return self
        if self.persistent:
            self._claim_volume()  # before anything can re-attach to, or remove, someone else's container
        resumed = self._left_running()  # limits/image args are ignored then: they were fixed at creation
        if not resumed:
            if self.persistent:  # a stopped leftover would block the name; its data lives in the volume
                self._docker("rm", "-f", self.name, check=False)
            if self.network == "isolated":
                self._isolated_up()
            try:
                env = self._egress_up() if self.egress else None
                self._docker(*self.run_args(), input=env)
            except BaseException:
                if self.egress:
                    self._egress_down()
                raise
        self._running = True  # the container exists from here, so stop() must clean up if setup fails
        try:
            if self.network == "isolated" and not resumed:
                self._isolated_dns()
            if self.gui:  # random host port + random password instead of the image's gnome:gnome
                out = self._docker("port", self.name, f"{RDP_PORT}/tcp").stdout
                self.rdp_port = int(out.splitlines()[0].rsplit(":", 1)[1])
                self.password = secrets.token_urlsafe(12)  # urlsafe: no quote that could break the file below
                self._docker("exec", "-i", "-u", "root", self.name, "chpasswd",
                             input=f"{GUI_USER}:{self.password}\n")  # sudo / console login
                creds = f"[RDP]\ncredentials={{'username': <'{GUI_USER}'>, 'password': <'{self.password}'>}}\n"
                self._docker("exec", "-i", "-u", GUI_USER, self.name, "sh", "-c", _RDP_SETUP, "sh", _RDP_CREDS,
                             input=creds)  # RDP login; stdin keeps the password out of `ps`. Returns once RDP listens
        except BaseException:
            if resumed:  # never destroy a container (and its processes) that we merely re-attached to
                self._running = False
            else:
                self.stop()
            raise
        return self

    def exec(self, cmd, *, input=None, timeout=120, on_output=None):
        """Run a shell command inside. Returns CompletedProcess; returncode 124 = timed out.
        `on_output(text)` is called with output as it is produced (stderr merged into stdout, which then holds
        only the last ~100 KB; it must not raise, and can't be combined with `input`).
        That needs GNU `timeout`, i.e. a Debian/Ubuntu-based image. Alpine's busybox returns 143 instead and kills
        only the shell: the children of a compound command (`a; b`) keep running."""
        if self.idle_ttl:
            if timeout + 6 >= self.idle_ttl:  # +5 = the kill grace below, +1 = the watchdog compares whole seconds
                raise ValueError(f"timeout={timeout}s must stay below idle_ttl={self.idle_ttl}s, "
                                 "or the sandbox could expire in the middle of the command")
            # activity = an exec starting or ending (the trap also fires on `exit`)
            cmd = f"trap 'touch {_MARK} 2>/dev/null' EXIT; touch {_MARK} 2>/dev/null; {cmd}"
        argv = ["timeout", "-k", "5", str(timeout), "sh", "-c", cmd]  # kills inside, not just the client
        if on_output:
            if input is not None:
                raise ValueError("on_output can't be combined with input")
            r = self._stream("exec", "-i", self.name, *argv, on_output=on_output)
            msg = r.stdout  # _stream merges stderr into stdout
        else:
            r = self._docker("exec", "-i", self.name, *argv, input=input, check=False)
            msg = r.stderr
        # ponytail: matches message text. "is not running" and "WaitPID" were seen on a real gVisor VM; "Error response
        # from daemon" and "No such container" are docker's standard wording, not re-checked there. The prefix is
        # required for the first two so a command printing "nginx is not running" isn't mistaken for a dead sandbox.
        msg = msg.lower()
        if r.returncode in (1, 128) and ("waitpid" in msg or "error response from daemon" in msg
                                         and ("is not running" in msg or "no such container" in msg)):
            raise SandboxGone(f"{self.name} is gone: {msg.strip()[-300:]}")
        return r

    def _short(self):  # an exec() that only looks at files: well under idle_ttl, whatever it is
        return min(30, self.idle_ttl - 7) if self.idle_ttl else 30

    def spawn(self, cmd, *, timeout=None):
        """Start `sh -c cmd` in the background and return a Job at once, for what outlives a single exec(): a long scan, a
        dev server. `timeout` kills it after that many seconds; the default is to run until it ends, is kill()ed or the
        sandbox goes. exec(timeout=) must stay under idle_ttl, a background job doesn't have to, but nothing keeps the
        sandbox alive for it either: poll() it, or set `lifetime` for a hard budget. Same image requirements as exec()."""
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be > 0 seconds")
        job = Job(self, uuid.uuid4().hex[:12])
        d = shlex.quote(job._d)
        r = self.exec(f"mkdir -p {d} && {{ sh -c {shlex.quote(_JOB)} {d} {shlex.quote(cmd)} {int(timeout or 0)} >/dev/null 2>&1 </dev/null & }}; "
                      f"for i in $(seq 50); do [ -s {d}/pid ] && exit 0; sleep 0.1; done; exit 1", timeout=self._short())
        if r.returncode:
            raise RuntimeError(f"spawn failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")
        return job

    def python(self, code, *, timeout=60, restart=False, max_bytes=30_000):
        """Run Python `code` in a session that keeps variables, imports and functions between calls, like notebook cells:
        one long-lived interpreter inside, so the image needs python3. The value of a last expression is printed.
        Returns a CompletedProcess: stdout is what the code printed, stderr merged, the last `max_bytes` of it. A Python
        exception is a traceback in stdout, not a return code. At `timeout` seconds the code gets a TimeoutError and
        the session survives; stuck where that can't reach it (a C call), the session is killed and returncode is 124.
        127 = no python3 in the image. `restart` kills the session first. The session goes with the container. Same
        limits as exec(): timeout + 5 must stay under idle_ttl, GNU coreutils in the image."""
        if timeout < 1:
            raise ValueError("timeout must be >= 1 second")
        if restart:
            self.exec(_py("kill", max_bytes), timeout=self._short())
        r = self.exec(_py(int(timeout), max_bytes), input=code, timeout=int(timeout) + 5)  # the alarm fires 5 s before this
        if r.returncode == 124:
            self.exec(_py("kill", max_bytes), timeout=self._short())
        return r

    def put(self, path, data: bytes, *, timeout=120):
        """Write `data` to `path` inside: parent directories are created, an existing file is replaced. Goes through
        exec() (base64 on stdin: any bytes survive, a dead sandbox raises SandboxGone). Relative paths start in
        /workspace for a named sandbox. Needs GNU coreutils, like exec(timeout)."""
        q = shlex.quote(path)
        r = self.exec(f'mkdir -p -- "$(dirname -- {q})" && base64 -d > {q}', input=base64.b64encode(data).decode(), timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"put {path!r} failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")

    def put_dir(self, local, remote, *, timeout=120):
        """Copy the local directory `local` into `remote` inside: created if needed, files of the same name replaced,
        others kept. A tar through put(), so the same limits (GNU tar and coreutils in the image). Links are copied as
        links, never followed: a link on your side can't pull a host file into the sandbox. Owners are reset."""
        if not os.path.isdir(local):
            raise NotADirectoryError(local)

        def clean(ti):  # your uid and user name are none of the sandbox's business
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            return ti

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            t.add(local, arcname=".", filter=clean)
        tmp, q = f"/var/tmp/.agvisor-{uuid.uuid4().hex[:12]}.tar", shlex.quote(remote)
        self.put(tmp, buf.getvalue(), timeout=timeout)
        r = self.exec(f"mkdir -p -- {q} && tar -xf {tmp} -C {q} --no-same-owner; rc=$?; rm -f {tmp}; exit $rc", timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"put_dir {remote!r} failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")

    def get_dir(self, remote, local, *, max_bytes=50_000_000, timeout=120):
        """Copy the directory `remote` inside to `local` here: created if needed, files of the same name replaced. The
        sandbox is untrusted, so the tar that comes out is capped at `max_bytes` (ValueError) and extracted with
        tarfile's "data" filter: nothing lands outside `local`, no device files, no setuid bits (tarfile.FilterError
        otherwise). Links that point outside `local` (every absolute one, e.g. a venv's python) are left out, not
        followed or an error: returns their names."""
        tmp, q = f"/var/tmp/.agvisor-{uuid.uuid4().hex[:12]}.tar", shlex.quote(remote)
        r = self.exec(f"[ -d {q} ] || {{ echo 'not a directory' >&2; exit 1; }}; tar -cf {tmp} -C {q} .", timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"get_dir {remote!r} failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
        try:
            data = self.get(tmp, max_bytes=max_bytes, timeout=timeout)
        finally:
            self.exec(f"rm -f {tmp}", timeout=timeout)
        os.makedirs(local, exist_ok=True)
        skipped = []

        def keep(member, dest):
            try:
                return tarfile.data_filter(member, dest)
            except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
                skipped.append(member.name)  # None = don't extract it
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            t.extractall(local, filter=keep)
        return skipped

    def _b64(self, script, max_bytes, timeout):
        """Run `script`, which prints base64, and decode it; None if it is more than max_bytes. It is the sandbox talking:
        a doctored `base64` can't fill our memory. A failing script raises RuntimeError with what it printed (stderr is
        merged into the same stream)."""
        out, size = [], 0
        limit = 4 * (max_bytes // 3 + 2)  # base64 characters of max_bytes+1 bytes; a sandbox that sends more is lying

        def keep(text):
            nonlocal size
            size += len(text)
            if size <= limit:
                out.append(text)

        r = self.exec(script, timeout=timeout, on_output=keep)  # on_output: bounded memory
        if r.returncode:
            raise RuntimeError(f"(exit {r.returncode}): {r.stdout.strip()[-300:]}")
        data = None if size > limit else base64.b64decode("".join(out), validate=True)
        return data if data is not None and len(data) <= max_bytes else None

    def get(self, path, *, max_bytes=10_000_000, timeout=120):
        """Contents of the regular file at `path` inside, as bytes. A bigger file raises ValueError: the sandbox is
        untrusted, so what we read from it is capped (it can't fill our memory, even with a doctored `base64`)."""
        q = shlex.quote(path)
        try:
            data = self._b64(f"[ -f {q} ] || {{ printf 'not a file: %s\\n' {q} >&2; exit 1; }}; head -c {max_bytes + 1} -- {q} | base64 -w0",
                             max_bytes, timeout)
        except RuntimeError as e:
            raise RuntimeError(f"get {path!r} failed {e}") from None
        if data is None:
            raise ValueError(f"{path!r} is larger than max_bytes={max_bytes}")
        return data

    def desktop_token(self, key: bytes, ttl=300):
        """Token for <agvisor-desktop>. Mint it server-side only: it carries the RDP password.
        Replayable until `ttl` runs out."""
        if self.rdp_port is None:
            raise ValueError("start() a gui=True sandbox first")
        return make_token(key, self.rdp_port, self.password, ttl)

    def stop(self):
        """Remove the container and free its CPU/RAM. A named sandbox keeps its data volume."""
        self._docker("rm", "-f", self.name, check=False)
        if self._has_egress:
            self._egress_down()
        self._running = False

    def delete(self):
        """stop() and also erase the persistent data and its snapshots, so create_sandbox(name=...) starts from scratch."""
        self.stop()
        if self.persistent:
            snaps = self._docker("volume", "ls", "-q", "--filter", f"label=agvisor.snap={self.name}", check=False).stdout.split()
            self._docker("volume", "rm", "-f", self.name, *snaps, check=False)

    def _snap_volume(self, tag):
        if not self.persistent:
            raise ValueError("snapshots need a named sandbox: create_sandbox(name=...)")
        if not re.fullmatch(_TAG, tag):
            raise ValueError(f"invalid snapshot tag {tag!r}: letters, digits, '_', '-' only (max 32)")
        return f"agvisor_snap_{self.name.removeprefix('agvisor-')}.{tag}"  # '_' after 'agvisor': no sandbox volume is called that

    def _copy(self, src, dst):
        """Make volume `dst` an exact copy of volume `src`, in a throwaway gVisor container (hostile files never meet a
        host-side cp). Needs sh, rm and cp in the sandbox's image."""
        self._docker("run", "--rm", f"--runtime={self.runtime}", "--network=none", f"--memory={self.limits.memory}",
                     "--user=0", "--entrypoint=sh", f"--volume={src}:/from:ro", f"--volume={dst}:/to", self.image,
                     "-c", "set -e; rm -rf /to/* /to/.[!.]* /to/..?*; cp -a /from/. /to/")

    def snapshot(self, tag):
        """Save a copy of the data dir (/workspace) as `tag`, replacing a snapshot of that name. Named sandboxes only.
        Kept until delete(). Costs as much disk as the data dir, uncapped like it. Stop writing first: a copy taken
        while something writes can be inconsistent."""
        snap = self._snap_volume(tag)
        self._claim_volume()  # also makes sure the volume exists: `docker run -v` would silently create an empty one
        self._docker("volume", "create", f"--label=agvisor.snap={self.name}", snap)
        try:
            self._copy(self.name, snap)
        except BaseException:
            self._docker("volume", "rm", "-f", snap, check=False)  # a half-copied snapshot must not be restorable
            raise

    def list_snapshots(self):
        """Tags of this sandbox's snapshots, sorted. Named sandboxes only; the owner check applies."""
        prefix = self._snap_volume("x").removesuffix("x")  # validates persistence, and gives the volume-name prefix
        self._claim_volume()
        vols = self._docker("volume", "ls", "-q", "--filter", f"label=agvisor.snap={self.name}").stdout.split()
        return sorted(v.removeprefix(prefix) for v in vols)

    def delete_snapshot(self, tag):
        """Remove snapshot `tag` and free its disk. Not an error if it doesn't exist."""
        snap = self._snap_volume(tag)
        self._claim_volume()
        self._docker("volume", "rm", "-f", snap)

    def restore(self, tag):
        """Put the data dir back to snapshot `tag`: whatever is in it now is erased. The container is replaced by a
        fresh one (processes lost), so a bad install is gone from both /workspace and everything else. Returns self.
        If the copy fails the sandbox stays stopped and the data dir is partial: call restore() again, the snapshot is intact."""
        snap = self._snap_volume(tag)
        if self._docker("volume", "inspect", snap, check=False).returncode:  # `run -v` would make an empty one and wipe the data dir with it
            raise ValueError(f"no snapshot {tag!r} of {self.name}")
        self._claim_volume()
        self.stop()
        self._copy(snap, self.name)
        return self.start()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


def _parse_created(created):  # docker's "2026-11-06 09:28:33 +0000 UTC": drop the zone name
    return datetime.strptime(created.rsplit(" ", 1)[0], "%Y-%m-%d %H:%M:%S %z")


def prune_egress(host: str | None = None):
    """Remove the proxies and networks that expired egress= sandboxes left behind (an egress= start does this too)."""
    Sandbox(host=host)._gc_egress()


def list_sandboxes(host: str | None = None, owner: str | None = None) -> list[Sandbox]:
    """Every agvisor sandbox on that docker host, running or not, as handles: exec(), stop() and delete() work.
    Each has `.created` (when its container was made) and `.owner`. `owner=` lists only that owner's.
    Anonymous ones (leaked by a crashed process) are included, so this is also how you find and kill them.
    Don't start() a stopped handle: that makes a fresh default-image container."""
    fmt = "\t".join(["{{.Names}}", "{{.State}}", '{{.Label "agvisor.name"}}', '{{.Label "agvisor.owner"}}', "{{.CreatedAt}}",
                     '{{.Label "agvisor.egress"}}'])
    out = Sandbox(host=host)._docker("ps", "-a", "--filter", "label=agvisor",
                                     *(["--filter", f"label=agvisor.owner={owner}"] if owner else []),
                                     "--format", fmt).stdout
    boxes = []
    for line in out.splitlines():
        cname, state, label, own, created, egress = line.split("\t")  # tabs: CreatedAt has spaces, a label can be empty
        sb = Sandbox(name=label or None, owner=own or None, host=host)
        if not label:
            sb.name = cname  # anonymous: its random container name is all there is
        sb._running, sb._has_egress = state == "running", bool(egress)  # a proxy and a network to remove too
        sb.created = _parse_created(created)
        boxes.append(sb)
    return boxes


def create_sandbox(
    image: str | None = None,
    *,
    gui: bool = False,
    limits: Limits = Limits(),
    network: str = "bridge",
    lifetime: int | None = None,
    idle_ttl: int | None = None,
    name: str | None = None,
    owner: str | None = None,
    host: str | None = None,
    runtime: str = "runsc",
    egress: list[str] | None = None,
) -> Sandbox:
    """Create and start a gVisor sandbox. Use `with create_sandbox(...) as sb:` or call sb.stop() yourself.

    image     Image to run. Default: ubuntu:24.04, or the GNOME desktop image when gui=True.
    gui       True -> GNOME desktop over RDP (sb.rdp_port, sb.password, sb.desktop_token()). start() returns once the
              desktop has booted and RDP listens (~30-60 s).
    limits    Limits(cpus, memory, pids, shm, disk). Default: 2 cores, 2g RAM, 512 processes, 5g root disk.
              Reusable across sandboxes. `disk` doesn't cover the /workspace volume of a named sandbox.
    name      Makes the sandbox resumable. Files in sb.data_dir (/workspace; the gnome home when gui) are kept
              in a docker volume on the host machine and survive stop(), `with` exit and lifetime expiry.
              create_sandbox(name=same) later gets them back: it re-attaches to the container if an earlier
              session left it running (processes and installed packages intact), else makes a fresh one on the
              same volume. sb.delete() erases the data. Default: anonymous, nothing is kept.
              Without owner= there is NO ownership check: anyone who passes the same name gets that sandbox.
              The volume has no size cap either (limits.disk covers the rest).
    owner     A label such as a tenant id (same characters as name). A named sandbox's volume is claimed by the
              first owner that creates it, and create_sandbox(name=same, owner=other) (or no owner) raises
              PermissionError instead of re-attaching, restarting or deleting it. list_sandboxes(owner=) lists
              only theirs. It is a guard against mixing up names, not authentication: whoever can run docker on
              the host, or build a Sandbox by hand and call delete(), still gets past it.

    network  "bridge" (internet access, default), "isolated" (internet access, but sandboxes can't connect to each
              other: they share one docker bridge, agvisor-isolated, with inter-container traffic off; it still
              reaches the host and the internet like "bridge", and names resolve), "none" (headless only: gui needs its RDP port) or the
              name of a docker network you created. A host firewall that drops traffic from docker0 blocks the gateway
              from reaching a gui sandbox there: use a network of its own.
    egress   Outbound allowlist, e.g. ["pypi.org", "files.pythonhosted.org", "10.0.0.0/24:8080"]. The sandbox gets a
              private network with no route out, and its only way out is an HTTP(S) proxy (HTTP(S)_PROXY is set) that
              lets those hosts through: `host` or `*.host` (ports 80 and 443 unless `host:port` or `host:*`), an IPv4
              address or a network. A name must resolve to public addresses; reaching a private one takes an IP entry.
              The proxy runs python:3.12-slim (must be on the docker host) under docker's default runtime, runc: under
              gVisor docker's DNS doesn't work. Don't pass network=. Headless only; fixed at creation, like limits.
    runtime  The docker runtime, default "runsc". Name a second one (e.g. "runsc-gui", same runsc plus the GUI
              flags) to keep the strict one for headless sandboxes. Must be a gVisor runtime: plain runc is
              refused for gui, which runs --privileged.
    lifetime  Seconds until the sandbox destroys itself, even if your process dies. Headless only.
              Default: lives until stop().
    idle_ttl  Seconds without an exec() before the sandbox destroys itself, even if your process dies. The clock
              restarts when each exec() starts and ends; background processes don't count. exec(timeout=) must be
              shorter. A named sandbox keeps its files, and create_sandbox(name=same) later makes a fresh
              container. Combine with lifetime for a hard cap. Headless only. Fixed at creation.
    host     Which machine supplies the resources. None -> docker on this machine (or $DOCKER_HOST).
              "ssh://user@host" -> docker on a remote VM/server, e.g. an EC2 or Azure VM.
    """
    return Sandbox(image, gui=gui, limits=limits, network=network, lifetime=lifetime, idle_ttl=idle_ttl,
                   name=name, owner=owner, host=host, runtime=runtime, egress=egress).start()
