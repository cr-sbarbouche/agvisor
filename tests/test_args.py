import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agvisor import Limits, Sandbox, SandboxGone
from agvisor.sandbox import _RDP_SETUP, create_sandbox, list_sandboxes


def test_headless():
    a = Sandbox(limits=Limits(cpus=1, memory="512m", pids=64)).run_args()
    assert {"--runtime=runsc", "--cpus=1", "--memory=512m", "--memory-swap=512m", "--pids-limit=64"} <= set(a)
    assert "--privileged" not in a and a[-2:] == ["sleep", "infinity"]
    assert "--init" in a  # a real PID 1 reaps orphans; `sleep` would leave zombies that eat the pids limit
    assert "--annotation=dev.gvisor.flag.overlay2=root:self,size=5g" in a  # default disk cap
    a = Sandbox(limits=Limits(disk="100M"), gui=True).run_args()
    assert "--annotation=dev.gvisor.flag.overlay2=root:self,size=100M" in a  # gui too; root only, never `all:`


def test_gui_start():
    s = Sandbox(gui=True)
    a = s.run_args()
    assert "--privileged" in a and "--publish=127.0.0.1::3389" in a and a[-1] == "systemd-debian-gnome"
    assert "--init" not in a  # systemd is PID 1 here
    calls = []

    def fake(*args, input=None, **_):
        calls.append((args, input))
        return subprocess.CompletedProcess(args, 0, "127.0.0.1:49153\n[::]:49153\n", "")

    s._docker = fake
    s.start()
    chpasswd, rdp = calls[-2], calls[-1]
    assert s.rdp_port == 49153 and chpasswd[1] == f"gnome:{s.password}\n"  # sudo / console password
    # RDP checks GNOME Remote Desktop's own file (baked as gnome:gnome), not the unix password: it must get ours
    assert rdp[0][:5] == ("exec", "-i", "-u", "gnome", s.name) and rdp[0][-1].endswith("/gnome-remote-desktop/credentials.ini")
    assert rdp[1] == f"[RDP]\ncredentials={{'username': <'gnome'>, 'password': <'{s.password}'>}}\n"
    assert not any(s.password in str(a) for a in rdp[0])  # stdin only: a command line shows up in `ps`


