# agvisor

gVisor (`runsc`) sandboxes for coding agents. Thin wrapper over the `docker` CLI: CPU/memory/pids limits, optional GNOME desktop over RDP.

## Host setup (Linux VM or server)

Install [gVisor](https://gvisor.dev/docs/user_guide/install/), then register the runtime in `/etc/docker/daemon.json` and restart docker.
The two `runtimeArgs` are only needed for GUI sandboxes (systemd needs cgroup v2, `sudo` needs suid):

```json
{"runtimes": {"runsc": {"path": "/usr/local/bin/runsc",
  "runtimeArgs": ["--in-sandbox-cgroup=v2", "--allow-suid"]}}}
```

**On AWS/Azure/GCP VMs:** with `network="bridge"` a sandbox can reach the cloud metadata endpoint (`169.254.169.254`) and read the VM's instance credentials. Block it on the host (untested here): `sudo iptables -I DOCKER-USER -d 169.254.169.254 -j DROP`, or use `network="none"`.

GUI image (x86_64), from the [gVisor systemd post](https://gvisor.dev/blog/2026/09/17/systemd-in-gvisor/):

```sh
git clone https://github.com/google/gvisor.git && cd gvisor
docker build -t systemd-debian-gnome \
  -f images/systemd-user/systemd-debian-gnome/Dockerfile.x86_64 images/systemd-user/systemd-debian-gnome
```

## Use

```python
from agvisor import Limits
from agvisor.sandbox import create_sandbox   # creates AND starts the sandbox

SMALL = Limits(cpus=1, memory="512m", pids=128)   # define once, reuse for any number of sandboxes

with create_sandbox(limits=SMALL, lifetime=3600) as sb:                      # headless
    r = sb.exec("python3 -c 'print(1+1)'", timeout=30)
    print(r.returncode, r.stdout)

with create_sandbox(gui=True, limits=Limits(cpus=4, memory="6g")) as sb:     # desktop
    print(f"RDP 127.0.0.1:{sb.rdp_port}  user=gnome  password={sb.password}")
    input("press enter to destroy")

sb = create_sandbox(host="ssh://user@my-vm")   # same thing, on a remote VM/server (e.g. EC2, Azure)
sb.stop()                                      # without `with`, you stop it yourself
```

| param | meaning |
|---|---|
| `image` | default `ubuntu:24.04`, or the GNOME image when `gui=True` |
| `gui` | GNOME desktop over RDP |
| `limits` | `Limits(cpus, memory, pids, shm)`: default 2 cores, `2g`, 512 processes, docker's `/dev/shm` size. Zero or negative values are rejected (docker would read 0 as unlimited) |
| `network` | `"bridge"` (internet, default) or `"none"` (headless only) |
| `lifetime` | seconds until the sandbox deletes itself, even if your process dies (headless only) |
| `name` | makes the sandbox resumable, see below |
| `host` | `None` = docker on this machine (or `$DOCKER_HOST`); `"ssh://user@host"` = docker on a remote VM/server |

### Resuming a sandbox

```python
with create_sandbox("python:3.12", name="my-project") as sb:   # session 1
    sb.exec("git clone https://github.com/me/repo && python -m venv .venv && .venv/bin/pip install -r repo/requirements.txt")
# the container is gone (CPU/RAM freed), /workspace is kept

with create_sandbox("python:3.12", name="my-project") as sb:   # session 2, days later
    sb.exec("ls -a")                                            # -> repo/ .venv/   (cwd is /workspace)

create_sandbox(name="my-project").delete()          # forget it: erases the data too
```

- **Kept:** everything under `sb.data_dir` (`/workspace`; the `gnome` home for `gui=True`), in a docker volume `agvisor-<name>`.
- **Not kept:** processes, and anything outside `data_dir` (e.g. `apt install`): gVisor discards the rootfs overlay when the container is destroyed. Work in `/workspace` (venvs, repos, caches), or bake system packages into your image.
- **Keep everything:** don't call `stop()`. The container stays running and the next `create_sandbox(name=...)` re-attaches to it with processes and installed packages intact. (`limits`/`image` are then ignored: they were fixed at creation.)
- The volume lives on the docker host. On a cloud VM, keep `/var/lib/docker` on a persistent disk (EBS / managed disk), not instance-store, and resume on the same VM. Don't set `--overlay2=all:*` in the `runsc` args: it would stop volumes persisting.

For a remote host the RDP port is on that host's `127.0.0.1`: tunnel it with `ssh -L`.
`network="none"` cuts network access (headless only: GUI needs the published port).

## GUI in a web page (React, Next.js, Vue, plain HTML)

Browsers can't speak RDP, so there is a gateway in between: `browser <-wss-> web/gateway.js <-> guacd <-RDP-> sandbox`.
Run these on the docker host (`pip install 'agvisor[web]'` for the token helper):

```sh
# guacd, loopback only (the image's default entrypoint binds 0.0.0.0, an open RDP proxy on host networking)
docker run -d --name agvisor-guacd --restart=unless-stopped --network=host \
  --entrypoint /opt/guacamole/sbin/guacd guacamole/guacd -f -b 127.0.0.1 -L info

export AGVISOR_KEY=$(openssl rand -hex 32)          # shared by the gateway and your backend
cd web && npm install && npm run gateway            # listens on 127.0.0.1:8080
```

Put the gateway behind your TLS reverse proxy (so the page uses `wss://`) and your auth. In your backend, after the user is authorised:

```python
sb = create_sandbox(gui=True)
token = sb.desktop_token(bytes.fromhex(os.environ["AGVISOR_KEY"]), ttl=60)  # server-side only: it holds the RDP password
# return {"gateway": "wss://your.host/desktop", "token": token} to the page
```

In the page, give the element a size and pass both values (the component is `web/`, npm package `agvisor-desktop`):

```jsx
import "agvisor-desktop";   // React / Next.js ('use client'): safe to import during SSR, it does nothing on the server
<agvisor-desktop gateway={gateway} token={token} style={{ width: "100%", height: 600 }} />
```

```html
<!-- Vue: compilerOptions.isCustomElement = (tag) => tag === "agvisor-desktop"; plain HTML: <script type="module"> -->
<agvisor-desktop :gateway="gateway" :token="token" style="width:100%;height:600px"></agvisor-desktop>
```

A new `token` reconnects; the element fires an `error` event (`e.detail` = message). TypeScript needs a JSX `IntrinsicElements` entry for the tag.
A token only has to be valid when the session *opens*, but anyone holding it can open one until `ttl` runs out. `npm test` in `web/` checks the token round trip against the real gateway with a fake guacd.
