import codecs
import collections
import os
import re
import secrets
import subprocess
import uuid

from .desktop import GUI_HOME, GUI_IMAGE, GUI_USER, RDP_PORT, make_token
from .limits import Limits

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
_RUNC = ("runc", "io.containerd.runc.v2")  # --privileged (gui) is only simulated under gVisor, real under these


class Sandbox:
    """host=None -> local docker; host="ssh://user@vm" -> docker on a remote VM/server."""

    def __init__(self, image=None, *, gui=False, limits=Limits(), network="bridge", lifetime=None,
                 idle_ttl=None, name=None, host=None, runtime="runsc"):
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
        if name is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            raise ValueError(f"invalid name {name!r}: letters, digits, '_', '.', '-' only (max 63)")
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
             f"--pids-limit={l.pids}", f"--network={self.network}",
             # gVisor caps the rootfs overlay (a file on the host disk): writes past it get ENOSPC. Per-container,
             # no daemon setting needed. `root:` only: `all:` would also overlay /workspace and stop it persisting.
             f"--annotation=dev.gvisor.flag.overlay2=root:self,size={l.disk}"]
        if self.lifetime or self.idle_ttl:  # PID 1 ends on its own: the container exits, then --rm deletes it, even if we crash
            a.append("--rm")
        a.append("--label=agvisor=1")  # what list_sandboxes() finds
        if self.persistent:
            a.append(f"--label=agvisor.name={self.name.removeprefix('agvisor-')}")
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

    def start(self):
        if self._running:  # create_sandbox() already started it, then `with` calls start() again
            return self
        resumed = self._left_running()  # limits/image args are ignored then: they were fixed at creation
        if not resumed:
            if self.persistent:  # a stopped leftover would block the name; its data lives in the volume
                self._docker("rm", "-f", self.name, check=False)
            self._docker(*self.run_args())
        self._running = True  # the container exists from here, so stop() must clean up if setup fails
        try:
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
            return self._stream("exec", "-i", self.name, *argv, on_output=on_output)
        return self._docker("exec", "-i", self.name, *argv, input=input, check=False)

    def desktop_token(self, key: bytes, ttl=300):
        """Token for <agvisor-desktop>. Mint it server-side only: it carries the RDP password.
        Replayable until `ttl` runs out."""
        if self.rdp_port is None:
            raise ValueError("start() a gui=True sandbox first")
        return make_token(key, self.rdp_port, self.password, ttl)

    def stop(self):
        """Remove the container and free its CPU/RAM. A named sandbox keeps its data volume."""
        self._docker("rm", "-f", self.name, check=False)
        self._running = False

    def delete(self):
        """stop() and also erase the persistent data, so create_sandbox(name=...) starts from scratch."""
        self.stop()
        if self.persistent:
            self._docker("volume", "rm", "-f", self.name, check=False)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()


def list_sandboxes(host: str | None = None) -> list[Sandbox]:
    """Every agvisor sandbox on that docker host, running or not, as handles: exec(), stop() and delete() work.
    Anonymous ones (leaked by a crashed process) are included, so this is also how you find and kill them.
    Don't start() a stopped handle: that makes a fresh default-image container."""
    out = Sandbox(host=host)._docker("ps", "-a", "--filter", "label=agvisor",
                                     "--format", '{{.Names}} {{.State}} {{.Label "agvisor.name"}}').stdout
    boxes = []
    for line in out.splitlines():
        cname, state, *label = line.split()  # an empty label leaves no third field
        sb = Sandbox(name=label[0] if label else None, host=host)
        if not label:
            sb.name = cname  # anonymous: its random container name is all there is
        sb._running = state == "running"
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
    host: str | None = None,
    runtime: str = "runsc",
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
              There is NO ownership check: anyone who passes the same name gets that sandbox, so namespace
              names per tenant yourself. The volume has no size cap either (limits.disk covers the rest).

    network  "bridge" (internet access, default), "none" (headless only: gui needs its RDP port) or the name of a
              docker network you created. A host firewall that drops traffic from docker0 blocks the gateway from
              reaching a gui sandbox there: use a network of its own.
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
                   name=name, host=host, runtime=runtime).start()
