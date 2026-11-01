import re
import secrets
import subprocess
import uuid

from .desktop import GUI_HOME, GUI_IMAGE, GUI_USER, RDP_PORT, make_token
from .limits import Limits


class Sandbox:
    """host=None -> local docker; host="ssh://user@vm" -> docker on a remote VM/server."""

    def __init__(self, image=None, *, gui=False, limits=Limits(), network="bridge", lifetime=None,
                 name=None, host=None):
        self.gui, self.limits, self.network, self.lifetime, self.host = gui, limits, network, lifetime, host
        self.image = image or (GUI_IMAGE if gui else "ubuntu:24.04")
        if self.image.startswith("-"):  # `docker run ... IMAGE` would parse it as a flag, e.g. --privileged
            raise ValueError(f"invalid image name: {self.image!r}")
        if lifetime is not None and (gui or lifetime <= 0):
            raise ValueError("lifetime must be > 0 seconds and is only supported for headless sandboxes")
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

    def run_args(self):
        l = self.limits
        a = ["run", "-d", "--runtime=runsc", f"--name={self.name}",
             f"--cpus={l.cpus}", f"--memory={l.memory}", f"--memory-swap={l.memory}",
             f"--pids-limit={l.pids}", f"--network={self.network}"]
        if self.lifetime:  # PID 1 is `sleep <lifetime>`: the container exits, then --rm deletes it, even if we crash
            a.append("--rm")
        if self.data_dir:  # a docker volume is a host directory: it is outside gVisor's discarded rootfs overlay
            a.append(f"--volume={self.name}:{self.data_dir}")
            if not self.gui:
                a.append(f"--workdir={self.data_dir}")  # relative paths in exec() land on persistent storage
        if self.gui:  # --privileged only grants simulated caps inside gVisor, not host access
            a += ["--privileged", f"--shm-size={l.shm or '1g'}", f"--publish=127.0.0.1::{RDP_PORT}"]
        elif l.shm:
            a.append(f"--shm-size={l.shm}")
        return a + [self.image] + ([] if self.gui else ["sleep", str(self.lifetime or "infinity")])

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
                self.password = secrets.token_urlsafe(12)
                self._docker("exec", "-i", "-u", "root", self.name, "chpasswd",
                             input=f"{GUI_USER}:{self.password}\n")
        except BaseException:
            if resumed:  # never destroy a container (and its processes) that we merely re-attached to
                self._running = False
            else:
                self.stop()
            raise
        return self

    def exec(self, cmd, *, input=None, timeout=120):
        """Run a shell command inside. Returns CompletedProcess; returncode 124 = timed out."""
        argv = ["timeout", "-k", "5", str(timeout), "sh", "-c", cmd]  # kills inside, not just the client
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


def create_sandbox(
    image: str | None = None,
    *,
    gui: bool = False,
    limits: Limits = Limits(),
    network: str = "bridge",
    lifetime: int | None = None,
    name: str | None = None,
    host: str | None = None,
) -> Sandbox:
    """Create and start a gVisor sandbox. Use `with create_sandbox(...) as sb:` or call sb.stop() yourself.

    image     Image to run. Default: ubuntu:24.04, or the GNOME desktop image when gui=True.
    gui       True -> GNOME desktop over RDP (sb.rdp_port, sb.password, sb.desktop_token()).
    limits    Limits(cpus, memory, pids, shm). Default: 2 cores, 2g RAM, 512 processes. Reusable across sandboxes.
    name      Makes the sandbox resumable. Files in sb.data_dir (/workspace; the gnome home when gui) are kept
              in a docker volume on the host machine and survive stop(), `with` exit and lifetime expiry.
              create_sandbox(name=same) later gets them back: it re-attaches to the container if an earlier
              session left it running (processes and installed packages intact), else makes a fresh one on the
              same volume. sb.delete() erases the data. Default: anonymous, nothing is kept.

    network  "bridge" (internet access, default) or "none" (headless only: gui needs its RDP port).
    lifetime  Seconds until the sandbox destroys itself, even if your process dies. Headless only.
              Default: lives until stop().
    host      Which machine supplies the resources. None -> docker on this machine (or $DOCKER_HOST).
              "ssh://user@host" -> docker on a remote VM/server, e.g. an EC2 or Azure VM.
    """
    return Sandbox(image, gui=gui, limits=limits, network=network, lifetime=lifetime, name=name, host=host).start()
