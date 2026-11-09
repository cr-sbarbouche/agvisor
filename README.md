# agvisor

[![PyPI Downloads](https://static.pepy.tech/personalized-badge/agvisor?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/agvisor)

gVisor (`runsc`) sandboxes for coding agents. Thin wrapper over the `docker` CLI: CPU/memory/pids limits, optional GNOME desktop over RDP.

Two packages, versioned separately:

| package | what | install |
|---|---|---|
| [`agvisor`](https://pypi.org/project/agvisor/) (PyPI) | the Python library: `create_sandbox(...)`, limits, `list_sandboxes` | `pip install agvisor` |
| [`agvisor-desktop`](https://www.npmjs.com/package/agvisor-desktop) (npm) | the `<agvisor-desktop>` web component that shows a GUI sandbox in a web page ([details](#gui-in-a-web-page-react-nextjs-vue-plain-html)) | `npm install agvisor-desktop` |

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
| `egress` | an allowlist of hosts the sandbox may reach, instead of all or nothing: see [Limiting outbound traffic](#limiting-outbound-traffic). Headless only; don't combine with `network=` |
| `lifetime` | seconds until the sandbox deletes itself, even if your process dies (headless only) |
| `idle_ttl` | seconds without an `exec()` before the sandbox deletes itself, even if your process dies (headless only). Expiry can lag by up to ~5s. See below |
| `name` | makes the sandbox resumable, see below |
| `host` | `None` = docker on this machine (or `$DOCKER_HOST`); `"ssh://user@host"` = docker on a remote VM/server |

`sb.exec(cmd, input=None, timeout=120)` runs `sh -c cmd` inside and returns a `CompletedProcess`; on timeout the command is killed and `returncode` is 124. That relies on GNU `timeout`, so use Debian/Ubuntu-based images (`python:3.12-slim`, `ubuntu`). On Alpine (busybox) the code is 143 and only the shell is killed: the children of a compound command (`a; b`) keep running. An image's `ENTRYPOINT` is ignored: agvisor clears it for headless sandboxes, because it would take agvisor's own `sleep infinity` as arguments (`exec` never ran it anyway). If your entrypoint script did setup work, that no longer happens.

Under gVisor, the limits behave differently from plain docker (checked on real `runsc`):
- `pids` budgets the sandbox runtime's *host threads*, not guest processes: an idle sandbox already uses ~35. Keep the default (512): at 64–128, a fork storm made `fork` fail with `Out of memory` and, pushed harder, killed the sandbox.
- Going over `memory` or `pids` kills the **whole sandbox**, not one process, and the host doesn't notice. Afterwards `exec()` raises `SandboxGone` (it also does once `lifetime` or `idle_ttl` has deleted the container), so a dead sandbox isn't mistaken for a failed command: it recognises docker's and runsc's error text and only looks at exit codes 1 and 128. Call `create_sandbox(name=...)` again: it starts a fresh container on the same `/workspace` volume.
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

To undo a bad install that landed in `/workspace`, save a known-good state and go back to it:

```python
with create_sandbox("python:3.12", name="my-project") as sb:
    sb.exec("python -m venv .venv && .venv/bin/pip install -r requirements.txt")
    sb.snapshot("clean")                            # copies /workspace into a second volume
    sb.exec(".venv/bin/pip install some-sketchy-package")
    sb.restore("clean")                             # /workspace is exactly as it was; new container, processes lost
```

- `snapshot(tag)` replaces a snapshot of the same tag; `restore(tag)` erases whatever is in the data dir now. `list_snapshots()` returns the tags, `delete_snapshot(tag)` frees one, and `delete()` removes them all with the sandbox. Tags: letters, digits, `_`, `-`.
- Named sandboxes only, and the owner check applies. The copy runs in a throwaway gVisor container from the sandbox's image (it needs `sh`, `rm`, `cp`), with no network.
- Each snapshot costs as much host disk as the data dir, and has no size cap either. Stop writing before `snapshot()`: a copy taken during writes can be inconsistent.

- **Kept:** everything under `sb.data_dir` (`/workspace`; the `gnome` home for `gui=True`), in a docker volume `agvisor-<name>`.
- **Not kept:** processes, and anything outside `data_dir` (e.g. `apt install`): gVisor discards the rootfs overlay when the container is destroyed. Work in `/workspace` (venvs, repos, caches), or bake system packages into your image.
- **Keep everything:** don't call `stop()`. The container stays running and the next `create_sandbox(name=...)` re-attaches to it with processes and installed packages intact. (`limits`/`image` are then ignored: they were fixed at creation.)
- The volume lives on the docker host. On a cloud VM, keep `/var/lib/docker` on a persistent disk (EBS / managed disk), not instance-store, and resume on the same VM. Don't set `--overlay2=all:*` in the `runsc` args: it would stop volumes persisting.

For a remote host the RDP port is on that host's `127.0.0.1`: tunnel it with `ssh -L`.
`network="none"` cuts network access (headless only: GUI needs the published port).

### Limiting outbound traffic

```python
sb = create_sandbox("python:3.12-slim", egress=["pypi.org", "*.pythonhosted.org", "10.0.0.0/24:8080"])
sb.exec("pip install requests")      # works: the host is on the list
sb.exec("curl https://example.com")  # 403 from the proxy: "example.com:443 is not in this sandbox's egress allowlist"
```

The sandbox gets a private docker network (`--internal`: no route out) and the only thing on it besides the sandbox is a small allowlist proxy, which also sits on the default bridge. `HTTP_PROXY`/`HTTPS_PROXY` point at it, so `pip`, `apt`, `curl` and Python's `urllib` just work; code that ignores the variables has no route anywhere. Sandboxes with `egress=` can't reach each other either: each has its own network.

- Entries are `host` (exact), `*.host` (subdomains, not `host` itself), an IPv4 address or an IPv4 network, with an optional `:port`. Ports default to 80 and 443; `host:*` is any port. There is no allow-everything entry: that is `network="bridge"`.
- **A name only gets through if it resolves to public addresses**, so a name can't be used to reach `10.x`, the docker host or the cloud metadata endpoint (`169.254.169.254`). Reaching a private address takes an entry that says so (`10.0.0.5`, `10.0.0.0/24`). The proxy connects to the address it checked, so a second lookup can't swap it.
- It is an HTTP(S) proxy: raw sockets (SYN scans, ICMP, UDP) don't pass.
- The proxy runs `python:3.12-slim` (a constant in `sandbox.py`) under the same runtime, so that image has to be on the docker host or pullable. It needs a password because the default bridge can reach it: the password goes to docker on stdin, not on a command line, and is in the sandbox's environment (`HTTPS_PROXY`), where it is useless to anything that can't reach the proxy. `docker logs agvisor_proxy_<name>` shows one `allow` / `deny` line per connection.
- A sandbox that expires (`lifetime`, `idle_ttl`) can't clean up after itself: its proxy notices within ~10 s and ends, and the empty network is removed by the next `egress=` start (networks younger than a minute are left alone). `stop()`, `delete()` and `agvisor rm` remove the proxy and network at once. Docker has ~30 default network address pools, so ~30 `egress=` sandboxes at once; widen `default-address-pools` in `daemon.json` for more.
- Starting costs about five more docker calls: on `host="ssh://..."` that is noticeable (enable `ControlMaster`). Fixed at creation, like `limits`: re-attaching to a running named sandbox ignores `egress=`.
- Not done: injecting credentials into requests (that needs intercepting TLS), and checking that the sandbox can't resolve outside names through docker's DNS (see Security).
- Under gVisor, and with the proxy and the network, this has only run as unit tests so far. `tests/test_gvisor_egress.py` checks it against a real host.

### Files in and out

```python
sb.put("/tmp/job/input.csv", open("input.csv", "rb").read())   # parent dirs are created, an existing file is replaced
data = sb.get("/tmp/job/result.json")                          # bytes; a relative path starts in /workspace for a named sandbox
```

Both go through `exec()` (base64 over stdin/stdout), so they need GNU coreutils in the image (Debian/Ubuntu), raise `SandboxGone` if the sandbox is dead and `RuntimeError` if the write or read fails. `get()` reads regular files only and raises `ValueError` past `max_bytes` (default 10 MB): the sandbox is untrusted, so what comes out of it is capped. `tests/test_files.py` runs both against a real container.

```python
sb.put_dir("./project", "/workspace/project")   # a whole directory in: created, same-named files replaced, others kept
sb.get_dir("/workspace/out", "./results")       # and out (max_bytes=50 MB of tar)
```

They are a tar through `put()`/`get()` (GNU tar in the image). `put_dir` copies links as links and never follows them, so a link on your side can't pull a host file into the sandbox, and resets owners. `get_dir` extracts what an untrusted sandbox sends with tarfile's `data` filter: nothing lands outside the destination, no device files, no links that point out, no setuid bits (`tarfile.FilterError` otherwise). From a shell: `agvisor put NAME ./file-or-dir /remote/path` and `agvisor get NAME /remote/path ./local`.

### Background jobs

```python
job = sb.spawn("python scan.py targets.txt", timeout=36000)   # returns at once; it outlives this exec() and your process
while (rc := job.poll()) is None:                              # or job.wait(timeout=...)
    chunk = job.read(offset); offset += len(chunk)             # bytes since `offset`, stdout and stderr merged
job.kill()                                                     # TERM to the command and what it started; kill(force=True) = KILL
Job(sb, job_id).wait()                                         # pick a job up again after your process restarted
```

`exec(timeout=)` has to stay under `idle_ttl`, so anything longer goes here. A job writes to `/var/tmp/.agvisor-jobs/<id>/` (the root filesystem, so `Limits.disk` caps its log). Exit codes: 124 = hit its `timeout`, 143 = killed (TERM), 137 = KILL. `wait()` makes one `exec()` per ~25 s. A running job does **not** keep an `idle_ttl` sandbox alive (background processes never did), but `poll()`, `read()` and `wait()` are execs, so a client that checks on it does; set `lifetime` for a hard budget. `Limits.disk` is the only cap on how much a job logs. `tests/test_gvisor_jobs.py` runs this against real gVisor.

### Expiry and cleanup

```python
from agvisor import list_sandboxes

sb = create_sandbox(name="u42", idle_ttl=900, lifetime=4 * 3600)   # gone after 15 idle minutes, or 4h at most
for sb in list_sandboxes(host="ssh://user@my-vm"):                 # every agvisor sandbox on that docker host
    print(sb.name, sb.created); sb.stop()                          # exec(), stop() and delete() work on these handles
```

- `idle_ttl`: the clock restarts when each `exec()` starts and ends; background processes don't count. `exec(timeout=)` must be shorter than `idle_ttl` (else `ValueError`), so a command can't outlive the sandbox. It is fixed at creation, like `limits`.
- A named sandbox that expires keeps its files: the next `create_sandbox(name=...)` makes a fresh container on the same volume. `delete()` is what erases them: run it from your own retention job.
- `list_sandboxes()` also finds anonymous sandboxes left behind by a crashed process. Don't `start()` a stopped handle: it would make a fresh default-image container.
- `sb.created` is a timezone-aware `datetime` (second precision) of when the container was made, so a retention job can stop what is older than it should be: `[sb.stop() for sb in list_sandboxes() if now - sb.created > max_age]`. Sandboxes from `create_sandbox()` have `created = None`.
- `owner=` tags a sandbox, e.g. with a tenant id: `create_sandbox(name="u42-scan7", owner="u42")`, and `list_sandboxes(owner="u42")` lists only theirs. See the `name=` note under Security for what it does and doesn't protect.
- `agvisor ls`, `agvisor rm`, `agvisor exec` do the same from a shell (add `--host ssh://user@vm` for a remote one, `--owner` to filter): `ls` prints name, state, owner and creation time; `rm NAME...` and/or `rm --older-than SECONDS` removes sandboxes (never everything at once: it needs one of the two), `--data` also erases the files and snapshots of named ones; `exec NAME cmd...` runs a command in a running sandbox and exits with its exit code; `put` and `get` copy files and directories (see Files in and out). A retention job is `agvisor rm --older-than 14400` in cron. `exec`, `put` and `get` don't count as activity for `idle_ttl`.

## Coding agents (skill + MCP server)

Two optional pieces for Claude Code, VS Code (Copilot agent mode) and OpenCode:

- **Skill**, [`.claude/skills/agvisor/SKILL.md`](https://github.com/cr-sbarbouche/agvisor/blob/main/.claude/skills/agvisor/SKILL.md): tells the agent when to sandbox, plus the gotchas above. Copy the `agvisor` folder to `.claude/skills/` (all three read it), or to `.github/skills/` / `.agents/skills/`.
- **MCP server**, `agvisor-mcp`: `run(command, timeout)`, `write_file(path, content)`, `read_file(path)`, `list_files(path)`, `start_job(command)`/`job_output(job_id, offset)`/`kill_job(job_id)` for what outlives `run()`, and `reset()` (plus `snapshot()`/`restore()`, opt-in, below), so the agent runs code in the sandbox, not on your host. It makes one sandbox on first use and keeps it until it has been idle for `AGVISOR_IDLE_TTL`. A skill only advises; to make the sandbox the *only* way the agent runs code, also turn off its host shell tool (if your client lets you).

Settings are environment variables, so the agent can't pick its own image, network or limits:

| variable | default |
|---|---|
| `AGVISOR_HOST` | local docker / `$DOCKER_HOST`; `ssh://user@vm` for a remote Linux host |
| `AGVISOR_NETWORK` | `none` (no internet: `pip install` fails). `bridge` for internet |
| `AGVISOR_EGRESS` | unset. Comma-separated hosts the sandbox may reach, e.g. `pypi.org,files.pythonhosted.org`: [`egress=`](#limiting-outbound-traffic), instead of `AGVISOR_NETWORK` |
| `AGVISOR_IMAGE` | `ubuntu:24.04` |
| `AGVISOR_CPUS` / `AGVISOR_MEMORY` | `2` / `2g` |
| `AGVISOR_IDLE_TTL` / `AGVISOR_LIFETIME` | `600` / `3600` seconds |
| `AGVISOR_NAME` | unset. Set it and `/workspace` is kept in a docker volume, and two more tools appear (below) |

By default the sandbox is anonymous: no `/workspace` volume, so nothing is shared between projects, and the sandbox can't see your files (the agent writes code in with `write_file`).

With `AGVISOR_NAME=my-project` the agent also gets `snapshot()` and `restore()`: one checkpoint of `/workspace` it can save and roll back to, e.g. before a risky `pip install`. `reset()` then keeps `/workspace` (use `restore()` to roll that back). Mind the cost: `/workspace` is a volume with no size cap, shared by every server process that uses that name, and the checkpoint is a second full copy of it. Claude Code:

```sh
claude mcp add agvisor -e AGVISOR_HOST=ssh://user@my-vm -- uvx --from 'agvisor[mcp]' agvisor-mcp
```

VS Code, `.vscode/mcp.json`:

```json
{"servers": {"agvisor": {"type": "stdio", "command": "uvx", "args": ["--from", "agvisor[mcp]", "agvisor-mcp"],
                         "env": {"AGVISOR_HOST": "ssh://user@my-vm"}}}}
```

OpenCode, `opencode.json`:

```json
{"mcp": {"servers": {"agvisor": {"type": "local", "command": ["uvx", "--from", "agvisor[mcp]", "agvisor-mcp"],
                                 "environment": {"AGVISOR_HOST": "ssh://user@my-vm"}}}}}
```

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

In the page, give the element a size and pass both values. The component is the npm package [`agvisor-desktop`](https://www.npmjs.com/package/agvisor-desktop) (source in [`web/`](https://github.com/cr-sbarbouche/agvisor/tree/main/web); its only dependency is `guacamole-common-js`, installed with it):

```sh
npm install agvisor-desktop
```

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
- **`name=` has no ownership check unless you pass `owner=`.** `create_sandbox(name="x")` re-attaches to whatever running container is called `agvisor-x`, removes a stopped one of that name, and `list_sandboxes()` returns every sandbox on the host. If a tenant can choose the name, they can attach to, run commands in, stop and `delete()` another tenant's sandbox. With `create_sandbox(name="x", owner="tenant-a")`, the first owner claims the sandbox's data volume, and any other `owner=` (or none) gets a `PermissionError` before anything is re-attached, removed or deleted. That is a guard against mixed-up names in your own orchestrator, not authentication: `owner` is whatever string the caller passes, and anyone who can run `docker` or build a `Sandbox` by hand and call `delete()` gets around it. Build the names and owners yourself, from your own user ids, never from tenant input. A named sandbox created without `owner=` (including any made before 0.1.2) stays unowned: Docker can't add a label to an existing volume, so a call with `owner=` is refused, and `delete()` it first (that erases its files) to put it under an owner.
- **`/workspace` has no size cap.** `Limits(disk=)` only caps the root filesystem. The `/workspace` volume of a named sandbox is a plain host directory, so a runaway or hostile process can fill the host's disk through it. Anonymous sandboxes have no volume: their writes fall under `disk` (and RAM-backed `/tmp` under `memory`). For named ones, put `/var/lib/docker` on a disk of its own, so a full disk doesn't take other services down with it.
- **Sandboxes on one docker network can reach each other.** That is Docker's default: every sandbox on `"bridge"` (the default) or on a network you share between tenants can connect to the others' open ports. Use `network="none"`, a separate network per tenant or run, or a network created with `-o com.docker.network.bridge.enable_icc=false` (from Docker's documentation; not tested with agvisor).
- **`network="bridge"` also reaches the outside:** the internet, services listening on the host itself (through the bridge gateway), and on a cloud VM the metadata endpoint that serves instance credentials (see Host setup). Block what you don't want on the host's firewall, or use `network="none"`.
- **`egress=` limits what a sandbox can connect to, not what it can say.** Allowed hosts can receive anything (data can ride in a URL to an allowed host), and an allowed name that an attacker controls the DNS of (a `*.` entry on a domain someone else can add subdomains to) can serve anything. I have not checked whether docker's embedded DNS on the private network still forwards lookups of arbitrary names to the host's resolver: if it does, names (not connections) can leave, a slow covert channel. The proxy and the network are plain docker objects: whoever can run `docker` can read the proxy password and the allowlist.
- **`gui=True` runs `--privileged`.** Under gVisor that only grants simulated capabilities; under plain `runc` it would be real, so agvisor refuses `runc` for GUI sandboxes. The RDP password is random per sandbox and the gateway token is encrypted and authenticated, but anyone holding a token can open a session until it expires.

To report a vulnerability, email sadokbarbouche1@outlook.com instead of opening a public issue (details in `SECURITY.md`).

## CI and dependency updates

- `.github/workflows/ci.yml` runs on every PR, every push to `main`, and weekly (new advisories appear without commits):
  `test` (Python tests, the gateway token round trip, and the component in real Chrome), `audit` (`pip-audit` on the locked Python deps, `npm audit` + registry-signature check on `web/`), `semgrep` (code scan of the Python, the JS and the workflows).
- `.github/dependabot.yml` opens weekly update PRs for `uv`, `npm` and the GitHub Actions themselves (minor+patch grouped, 7-day cooldown). Those PRs run the same checks.
- `.github/workflows/publish-npm.yml` publishes `web/` (`agvisor-desktop`) to npm with Trusted Publishing: push a tag `web-vX.Y.Z` that equals the version in `web/package.json` and is on `main`. Run by hand, it only builds, tests and lists the package. Setup steps are at the top of the file.
- Not configurable from a workflow, set these in the repo settings: make `test`, `audit` and `semgrep` required checks on `main`, and turn on Dependabot alerts.
- Run it locally: `uv run python tests/test_args.py`, then in `web/`: `npm test` and `npm run test:browser` (after `npm ci` in both `web/` and `gateway/`), plus `uvx pip-audit …`, `uvx semgrep …` (exact commands in `ci.yml`).
