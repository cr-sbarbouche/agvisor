# agvisor

gVisor (`runsc`) sandboxes for coding agents. Thin wrapper over the `docker` CLI: CPU/memory/pids limits, optional GNOME desktop over RDP.

> **Alpha, and not security-audited.** Read [Security](#security) before running code you don't trust.

## Host setup (Linux VM or server)

Install [gVisor](https://gvisor.dev/docs/user_guide/install/), then register the runtime in `/etc/docker/daemon.json` and `systemctl reload docker` (a *restart* bounces every running container unless `live-restore` is on).
The two `runtimeArgs` are only needed for GUI sandboxes (systemd needs cgroup v2, `sudo` needs suid). Register them as a **second** runtime, so headless sandboxes keep the strict one, and pass `runtime="runsc-gui"` with `gui=True`:

```json
{"runtimes": {"runsc": {"path": "/usr/local/bin/runsc"},
              "runsc-gui": {"path": "/usr/local/bin/runsc",
                            "runtimeArgs": ["--in-sandbox-cgroup=v2", "--allow-suid"]}}}
```

(Putting the args on `runsc` itself also works and needs no `runtime=`, but then every sandbox on the host gets them.)

**On AWS/Azure/GCP VMs:** with `network="bridge"` a sandbox can reach the cloud metadata endpoint (`169.254.169.254`) and read the VM's instance credentials. Block it on the host (untested here): `sudo iptables -I DOCKER-USER -d 169.254.169.254 -j DROP`, or use `network="none"`.

GUI image (x86_64), from the [gVisor systemd post](https://gvisor.dev/blog/2026/09/17/systemd-in-gvisor/):

```sh
git clone https://github.com/google/gvisor.git && cd gvisor
docker build -t systemd-debian-gnome \
  -f images/systemd-user/systemd-debian-gnome/Dockerfile.x86_64 images/systemd-user/systemd-debian-gnome
```

The image is ~8 GB and the build takes ~30 min on 2 vCPUs (checked on a 2 vCPU / 4 GB VM: peak ~1.5 GB RAM). A desktop needs about 0.7 GB RAM idle, so give it `memory="1536m"` or more.

**Host firewall:** if yours drops traffic from `docker0` (e.g. `INPUT -i docker0 -j DROP`, a common way to keep sandboxes away from the host), the gateway can't reach a desktop on the default bridge: guacd hangs without an error. Create a network for desktops (`docker network create desktops`) and pass `network="desktops"`. Your egress rules don't apply to it, so don't run untrusted code there.

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
| `gui` | GNOME desktop over RDP, with a random RDP password (not the image's `gnome:gnome`). `start()` waits for the desktop to boot: ~30-60 s |
| `runtime` | docker runtime, default `runsc`. With a separate GUI runtime: `"runsc-gui"`. Must be a gVisor runtime: plain `runc` is refused for `gui` (it runs `--privileged`, which is only simulated under gVisor) |
| `limits` | `Limits(cpus, memory, pids, shm, disk)`: default 2 cores, `2g`, 512 processes, docker's `/dev/shm` size, `5g` root disk. Zero or negative values are rejected (docker and runsc would read 0 as unlimited) |
| `network` | `"bridge"` (internet, default), `"none"` (headless only) or the name of a docker network you created |
| `lifetime` | seconds until the sandbox deletes itself, even if your process dies (headless only) |
| `idle_ttl` | seconds without an `exec()` before the sandbox deletes itself, even if your process dies (headless only). Expiry can lag by up to ~5s. See below |
| `name` | makes the sandbox resumable, see below |
| `host` | `None` = docker on this machine (or `$DOCKER_HOST`); `"ssh://user@host"` = docker on a remote VM/server |

`sb.exec(cmd, input=None, timeout=120)` runs `sh -c cmd` inside and returns a `CompletedProcess`; on timeout the command is killed and `returncode` is 124. That relies on GNU `timeout`, so use Debian/Ubuntu-based images (`python:3.12-slim`, `ubuntu`). On Alpine (busybox) the code is 143 and only the shell is killed: the children of a compound command (`a; b`) keep running. Also don't set an `ENTRYPOINT` in your image: agvisor starts it with `sleep infinity` as arguments.

Under gVisor, the limits behave differently from plain docker (checked on real `runsc`):
- `pids` budgets the sandbox runtime's *host threads*, not guest processes: an idle sandbox already uses ~35. Keep the default (512): at 64–128, a fork storm made `fork` fail with `Out of memory` and, pushed harder, killed the sandbox.
- Going over `memory` or `pids` kills the **whole sandbox**, not one process, and the host doesn't notice. Afterwards `exec` returns `returncode` 1 or 128 with `is not running` or `WaitPID` in stderr. Call `create_sandbox(name=...)` again: it starts a fresh container on the same `/workspace` volume.
- `disk` caps the **root filesystem** (runsc's `overlay2` annotation, checked on release-20261026.0): past it, writes fail with `No space left on device` and the sandbox keeps running. Whole number + `k/m/g/t`: `1.5g` and `2gb` are rejected. It does **not** cover `/tmp`, which gVisor backs with RAM (counted in `memory`), nor the `/workspace` volume of a named sandbox: that is a plain host directory with **no size cap**, so a runaway agent can fill the host's disk through it. Until that has a cap, keep `/var/lib/docker` on a disk of its own, and don't share it with anything you care about. `tests/test_gvisor_disk.py` checks the cap against real gVisor.
- Inside, `uname -r` reports `4.19.0-gvisor` and `/sys/fs/cgroup` and `/sys/class/net` don't exist (use `/proc/net/dev`).

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

### Expiry and cleanup

```python
from agvisor import list_sandboxes

sb = create_sandbox(name="u42", idle_ttl=900, lifetime=4 * 3600)   # gone after 15 idle minutes, or 4h at most
for sb in list_sandboxes(host="ssh://user@my-vm"):                 # every agvisor sandbox on that docker host
    print(sb.name); sb.stop()                                      # exec(), stop() and delete() work on these handles
```

- `idle_ttl`: the clock restarts when each `exec()` starts and ends; background processes don't count. `exec(timeout=)` must be shorter than `idle_ttl` (else `ValueError`), so a command can't outlive the sandbox. It is fixed at creation, like `limits`.
- A named sandbox that expires keeps its files: the next `create_sandbox(name=...)` makes a fresh container on the same volume. `delete()` is what erases them: run it from your own retention job.
- `list_sandboxes()` also finds anonymous sandboxes left behind by a crashed process. Don't `start()` a stopped handle: it would make a fresh default-image container.

## GUI in a web page (React, Next.js, Vue, plain HTML)

Browsers can't speak RDP, so there is a gateway in between: `browser <-wss-> gateway/gateway.js <-> guacd <-RDP-> sandbox`.
Start both on the docker host with the compose file (`pip install 'agvisor[web]'` for the token helper):

```sh
export AGVISOR_KEY=$(openssl rand -hex 32)   # shared by the gateway and your backend; compose refuses to start without it
docker compose up -d --build                 # guacd + gateway, both on 127.0.0.1; gateway on :8090 (GATEWAY_PORT to change)
```

The compose file pins both images by digest, binds `guacd` to loopback (its image defaults to `0.0.0.0`, an open RDP proxy on host networking), and runs the gateway as a non-root, read-only container. Linux only: both use the host network. Without Docker: `cd gateway && npm install && npm start`, plus `guacd` bound to `127.0.0.1`.

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

A new `token` reconnects. The element fires an `error` event (`e.detail` = message) when the connection fails or drops, e.g. a rejected or expired token: mint a fresh one and set it. TypeScript needs a JSX `IntrinsicElements` entry for the tag.
A token only has to be valid when the session *opens*, but anyone holding it can open one until `ttl` runs out.
In `web/`: `npm test` checks the token round trip against the real gateway with a fake guacd; `npm run test:browser` runs the component in real Chrome (render, mouse, keyboard, resize, errors, cleanup).

## Security

**This is alpha software and nobody has audited it.** gVisor shrinks the part of the kernel that sandboxed code can reach; it is not a guarantee, and this library's own code has not been reviewed for flaws. Don't use it as the boundary for hostile multi-tenant workloads without a review of your own, and keep `runsc` up to date (tested with release-20261026.0). Known gaps:

- **Whoever can run `docker` on the host controls every sandbox**, and is effectively root on that host. agvisor adds no authentication or authorization: put your own in front of it.
- **`name=` has no ownership check.** `create_sandbox(name="x")` re-attaches to whatever running container is called `agvisor-x`, removes a stopped one of that name, and `list_sandboxes()` returns every sandbox on the host. If a tenant can choose the name, they can attach to, run commands in, stop and `delete()` another tenant's sandbox. Build names yourself (e.g. `f"{tenant_id}-{run_id}"`) and check ownership before you pass them in.
- **`/workspace` has no size cap.** `Limits(disk=)` only caps the root filesystem. The `/workspace` volume of a named sandbox is a plain host directory, so a runaway or hostile process can fill the host's disk through it. Anonymous sandboxes have no volume: their writes fall under `disk` (and RAM-backed `/tmp` under `memory`). For named ones, put `/var/lib/docker` on a disk of its own, so a full disk doesn't take other services down with it.
- **Sandboxes on one docker network can reach each other.** That is Docker's default: every sandbox on `"bridge"` (the default) or on a network you share between tenants can connect to the others' open ports. Use `network="none"`, a separate network per tenant or run, or a network created with `-o com.docker.network.bridge.enable_icc=false` (from Docker's documentation; not tested with agvisor).
- **`network="bridge"` also reaches the outside:** the internet, services listening on the host itself (through the bridge gateway), and on a cloud VM the metadata endpoint that serves instance credentials (see Host setup). Block what you don't want on the host's firewall, or use `network="none"`.
- **`gui=True` runs `--privileged`.** Under gVisor that only grants simulated capabilities; under plain `runc` it would be real, so agvisor refuses `runc` for GUI sandboxes. The RDP password is random per sandbox and the gateway token is encrypted and authenticated, but anyone holding a token can open a session until it expires.

To report a vulnerability, email sadokbarbouche1@outlook.com instead of opening a public issue (details in `SECURITY.md`).

## CI and dependency updates

- `.github/workflows/ci.yml` runs on every PR, every push to `main`, and weekly (new advisories appear without commits):
  `test` (Python tests, the gateway token round trip, and the component in real Chrome), `audit` (`pip-audit` on the locked Python deps, `npm audit` + registry-signature check on `web/`), `semgrep` (code scan of the Python, the JS and the workflows).
- `.github/dependabot.yml` opens weekly update PRs for `uv`, `npm` and the GitHub Actions themselves (minor+patch grouped, 7-day cooldown). Those PRs run the same checks.
- Not configurable from a workflow, set these in the repo settings: make `test`, `audit` and `semgrep` required checks on `main`, and turn on Dependabot alerts.
- Run it locally: `uv run python tests/test_args.py`, then in `web/`: `npm test` and `npm run test:browser` (after `npm ci` in both `web/` and `gateway/`), plus `uvx pip-audit …`, `uvx semgrep …` (exact commands in `ci.yml`).