def test_gui_setup_failure_cleans_up():
    calls, run = fake_docker(fail=(_RDP_SETUP,))  # e.g. the desktop never booted
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            create_sandbox(gui=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the RDP setup failure to propagate")
    assert calls[-1][1:3] == ["rm", "-f"]  # a fresh container that never became usable is not left running


def test_runtime():
    assert "--runtime=runsc" in Sandbox().run_args()
    assert "--runtime=runsc-gui" in Sandbox(gui=True, runtime="runsc-gui").run_args()
    for bad in ({"runtime": "--privileged"}, {"runtime": "runsc,x"}, {"runtime": ""},
                {"gui": True, "runtime": "runc"}, {"gui": True, "runtime": "io.containerd.runc.v2"}):  # gui = --privileged
        try:
            Sandbox(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def test_create_sandbox_starts_once():
    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch("agvisor.sandbox.subprocess.run", fake_run):
        with create_sandbox(limits=Limits(cpus=1, memory="512m", pids=64), host="ssh://u@vm") as sb:
            assert sb._running  # `with` calls start() again: must be a no-op
    assert calls[0][:3] == ["docker", "-H", "ssh://u@vm"]  # host= routes docker to the remote machine
    assert [c[3] for c in calls] == ["run", "rm"]  # one docker run, then cleanup
    assert {"--cpus=1", "--memory=512m", "--pids-limit=64"} <= set(calls[0])  # flat params reach docker
    assert not sb._running


def test_limits_reject_unlimited():
    Limits(cpus=0.5, memory="512m", pids=1)  # valid
    for ok in ("1", "100k", "512M", "5g", "1T"):  # what runsc accepted on a real VM
        Limits(disk=ok)
    bad_disk = [{"disk": d} for d in ("0", "0g", "", "1.5g", "2gb", "-1g", "lots", " 1g", "1g\n")]  # 0 = unlimited in runsc
    for bad in ({"cpus": 0}, {"memory": "0"}, {"memory": "0g"}, {"memory": "lots"}, {"pids": 0}, {"pids": -1}, *bad_disk):
        try:
            Limits(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}: docker would treat it as unlimited or fail late")


def test_lifetime_and_validation():
    a = Sandbox(lifetime=60).run_args()
    assert "--rm" in a and a[-2:] == ["sleep", "60"]
    assert "--rm" not in Sandbox().run_args()
    for bad in ({"image": "--privileged"}, {"lifetime": 0}, {"gui": True, "lifetime": 60}):
        try:
            Sandbox(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")


def fake_docker(inspect_out="", fail=(), ps_out="", volume_owner=""):
    """Patchable subprocess.run: records commands; `fail` = words that make a command exit 1;
    `volume_owner` = the agvisor.owner label that `docker volume inspect` reports."""
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        out = {"inspect": inspect_out, "port": "127.0.0.1:49153\n", "ps": ps_out}.get(cmd[1], "")
        if cmd[1:3] == ["volume", "inspect"]:
            out = volume_owner + "\n"
        bad = any(w in cmd for w in fail)
        return subprocess.CompletedProcess(cmd, 1 if bad else 0, out, "boom" if bad else "")

    return calls, run


def test_named_sandbox_mounts_volume():
    a = Sandbox(name="proj").run_args()
    assert "--name=agvisor-proj" in a and "--volume=agvisor-proj:/workspace" in a and "--workdir=/workspace" in a
    g = Sandbox(name="proj", gui=True).run_args()
    assert "--volume=agvisor-proj:/home/gnome" in g and not any(x.startswith("--workdir") for x in g)
    assert not any(x.startswith("--volume") for x in Sandbox().run_args())  # anonymous: nothing persisted
    for bad in ("", "-x", "a b", "a,type=bind,src=/", "a:/etc", "../x", "x" * 64):
        try:
            Sandbox(name=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted name {bad!r}")


def test_resume_and_delete():
    calls, run = fake_docker(inspect_out="false\n")  # earlier container is gone/stopped: fresh one, same volume
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb = create_sandbox(name="proj")
        sb.delete()
    assert [c[1:3] for c in calls] == [["volume", "create"], ["volume", "inspect"], ["inspect", "-f"], ["rm", "-f"],
                                       ["run", "-d"], ["rm", "-f"], ["volume", "rm"]]
    assert calls[-1][1:] == ["volume", "rm", "-f", "agvisor-proj"]

    calls, run = fake_docker(inspect_out="true\n")  # left running: re-attach, never docker run
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb = create_sandbox(name="proj")
    assert [c[1] for c in calls] == ["volume", "volume", "inspect"] and sb._running


def test_failed_resume_keeps_the_container():
    calls, run = fake_docker(inspect_out="true\n", fail=("chpasswd",))
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            create_sandbox(name="proj", gui=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the chpasswd failure to propagate")
    assert "rm" not in [c[1] for c in calls]  # we re-attached to someone's container: don't destroy it


def test_idle_ttl():
    a = Sandbox(idle_ttl=60).run_args()
    assert "--rm" in a and a[-3:-1] == ["sh", "-c"] and "-lt 60 ]" in a[-1]  # PID 1 is the idle watchdog
    a = Sandbox(idle_ttl=60, lifetime=3600).run_args()  # lifetime stays a hard cap on top of the idle clock
    assert a[-5:-1] == ["timeout", "3600", "sh", "-c"]
    for bad in ({"idle_ttl": 0}, {"idle_ttl": -5}, {"idle_ttl": 60, "gui": True}):
        try:
            Sandbox(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")

    sb, seen = Sandbox(idle_ttl=30), []
    sb._docker = lambda *args, **_: seen.append(args) or subprocess.CompletedProcess(args, 0, "", "")
    sb.exec("ls", timeout=10)
    assert "/tmp/.agvisor_active" in seen[0][-1] and seen[0][-1].endswith("; ls")  # exec resets the idle clock
    try:
        sb.exec("ls", timeout=30)  # could still be running when the sandbox expires
    except ValueError:
        pass
    else:
        raise AssertionError("exec timeout >= idle_ttl must be rejected")
    assert len(seen) == 1  # ...before any docker call


def test_list_sandboxes():
    assert "--label=agvisor=1" in Sandbox().run_args()  # what list_sandboxes() filters on
    assert "--label=agvisor.name=proj" in Sandbox(name="proj").run_args()
    assert not any(x.startswith("--label=agvisor.name") for x in Sandbox().run_args())

    # name, state, name label, owner label, CreatedAt: tab-separated, labels may be empty
    calls, run = fake_docker(ps_out="agvisor-proj\trunning\tproj\tu42\t2026-11-06 09:28:33 +0000 UTC\n"
                                    "agvisor-ab12cd34\texited\t\t\t2026-11-05 23:01:02 +0100 CET\n")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        named, anon = list_sandboxes()
        anon.stop()  # a leaked anonymous sandbox can be found and killed
    assert calls[0][1:5] == ["ps", "-a", "--filter", "label=agvisor"] and "label=agvisor.owner=u42" not in calls[0]
    assert (named.name, named.persistent, named._running) == ("agvisor-proj", True, True)
    assert (anon.name, anon.persistent, anon._running) == ("agvisor-ab12cd34", False, False)
    assert (named.owner, anon.owner) == ("u42", None)
    assert named.created == datetime(2026, 11, 6, 9, 28, 33, tzinfo=timezone.utc)
    assert anon.created == datetime(2026, 11, 5, 23, 1, 2, tzinfo=timezone(timedelta(hours=1)))  # zone name dropped
    assert calls[-1][1:] == ["rm", "-f", "agvisor-ab12cd34"]


def test_owner():
    assert "--label=agvisor.owner=u42" in Sandbox(owner="u42").run_args()
    assert not any(x.startswith("--label=agvisor.owner") for x in Sandbox().run_args())
    for bad in ("", "-x", "a b", "a,b", "x" * 64):
        try:
            Sandbox(owner=bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted owner {bad!r}")

    calls, run = fake_docker(volume_owner="u42")  # first or matching owner: volume labelled, container created
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        create_sandbox(name="proj", owner="u42")
    assert calls[0][1:] == ["volume", "create", "--label=agvisor.owner=u42", "agvisor-proj"]
    assert "run" in [c[1] for c in calls]

    for who, kw in (("u99", {"owner": "u99"}), ("nobody", {})):  # another owner, or none: refused before any change
        calls, run = fake_docker(inspect_out="true\n", volume_owner="u42")
        with mock.patch("agvisor.sandbox.subprocess.run", run):
            try:
                create_sandbox(name="proj", **kw)
            except PermissionError:
                pass
            else:
                raise AssertionError(f"{who} got u42's sandbox")
        assert {c[2] for c in calls} == {"create", "inspect"} and {c[1] for c in calls} == {"volume"}

    calls, run = fake_docker()  # anonymous: no volume, so nothing to claim
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        create_sandbox(owner="u42")
    assert "volume" not in [c[1] for c in calls]

    calls, run = fake_docker()
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        list_sandboxes(owner="u42")
    assert calls[0][5:7] == ["--filter", "label=agvisor.owner=u42"]


def test_exec_raises_when_the_sandbox_is_gone():
    sb = Sandbox()
    gone = ["Error response from daemon: container 1a2b is not running",
            "Error response from daemon: No such container: agvisor-x",  # --rm deleted it after lifetime/idle_ttl
            "OCI runtime exec failed: containerManager.WaitPID: EOF"]
    for rc, err in [(1, gone[0]), (1, gone[1]), (128, gone[2])]:
        sb._docker = lambda *a, rc=rc, err=err, **_: subprocess.CompletedProcess(a, rc, "", err)
        try:
            sb.exec("ls")
        except SandboxGone:
            continue
        raise AssertionError(f"rc={rc} {err!r} must raise SandboxGone")
    sb._stream = lambda *a, **_: subprocess.CompletedProcess(a, 1, gone[1], "")  # streaming merges stderr into stdout
    try:
        sb.exec("ls", on_output=print)
    except SandboxGone:
        pass
    else:
        raise AssertionError("streaming exec must raise SandboxGone too")
    # real command failures are returned, not raised: exit 1, a command's own "is not running", a dead-looking stdout
    for rc, out, err in [(1, "", "grep: x: No such file"), (1, "", "nginx is not running"), (0, "Error response from daemon: No such container", ""),
                         (3, "", gone[0]), (1, gone[0], "")]:
        sb._docker = lambda *a, rc=rc, out=out, err=err, **_: subprocess.CompletedProcess(a, rc, out, err)
        assert sb.exec("ls").returncode == rc


def test_exec_streams_output():
    r, w = os.pipe()
    first, got = threading.Event(), []

    def writer():  # the first chunk ends in the middle of "é": it must not come out garbled
        os.write(w, b"h\xc3")
        first.wait(5)
        os.write(w, b"\xa9llo\n")
        os.close(w)

    class Popen:
        def __init__(self, cmd, **_):
            self.cmd, self.stdout, self.returncode = cmd, os.fdopen(r, "rb"), 3

        def wait(self):
            return self.returncode

    sb = Sandbox(name="proj", host="ssh://u@vm")
    threading.Thread(target=writer).start()
    with mock.patch("agvisor.sandbox.subprocess.Popen", Popen):
        res = sb.exec("echo hi", on_output=lambda t: (got.append(t), first.set()))
    assert got == ["h", "\u00e9llo\n"] and (res.returncode, res.stdout) == (3, "h\u00e9llo\n")
    assert res.args[:3] == ["docker", "-H", "ssh://u@vm"] and res.args[3:6] == ["exec", "-i", "agvisor-proj"]
    try:
        sb.exec("cat", input="x", on_output=print)
    except ValueError:
        return
    raise AssertionError("accepted stdin together with on_output")


def test_mcp_server():
    from agvisor import mcp_server as m

    class Fake:  # stands in for a Sandbox
        idle_ttl = 30

        def __init__(self, gone):
            self.gone, self.calls, self.stopped = gone, [], False

        def exec(self, cmd, *, timeout, on_output):
            self.calls.append((cmd, timeout))
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            return subprocess.CompletedProcess([], 3, "out\n", "")

        def stop(self):
            self.stopped = True

    boxes, kws = [], []

    def create(*a, **kw):
        kws.append(kw)
        boxes.append(Fake(gone=len(boxes) == 1))  # the second sandbox dies on its first command
        return boxes[-1]

    m._sb = None
    with mock.patch.object(m, "create_sandbox", create), \
            mock.patch.dict(os.environ, {"AGVISOR_IDLE_TTL": "30", "AGVISOR_HOST": "ssh://u@vm"}):
        assert m.run("echo hi", timeout=999) == "out\n\n[exit 3]"
        # the agent gets no say in these: network off, host and limits from the environment, hard lifetime cap
        assert (kws[0]["network"], kws[0]["host"], kws[0]["idle_ttl"], kws[0]["lifetime"]) == ("none", "ssh://u@vm", 30, 3600)
        assert kws[0]["limits"] == Limits(cpus=2, memory="2g")
        assert boxes[0].calls == [("echo hi", 23)]  # exec() refuses timeout >= idle_ttl - 6: clamped to idle_ttl - 7
        m.run("again")
        assert len(boxes) == 1  # reused, not recreated
        m.reset()
        assert boxes[0].stopped and m._sb is None
        msg = m.run("x")  # sandbox 2 is dead: the agent is told, nothing is silently re-run
        assert msg.startswith("sandbox was reset") and boxes[1].stopped and m._sb is None and len(boxes) == 2
        assert m.run("y").endswith("[exit 3]") and len(boxes) == 3  # the next call gets a fresh one
    m._sb = None


def test_skill_frontmatter_is_strict_yaml():
    # Claude Code and OpenCode forgive a bare `: ` inside a value; VS Code's strict YAML parser rejects the skill
    path = Path(__file__).parent.parent / ".claude/skills/agvisor/SKILL.md"
    if not path.exists():  # the sdist ships tests/ but not .claude/
        return
    front = path.read_text().split("---")[1]
    for line in front.strip().splitlines():
        _, _, val = line.partition(": ")
        if val.startswith(('"', "'")):
            assert val[-1] == val[0] and val[0] not in val[1:-1], f"unclosed or nested quote: {line}"
        else:
            assert ": " not in val and " #" not in val, f"quote this value, a bare ': ' or ' #' breaks YAML: {line}"
    assert "\nname: agvisor\n" in front  # VS Code: `name` must equal the skill's directory name


if __name__ == "__main__":
    test_headless()
    test_gui_start()
    test_gui_setup_failure_cleans_up()
    test_runtime()
    test_create_sandbox_starts_once()
    test_limits_reject_unlimited()
    test_lifetime_and_validation()
    test_named_sandbox_mounts_volume()
    test_resume_and_delete()
    test_failed_resume_keeps_the_container()
    test_idle_ttl()
    test_list_sandboxes()
    test_owner()
    test_exec_raises_when_the_sandbox_is_gone()
    test_exec_streams_output()
    test_mcp_server()
    test_skill_frontmatter_is_strict_yaml()
    print("ok")
