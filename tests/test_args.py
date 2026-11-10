import base64
import contextlib
import io
import os
import re
import shlex
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agvisor import Job, Limits, Sandbox, SandboxGone
from agvisor.sandbox import _RDP_SETUP, create_sandbox, list_sandboxes, prune_egress


def test_headless():
    a = Sandbox(limits=Limits(cpus=1, memory="512m", pids=64)).run_args()
    assert {"--runtime=runsc", "--cpus=1", "--memory=512m", "--memory-swap=512m", "--pids-limit=64"} <= set(a)
    assert "--privileged" not in a and a[-2:] == ["sleep", "infinity"]
    assert "--init" in a  # a real PID 1 reaps orphans; `sleep` would leave zombies that eat the pids limit
    assert "--annotation=dev.gvisor.flag.overlay2=root:self,size=5g" in a  # default disk cap
    assert "--entrypoint=" in a  # an image ENTRYPOINT would take `sleep infinity` as its arguments
    assert a.index("--entrypoint=") < a.index("ubuntu:24.04")  # a docker flag only works before the image
    assert not any(x.startswith("--entrypoint") for x in Sandbox(gui=True).run_args())  # systemd image: its own PID 1
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


def fake_docker(inspect_out="", fail=(), ps_out="", volume_owner="", vols=""):
    """Patchable subprocess.run: records commands; `fail` = words that make a command exit 1;
    `volume_owner` = the agvisor.owner label that `docker volume inspect` reports; `vols` = `docker volume ls -q`."""
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        out = {"inspect": inspect_out, "port": "127.0.0.1:49153\n", "ps": ps_out}.get(cmd[1], "")
        if cmd[1:3] == ["volume", "inspect"]:
            out = volume_owner + "\n"
        if cmd[1:3] == ["volume", "ls"]:
            out = vols
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
                                       ["run", "-d"], ["rm", "-f"], ["volume", "ls"], ["volume", "rm"]]
    assert calls[-2][1:] == ["volume", "ls", "-q", "--filter", "label=agvisor.snap=agvisor-proj"]  # its snapshots go too
    assert calls[-1][1:] == ["volume", "rm", "-f", "agvisor-proj"]

    calls, run = fake_docker(inspect_out="true\n")  # left running: re-attach, never docker run
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb = create_sandbox(name="proj")
    assert [c[1] for c in calls] == ["volume", "volume", "inspect"] and sb._running


def test_snapshot_and_restore():
    snap = "agvisor_snap_proj.clean"
    calls, run = fake_docker(inspect_out="false\n")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb = create_sandbox(name="proj")
        calls.clear()
        sb.snapshot("clean")
        assert [c[1:3] for c in calls] == [["volume", "create"], ["volume", "inspect"], ["volume", "create"], ["run", "--rm"]]
        assert calls[2][2:] == ["create", "--label=agvisor.snap=agvisor-proj", snap]  # delete() finds it by this label
        cp = calls[-1]
        assert "--network=none" in cp and "--runtime=runsc" in cp and f"--volume=agvisor-proj:/from:ro" in cp
        assert f"--volume={snap}:/to" in cp  # the source is read-only, only the copy is written

        calls.clear()
        assert sb.restore("clean") is sb and sb._running
        steps = [c[1:3] for c in calls]
        assert steps.index(["rm", "-f"]) < steps.index(["run", "--rm"]) < steps.index(["run", "-d"])  # stop, copy, fresh container
        cp = calls[steps.index(["run", "--rm"])]
        assert f"--volume={snap}:/from:ro" in cp and "--volume=agvisor-proj:/to" in cp  # the other way round

    calls, run = fake_docker(fail=(snap,))  # no such snapshot: must not wipe the data dir with an empty volume
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            Sandbox(name="proj").restore("clean")
        except ValueError:
            pass
        else:
            raise AssertionError("restored a snapshot that does not exist")
    assert not any(c[1] in ("run", "rm") for c in calls)

    calls, run = fake_docker(fail=("--rm",))  # the copy dies (disk full): no half-copied snapshot is left to restore from
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        try:
            Sandbox(name="proj").snapshot("clean")
        except RuntimeError:
            pass
        else:
            raise AssertionError("swallowed a failed copy")
    assert calls[-1][1:] == ["volume", "rm", "-f", snap]

    calls, run = fake_docker(volume_owner="u42")  # another owner: refused before anything is copied or wiped
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        for act in ("snapshot", "restore"):
            try:
                getattr(Sandbox(name="proj", owner="other"), act)("clean")
            except PermissionError:
                continue
            raise AssertionError(f"{act} ignored the owner")
    assert not any(c[1] in ("run", "rm") for c in calls)

    for act in ("snapshot", "restore"):  # anonymous: no volume to snapshot
        try:
            getattr(Sandbox(), act)("clean")
        except ValueError:
            continue
        raise AssertionError(f"anonymous sandbox {act}d")
    for tag in ("", "-x", "a.b", "a b", "a:/etc", "x" * 33):  # ends up in a volume name
        try:
            Sandbox(name="proj").snapshot(tag)
        except ValueError:
            continue
        raise AssertionError(f"accepted tag {tag!r}")


def test_list_and_delete_snapshot():
    prefix = "agvisor_snap_proj."
    calls, run = fake_docker(vols=f"{prefix}b\n{prefix}a-1\n")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        assert Sandbox(name="proj").list_snapshots() == ["a-1", "b"]
        assert calls[-1][1:] == ["volume", "ls", "-q", "--filter", "label=agvisor.snap=agvisor-proj"]
        calls.clear()
        Sandbox(name="proj").delete_snapshot("b")
        assert calls[-1][1:] == ["volume", "rm", "-f", prefix + "b"]  # -f: deleting one that is already gone is fine

    calls, run = fake_docker(volume_owner="u42")  # another owner: refused, nothing listed or removed
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        for act, args in (("list_snapshots", ()), ("delete_snapshot", ("b",))):
            try:
                getattr(Sandbox(name="proj", owner="other"), act)(*args)
            except PermissionError:
                continue
            raise AssertionError(f"{act} ignored the owner")
    assert not any(c[1:3] in (["volume", "ls"], ["volume", "rm"]) for c in calls)

    # anonymous (no volume), and a tag that would end up in a volume name
    for sb, act, args in ((Sandbox(), "list_snapshots", ()), (Sandbox(), "delete_snapshot", ("b",)),
                          (Sandbox(name="proj"), "delete_snapshot", ("a.b",))):
        try:
            getattr(sb, act)(*args)
        except ValueError:
            continue
        raise AssertionError(f"{act}{args} was accepted")


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

    # name, state, name label, owner label, CreatedAt, egress label: tab-separated, labels may be empty
    calls, run = fake_docker(ps_out="agvisor-proj\trunning\tproj\tu42\t2026-11-06 09:28:33 +0000 UTC\t\n"
                                    "agvisor-ab12cd34\texited\t\t\t2026-11-05 23:01:02 +0100 CET\t1\n")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        named, anon = list_sandboxes()
        anon.stop()  # a leaked anonymous sandbox can be found and killed
    assert calls[0][1:5] == ["ps", "-a", "--filter", "label=agvisor"] and "label=agvisor.owner=u42" not in calls[0]
    assert (named.name, named.persistent, named._running) == ("agvisor-proj", True, True)
    assert (anon.name, anon.persistent, anon._running) == ("agvisor-ab12cd34", False, False)
    assert (named.owner, anon.owner) == ("u42", None)
    assert named.created == datetime(2026, 11, 6, 9, 28, 33, tzinfo=timezone.utc)
    assert anon.created == datetime(2026, 11, 5, 23, 1, 2, tzinfo=timezone(timedelta(hours=1)))  # zone name dropped
    # it was made with egress=: stopping the handle must remove its proxy and network too
    assert [c[1:] for c in calls[-3:]] == [["rm", "-f", "agvisor-ab12cd34"], ["rm", "-f", "agvisor_proxy_ab12cd34"],
                                           ["network", "rm", "agvisor_net_ab12cd34"]]
    assert not named._has_egress


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


def test_put_and_get():
    sb, seen = Sandbox(), []
    odd = "dir/a b;rm -rf x.bin"  # the path goes through a shell: it must arrive quoted
    sb._docker = lambda *a, input=None, **_: (seen.append((a, input)), subprocess.CompletedProcess(a, 0, "", ""))[1]
    sb.put(odd, b"\x00\xff bytes")
    cmd = seen[0][0][-1]
    assert shlex.quote(odd) in cmd and base64.b64decode(seen[0][1]) == b"\x00\xff bytes"  # stdin carries the data, any bytes
    sb._docker = lambda *a, **_: subprocess.CompletedProcess(a, 1, "", "sh: write error: No space left on device")
    try:
        sb.put("x", b"1")
    except RuntimeError as e:
        assert "No space left" in str(e)
    else:
        raise AssertionError("a failed write must raise")
    sb._docker = lambda *a, **_: subprocess.CompletedProcess(a, 1, "", "Error response from daemon: No such container: agvisor-x")
    try:
        sb.put("x", b"1")
    except SandboxGone:
        pass
    else:
        raise AssertionError("a dead sandbox must raise SandboxGone")

    def stream(*chunks, rc=0):  # what the sandbox prints, as docker exec would deliver it
        def f(*a, on_output, **_):
            for c in chunks:
                on_output(c)
            return subprocess.CompletedProcess(a, rc, "".join(chunks), "")
        return f

    b64 = lambda n: base64.b64encode(b"z" * n).decode()
    sb._stream = stream("AAH/", "YWJj")  # split in the middle of the data
    assert sb.get("f") == b"\x00\x01\xffabc"
    sb._stream = stream()
    assert sb.get("empty") == b""
    sb._stream = stream(b64(30))
    assert sb.get("f", max_bytes=30) == b"z" * 30  # exactly the cap: fine
    for evil in (b64(31), "A" * 10_000, "not base64!"):  # one byte over / a sandbox that floods us / junk
        sb._stream = stream(evil)
        try:
            sb.get("f", max_bytes=30)
        except ValueError:
            continue
        raise AssertionError(f"accepted {evil[:12]!r}")
    sb._stream = stream("not a file: nope\n", rc=1)
    try:
        sb.get("nope")
    except RuntimeError as e:
        assert "not a file: nope" in str(e)
    else:
        raise AssertionError("a missing file must raise")


def test_egress_args():
    for bad in ({"egress": []}, {"egress": ["*"]}, {"egress": ["a b"]}, {"egress": ["a,b"]}, {"egress": ["1.2.3.4/33"]},
                {"egress": ["x.org:0"]}, {"egress": ["x.org:99999"]}, {"egress": ["x.org:"]}, {"egress": ["::1"]},
                {"egress": ["x.org"], "network": "none"}, {"egress": ["x.org"], "gui": True}):
        try:
            Sandbox(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    a = Sandbox(name="proj", egress=["pypi.org"]).run_args()
    assert "--network=agvisor_net_proj" in a and "--env-file=/dev/stdin" in a and "--label=agvisor.egress=1" in a
    assert "--network=bridge" in Sandbox().run_args() and "--env-file=/dev/stdin" not in Sandbox().run_args()

    calls, ps_rows = [], ""

    def run(cmd, **kw):
        calls.append((cmd[1:], kw.get("input")))
        out = {"ps": ps_rows}.get(cmd[1], "")
        if cmd[1] == "inspect" and cmd[-1].startswith("agvisor_proxy_"):
            out = "172.18.0.2\n"  # the proxy's address on the private network
        return subprocess.CompletedProcess(cmd, 1 if "ubuntu:24.04" in cmd and fail else 0, out, "boom")

    fail = False
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb = create_sandbox(name="proj", egress=["pypi.org", "*.pythonhosted.org", "10.0.0.0/24:8080"])
        verbs = [c[0][:2] if c[0][0] in ("network", "volume") else c[0][:1] for c in calls]
        # orphans first, then leftovers of the same name, then proxy -> network -> attach -> its address -> sandbox
        assert verbs[4:] == [["ps"], ["network", "prune"], ["rm"], ["network", "rm"], ["run"], ["network", "create"],
                             ["network", "connect"], ["inspect"], ["run"]], verbs
        proxy, create, connect, _, box = [c for c in calls if c[0][0] in ("run", "network", "inspect") and c[0][1] not in ("rm", "prune")][-5:]
        assert create[0] == ["network", "create", "--internal", "--label=agvisor.egress=1", "agvisor_net_proj"]
        assert connect[0] == ["network", "connect", "agvisor_net_proj", "agvisor_proxy_proj"]  # no DNS alias: gVisor has no 127.0.0.11
        penv = dict(l.split("=", 1) for l in proxy[1].split())
        assert penv["AGVISOR_ALLOW"] == "pypi.org,*.pythonhosted.org,10.0.0.0/24:8080"
        benv = dict(l.split("=", 1) for l in box[1].split())
        assert benv["HTTPS_PROXY"] == benv["http_proxy"] == f"http://agvisor:{penv['AGVISOR_TOKEN']}@172.18.0.2:3128"
        assert "localhost" in benv["NO_PROXY"]
        assert not any(penv["AGVISOR_TOKEN"] in str(c[0]) for c in calls)  # stdin only: a command line shows up in `ps`
        assert "--runtime=runc" in proxy[0] and "--runtime=runsc" in box[0]  # gVisor has no embedded DNS: the proxy can't resolve names there
        assert {"--user=65534:65534", "--cap-drop=ALL", "--read-only", "--security-opt=no-new-privileges"} <= set(proxy[0])
        assert "--network=agvisor_net_proj" in box[0] and "python:3.12-slim" in proxy[0]
        assert "def parse_rules" in proxy[0][-1]  # the proxy runs the real source
        calls.clear()
        sb.stop()  # the sandbox first, then what it hangs on
        assert [c[0] for c in calls] == [["rm", "-f", "agvisor-proj"], ["rm", "-f", "agvisor_proxy_proj"],
                                         ["network", "rm", "agvisor_net_proj"]]

        calls.clear()  # an anonymous sandbox has no leftovers of its name to remove first
        create_sandbox(egress=["pypi.org"]).stop()
        assert [c[0][:2] for c in calls] == [["ps", "-a"], ["network", "prune"], ["run", "-d"], ["network", "create"],
                                             ["network", "connect"], ["inspect", "-f"], ["run", "-d"], ["rm", "-f"], ["rm", "-f"],
                                             ["network", "rm"]]

        # orphans: a proxy whose sandbox expired (--rm) is removed once it is old enough, its network pruned after it
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
        old = "2026-01-01 00:00:00 +0000 UTC"
        ps_rows = (f"agvisor_proxy_dead\tagvisor-dead\t{old}\n"  # its sandbox is gone
                   f"agvisor_proxy_live\tagvisor-live\t{old}\nagvisor-live\t1\t{old}\n"  # still has one
                   f"agvisor_proxy_new\tagvisor-new\t{now}\n")  # maybe its sandbox is just being pulled
        calls.clear()
        prune_egress()
        assert [c[0] for c in calls] == [
            ["ps", "-a", "--filter", "label=agvisor.egress", "--format", '{{.Names}}\t{{.Label "agvisor.egress"}}\t{{.CreatedAt}}'],
            ["rm", "-f", "agvisor_proxy_dead"],
            ["network", "prune", "-f", "--filter", "label=agvisor.egress", "--filter", "until=60s"]], calls
        ps_rows = ""
        fail = True  # the sandbox itself fails to start: the proxy and network must not be left behind
        calls.clear()
        try:
            create_sandbox(egress=["pypi.org"])
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the failed run to propagate")
        assert [c[0][0] for c in calls[-2:]] == ["rm", "network"] and calls[-1][0][1] == "rm"


def test_egress_proxy():
    import asyncio
    import base64

    from agvisor import egress_proxy as p

    for e, kind, val in (("pypi.org", "name", "pypi.org"), ("*.x.org:8080", "name", "*.x.org"), ("10.0.0.5", "net", None),
                         ("10.0.0.0/24:*", "net", None), ("PyPI.org", "name", "pypi.org")):
        (k, v, _), = p.parse_rules([e])
        assert k == kind and (val is None or v == val), e
    rules = p.parse_rules(["pypi.org", "*.x.org:8080", "10.0.0.0/24", "1.2.3.4:*"])
    ok = lambda h, port=443: p.match(rules, h, port)
    assert ok("pypi.org") == "name" and ok("pypi.org", 80) == "name" and not ok("pypi.org", 22)  # 80 and 443 by default
    assert not ok("www.pypi.org") and not ok("evil-pypi.org") and not ok("pypi.org.evil.com")
    assert ok("a.x.org", 8080) == "name" and not ok("x.org", 8080) and not ok("a.x.org")  # *. = subdomains, own ports
    assert ok("10.0.0.7") == "net" and not ok("10.0.1.7") and ok("1.2.3.4", 22) == "net"
    assert not ok("::ffff:10.0.0.7") and not ok("::1")  # no IPv6 rules, and a v4-mapped one is not a way in
    for addr, public in (("8.8.8.8", True), ("10.0.0.1", False), ("127.0.0.1", False), ("169.254.169.254", False),
                         ("192.168.1.1", False), ("100.64.0.1", False), ("::ffff:10.0.0.1", False), ("::1", False)):
        assert p._public(addr) is public, addr

    async def origin(r, w):
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi")
        await w.drain()
        w.close()

    async def ask(port, data):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(data)
        await w.drain()
        out = await asyncio.wait_for(r.read(), 5)
        w.close()
        return out

    async def main():
        o = await asyncio.start_server(origin, "127.0.0.1", 0)
        op = o.sockets[0].getsockname()[1]
        srv = await p.serve(p.parse_rules([f"127.0.0.1:{op}", f"localhost:{op}"]), "tok", "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        auth = b"Proxy-Authorization: Basic " + base64.b64encode(b"agvisor:tok") + b"\r\n"
        get = lambda host, *extra: b"".join([f"GET http://{host}/x HTTP/1.1\r\nHost: h\r\n".encode(), *extra, b"\r\n"])
        assert (await ask(port, get(f"127.0.0.1:{op}", auth))).endswith(b"\r\n\r\nhi")  # plain http, allowed
        assert (await ask(port, get(f"127.0.0.1:{op}"))).startswith(b"HTTP/1.1 407")  # no password
        bad = b"Proxy-Authorization: Basic " + base64.b64encode(b"agvisor:nope") + b"\r\n"
        assert (await ask(port, get(f"127.0.0.1:{op}", bad))).startswith(b"HTTP/1.1 407")
        out = await ask(port, get("127.0.0.1:1", auth))  # not in the allowlist: says so, and says which
        assert out.startswith(b"HTTP/1.1 403") and b"127.0.0.1:1 is not in" in out
        out = await ask(port, get(f"localhost:{op}", auth))  # allowed by name, but a name may not reach a private address
        assert out.startswith(b"HTTP/1.1 403") and b"no public address" in out
        out = await ask(port, f"CONNECT 127.0.0.1:{op} HTTP/1.1\r\n".encode() + auth + b"\r\nGET / HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n") and out.endswith(b"hi")  # a tunnel
        assert (await ask(port, b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n" + auth + b"\r\n")).startswith(b"HTTP/1.1 403")
        assert (await ask(port, b"GET / HTTP/1.1\r\n" + auth + b"\r\n")).startswith(b"HTTP/1.1 400")  # not a proxy request
        assert (await ask(port, b"GET https://x.org/ HTTP/1.1\r\n" + auth + b"\r\n")).startswith(b"HTTP/1.1 400")
        assert (await ask(port, b"junk\r\n" + auth + b"\r\n")).startswith(b"HTTP/1.1 400")

    log = io.StringIO()  # the proxy logs one line per connection
    with contextlib.redirect_stdout(log):
        asyncio.run(main())
    assert "allow 127.0.0.1" in log.getvalue() and "deny localhost" in log.getvalue()


def test_jobs():
    sb, calls, answers = Sandbox(idle_ttl=60), [], []

    def fake_exec(cmd, *, timeout, on_output=None, input=None):
        calls.append((cmd, timeout))
        rc, out = answers.pop(0) if answers else (0, "")
        if on_output:
            on_output(out)
        return subprocess.CompletedProcess([], rc, out, "boom")

    sb.exec = fake_exec
    odd = "echo \"it's\"; $(id) `x`"  # reaches a shell as one argument
    job = sb.spawn(odd, timeout=90)
    cmd, t = calls[0]
    assert re.fullmatch(r"[0-9a-f]{12}", job.id) and f"/var/tmp/.agvisor-jobs/{job.id}" in cmd
    assert f"{shlex.quote(odd)} 90 >/dev/null 2>&1 </dev/null &" in cmd and t == 30  # detached from docker exec's pipes
    sb.spawn("x")
    assert " x 0 >/dev/null" in calls[1][0]  # no timeout
    assert Sandbox(idle_ttl=20)._short() == 13 and Sandbox()._short() == 30  # exec() refuses timeout >= idle_ttl - 6
    for bad in ({"timeout": 0}, {"timeout": -1}):
        try:
            sb.spawn("x", **bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    answers.append((1, ""))
    try:
        sb.spawn("x")
    except RuntimeError as e:
        assert "spawn failed" in str(e)
    else:
        raise AssertionError("a job that never started must raise")

    calls.clear()
    answers[:] = [(0, ""), (0, "3\n")]
    assert job.poll() is None and job.poll() == 3 and job.poll() == 3 and len(calls) == 2  # an exit code is remembered
    assert job.returncode == 3
    other = Job(sb, "0123456789ab")
    answers[:] = [(2, "")]
    for call in (other.poll, other.kill, lambda: other.wait(1)):
        answers[:] = [(2, "")]
        try:
            call()
        except LookupError:
            continue
        raise AssertionError("an unknown job must raise LookupError")
    for bad in ("../x", "", "0123456789AB", "x" * 12):
        try:
            Job(sb, bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted job id {bad!r}")

    calls.clear()
    answers[:] = [(0, ""), (0, ""), (0, "0\n")]
    assert Job(sb, "0123456789ab").wait() == 0
    assert [t for _, t in calls] == [30, 30, 30] and "seq 25" in calls[0][0]  # slices: one exec per ~25 s, under idle_ttl
    try:
        Job(Sandbox(idle_ttl=12), "0123456789ab").wait()
    except ValueError:
        pass
    else:
        raise AssertionError("a wait() that can't fit under idle_ttl must say so")
    calls.clear()
    clock = iter([0, 0, 100])  # started at 0; still running after one 10 s slice; the deadline passed by the next look
    with mock.patch("agvisor.sandbox.time.monotonic", lambda: next(clock)):
        answers[:] = [(0, ""), (0, "")]
        try:
            Job(sb, "0123456789ab").wait(10)
        except TimeoutError:
            pass
        else:
            raise AssertionError("expected TimeoutError")
    assert "seq 10" in calls[0][0] and len(calls) == 1  # the last slice is no longer than what is left

    calls.clear()
    j = Job(sb, "0123456789ab")
    j.kill()
    j.kill(force=True)
    assert "kill -s TERM -- -$(cat /var/tmp/.agvisor-jobs/0123456789ab/pid)" in calls[0][0] and "kill -s KILL" in calls[1][0]

    rs = Sandbox(idle_ttl=60)  # the real exec(), over a fake _stream
    seen = []
    rs._stream = lambda *a, on_output: (seen.append(a[-1]), on_output(base64.b64encode(b"h\xc3\xa9llo").decode()),
                                        subprocess.CompletedProcess(a, 0, "", ""))[2]
    j = Job(rs, "0123456789ab")
    assert j.read(10, 50) == "h\u00e9llo".encode()
    assert "tail -c +11 /var/tmp/.agvisor-jobs/0123456789ab/log | head -c 50 | base64 -w0" in seen[0]  # bytes, 1-based
    rs._stream = lambda *a, on_output: (on_output("no such job\n"), subprocess.CompletedProcess(a, 2, "no such job\n", ""))[1]
    try:
        j.read()
    except LookupError:
        pass
    else:
        raise AssertionError("reading an unknown job must raise LookupError")


def test_mcp_jobs():
    from agvisor import mcp_server as m

    class Fake:
        idle_ttl = 600

        def __init__(self):
            self.calls, self.rc, self.out, self.gone, self.stopped = [], None, b"", False, False

        def spawn(self, cmd, *, timeout):
            self.calls.append(("spawn", cmd, timeout))
            return Job(self, "0123456789ab")

        def exec(self, cmd, *, timeout, on_output=None):  # what Job.poll() / .kill() run
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            return subprocess.CompletedProcess([], 0, "" if self.rc is None else f"{self.rc}\n", "")

        def _b64(self, script, max_bytes, timeout):
            self.calls.append(("read", script))
            return self.out[:max_bytes]

        def _short(self):
            return 30

        def stop(self):
            self.stopped = True

    box = Fake()
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: box):
        assert m.start_job("sleep 99", timeout=60) == "0123456789ab" and box.calls == [("spawn", "sleep 99", 60)]
        box.out = "caf\u00e9\n".encode()
        assert m.job_output("0123456789ab") == "caf\u00e9\n\n[running; next offset 6]"  # offsets count bytes
        box.rc = 0
        assert m.job_output("0123456789ab", 6).endswith("[exit 0; next offset 12]")
        assert "invalid job id" in m.job_output("../../etc")  # the agent's string never reaches a shell path
        assert "invalid job id" in m.kill_job("x")
        assert m.kill_job("0123456789ab") == "sent TERM to job 0123456789ab"
        box.gone = True
        assert m.job_output("0123456789ab").startswith("sandbox was reset") and box.stopped and m._sb is None
    m._sb = None


def test_put_dir_and_get_dir():
    import tarfile
    import tempfile

    sb, execs, puts = Sandbox(), [], []
    sb.exec = lambda cmd, **kw: (execs.append(cmd), subprocess.CompletedProcess([], 0, "", ""))[1]
    sb.put = lambda path, data, **kw: puts.append((path, data))
    with tempfile.TemporaryDirectory() as d:
        root = Path(d, "proj")
        (root / "sub").mkdir(parents=True)
        (root / "a.txt").write_text("a")
        (root / "sub" / "b.bin").write_bytes(b"\x00\xff")
        (root / "link").symlink_to("/etc/passwd")  # copied as a link, not followed: the host's file does not travel
        sb.put_dir(str(root), "/work dir")
        (tmp, data), = puts
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            ms = {m.name: m for m in t.getmembers()}
            assert {"./a.txt", "./sub/b.bin", "./link"} <= set(ms) and ms["./link"].issym() and ms["./link"].linkname == "/etc/passwd"
            assert all(m.uid == m.gid == 0 and not m.uname and not m.gname for m in ms.values())  # no local user names
            assert t.extractfile(ms["./sub/b.bin"]).read() == b"\x00\xff"
        assert tmp.startswith("/var/tmp/.agvisor-") and tmp.endswith(".tar")
        assert execs == [f"mkdir -p -- '/work dir' && tar -xf {tmp} -C '/work dir' --no-same-owner; rc=$?; rm -f {tmp}; exit $rc"]
        try:
            sb.put_dir(str(root / "a.txt"), "/x")
        except NotADirectoryError:
            pass
        else:
            raise AssertionError("a file is not a directory")

        def tar_of(*members):  # members: (name, kind, payload)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                for name, kind, payload in members:
                    ti = tarfile.TarInfo(name)
                    ti.type = {"file": tarfile.REGTYPE, "link": tarfile.SYMTYPE, "dir": tarfile.DIRTYPE}[kind]
                    if kind == "link":
                        ti.linkname = payload
                    ti.size = len(payload) if kind == "file" else 0
                    t.addfile(ti, io.BytesIO(payload) if kind == "file" else None)
            return buf.getvalue()

        sb.get = lambda path, *, max_bytes, timeout: got
        dest = Path(d, "out")
        got = tar_of((".", "dir", None), ("./ok.txt", "file", b"hi"), ("./sub", "dir", None), ("./sub/c", "file", b"c"))
        sb.get_dir("/work dir", str(dest))
        assert (dest / "ok.txt").read_bytes() == b"hi" and (dest / "sub" / "c").read_bytes() == b"c"
        assert execs[-2].startswith("[ -d '/work dir' ] ||") and execs[-2].endswith(" -C '/work dir' .") and execs[-1].startswith("rm -f ")
        try:  # a regular file outside the destination is an attack: an error, nothing extracted
            got = tar_of(("../escaped", "file", b"x"))
            sb.get_dir("/x", str(Path(d, "safe")))
        except tarfile.FilterError:
            pass
        else:
            raise AssertionError("extracted a ../ member")
        assert not Path(d, "escaped").exists()
        # links that point out are left out, and reported: absolute (a venv's python) or via ../
        got = tar_of(("./l", "link", "../../etc"), ("./l/pwn", "file", b"x"), ("./abs", "link", "/etc"), ("./ok", "file", b"ok"),
                     ("./inside", "link", "ok"))
        skipped = sb.get_dir("/x", str(Path(d, "safe2")))
        assert skipped == ["./l", "./abs"] and Path(d, "safe2", "ok").read_bytes() == b"ok"
        assert (Path(d, "safe2") / "inside").is_symlink() and not (Path(d, "safe2") / "abs").exists()  # a link that stays inside is kept
        assert Path(d, "safe2", "l", "pwn").read_bytes() == b"x" and not Path(d, "pwn").exists()  # `l` is a plain dir now: nothing left the destination
        got = tar_of(("/tmp/agvisor-escaped", "file", b"x"))  # an absolute name is made relative: it lands inside
        sb.get_dir("/x", str(Path(d, "safe")))
        assert Path(d, "safe", "tmp", "agvisor-escaped").read_bytes() == b"x" and not Path("/tmp/agvisor-escaped").exists()
        sb.get = lambda path, *, max_bytes, timeout: (_ for _ in ()).throw(ValueError("larger than max_bytes"))
        try:
            sb.get_dir("/x", str(Path(d, "big")))
        except ValueError:
            assert execs[-1].startswith("rm -f ") and not Path(d, "big").exists()  # the tarball is removed, nothing is created
        else:
            raise AssertionError("a tar over max_bytes must raise")
        sb.exec = lambda cmd, **kw: subprocess.CompletedProcess([], 1, "", "not a directory")
        try:
            sb.get_dir("/nope", str(Path(d, "x")))
        except RuntimeError as e:
            assert "not a directory" in str(e)
        else:
            raise AssertionError("a missing directory must raise")


def _running(sb):  # a handle as list_sandboxes() makes them
    sb._running = True
    sb.created = datetime.now(timezone.utc)
    return sb


def test_cli():
    from agvisor import cli

    def main(*argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:  # argparse errors
                rc = e.code
        return rc, out.getvalue()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
    ps = ("agvisor-old\trunning\told\tu42\t2026-11-06 09:28:33 +0000 UTC\t\n"
          f"agvisor-new\texited\tnew\t\t{now}\t\n")

    def run(*argv, **kw):
        calls, fake = fake_docker(ps_out=ps, **kw)
        with mock.patch("agvisor.sandbox.subprocess.run", fake):
            rc, out = main(*argv)
        return rc, out, [c[1:] for c in calls[1:]]  # calls[0] is the `ps`

    rc, out, _ = run("ls")
    assert rc == 0 and out.splitlines() == ["agvisor-old\trunning\tu42\t2026-11-06T09:28:33+00:00",
                                           f"agvisor-new\tstopped\t-\t{now[:10]}T{now[11:19]}+00:00"]
    assert run("ls", "--owner", "u42")[0] == 0
    assert run("rm")[2] == [] and run("rm")[0] == 2  # no selector: refuses instead of removing everything
    assert run("rm", "nope")[0] == 2
    rc, out, calls = run("rm", "--older-than", "3600")  # only the old one; its volume stays
    assert (rc, out, calls) == (0, "removed agvisor-old\n", [["rm", "-f", "agvisor-old"]])
    rc, out, calls = run("rm", "agvisor-old", "--data")
    assert out == "removed agvisor-old\n" and ["volume", "rm", "-f", "agvisor-old"] in calls  # --data erases the files too
    assert run("rm", "agvisor-new", "--older-than", "3600")[1] == ""  # both conditions must hold

    def stream(self, *a, on_output):
        on_output("hi\n")
        return subprocess.CompletedProcess(a, 3, "hi\n", "")

    with mock.patch.object(Sandbox, "_stream", stream):
        calls, fake = fake_docker(ps_out=ps)
        with mock.patch("agvisor.sandbox.subprocess.run", fake):
            assert main("exec", "agvisor-old", "echo", "hi") == (3, "hi\n")  # the command's exit code, output as it comes
            assert main("exec", "agvisor-new", "echo")[0] == 2  # stopped: nothing to run in

    calls, fake = fake_docker()
    with mock.patch("agvisor.sandbox.subprocess.run", fake):
        assert main("prune")[0] == 0  # the proxies and networks of expired egress= sandboxes
    assert [c[1] for c in calls] == ["ps", "network"]

    import tempfile
    seen = []

    class Fake(Sandbox):  # the file operations, recorded
        def put(self, path, data, **kw):
            seen.append(("put", path, data))

        def put_dir(self, local, remote, **kw):
            seen.append(("put_dir", Path(local).name, remote))

        def get(self, path, **kw):
            seen.append(("get", path, kw))
            if path == "/missing":
                raise RuntimeError("get '/missing' failed (exit 1): not a file: /missing")
            return b"out"

        def get_dir(self, remote, local, **kw):
            seen.append(("get_dir", remote, kw))

        def exec(self, cmd, **kw):
            return subprocess.CompletedProcess([], 0 if "dir" in cmd else 1, "", "")

    calls, fake = fake_docker(ps_out=ps)
    with tempfile.TemporaryDirectory() as d, mock.patch("agvisor.sandbox.subprocess.run", fake), \
            mock.patch("agvisor.cli.list_sandboxes", lambda *a, **kw: [_running(Fake(name="old", host=None))]):
        f = Path(d, "in.txt")
        f.write_bytes(b"in")
        assert main("put", "agvisor-old", str(f), "/w/in.txt") == (0, "done\n") and seen[-1] == ("put", "/w/in.txt", b"in")
        assert main("put", "agvisor-old", d, "/w")[0] == 0 and seen[-1] == ("put_dir", Path(d).name, "/w")
        assert main("put", "agvisor-old", str(Path(d, "nope")), "/w")[0] == 2  # a local file that is not there: an error, no traceback
        out = Path(d, "out.txt")
        assert main("get", "agvisor-old", "/w/x", str(out))[0] == 0 and out.read_bytes() == b"out" and seen[-1] == ("get", "/w/x", {})
        main("get", "agvisor-old", "/w/x", str(out), "--max-bytes", "5")
        assert seen[-1] == ("get", "/w/x", {"max_bytes": 5})
        assert main("get", "agvisor-old", "/w/dir", str(Path(d, "o")))[0] == 0 and seen[-1] == ("get_dir", "/w/dir", {})
        assert main("get", "agvisor-old", "/missing", str(out))[0] == 2  # the sandbox's error becomes a usage error


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

        def snapshot(self, tag):
            self.calls.append(("snapshot", tag))

        def restore(self, tag):
            self.calls.append(("restore", tag))

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
        assert kws[0]["name"] is None  # anonymous unless AGVISOR_NAME says otherwise
    m._sb = None

    # AGVISOR_EGRESS replaces the network setting: an allowlist, not "none"/"bridge"
    boxes.clear(), kws.clear()
    with mock.patch.object(m, "create_sandbox", create), \
            mock.patch.dict(os.environ, {"AGVISOR_EGRESS": " pypi.org, *.pythonhosted.org ,", "AGVISOR_NETWORK": "bridge"}):
        m.run("x")
        assert kws[0]["egress"] == ["pypi.org", "*.pythonhosted.org"] and "network" not in kws[0]
    m._sb = None

    # AGVISOR_NAME: a named sandbox, whose /workspace the checkpoint tools save and roll back
    boxes.clear(), kws.clear()
    with mock.patch.object(m, "create_sandbox", create), mock.patch.dict(os.environ, {"AGVISOR_NAME": "proj"}):
        assert m.snapshot() == "checkpoint saved" and kws[0]["name"] == "proj"
        assert "restarted" in m.restore()
        assert boxes[0].calls == [("snapshot", "checkpoint"), ("restore", "checkpoint")]  # one fixed slot: no tag from the agent
        m._sb = None
        assert "Only /workspace is kept" in m.run("x")  # boxes[1] is the one that dies; its files are not all gone
    m._sb = None


def test_mcp_write_file():
    from agvisor import mcp_server as m

    class Fake:
        idle_ttl = 60  # exec() refuses timeout >= idle_ttl - 6

        def __init__(self, gone):
            self.gone, self.puts, self.stopped, self.files, self.execs = gone, [], False, {}, []

        def put(self, path, data, *, timeout):
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            assert timeout == 53  # clamped under idle_ttl
            self.puts.append((path, data))

        def get(self, path, *, max_bytes, timeout):
            assert timeout == 53
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            if path not in self.files:
                raise RuntimeError(f"get {path!r} failed (exit 1): not a file: {path}")
            if len(self.files[path]) > max_bytes:
                raise ValueError("too big")
            return self.files[path]

        def exec(self, cmd, *, timeout, on_output):
            self.execs.append((cmd, timeout))
            return subprocess.CompletedProcess([], 0, "-rw-r--r-- 1 root root 3 2026-11-09 12:00 a b.txt\n", "")

        def stop(self):
            self.stopped = True

    boxes = []
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: boxes.append(Fake(len(boxes) == 1)) or boxes[-1]):
        assert m.write_file("/tmp/a.py", "caf\u00e9") == "wrote 5 bytes to /tmp/a.py"  # bytes, not characters
        assert boxes[0].puts == [("/tmp/a.py", "caf\u00e9".encode())]
        boxes[0].files = {"/a": "caf\u00e9".encode(), "/bin": b"\x7fELF\x00\x01", "/big": b"x" * 100_001}
        assert m.read_file("/a") == "caf\u00e9"
        assert m.read_file("/bin") == "/bin is binary (6 bytes)"  # no NUL-laden text in the agent's context
        assert "bigger than 100000 bytes" in m.read_file("/big") and "not a file" in m.read_file("/nope")
        assert m.list_files("/work dir; rm x").endswith("a b.txt\n\n[exit 0]")
        assert boxes[0].execs == [("ls -lAp --time-style=long-iso -- '/work dir; rm x'", 30)]  # the path arrives quoted
        m.reset()
        assert m.write_file("/tmp/b", "x").startswith("sandbox was reset") and boxes[1].stopped and m._sb is None
    m._sb = None


def test_mcp_edit_file():
    from agvisor import mcp_server as m

    class Fake:
        idle_ttl = 60

        def __init__(self):
            self.files, self.stopped = {"/a": "x = 1\ny = 1\ncafé = 1\n".encode(), "/bin": b"\xff\xfe", "/big": b"x" * 1_000_001}, False

        def get(self, path, *, max_bytes, timeout):
            if path not in self.files:
                raise RuntimeError(f"get {path!r} failed (exit 1): not a file: {path}")
            if len(self.files[path]) > max_bytes:
                raise ValueError("too big")
            return self.files[path]

        def put(self, path, data, *, timeout):
            self.files[path] = data

    box = Fake()
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: box):
        assert m.edit_file("/a", "y = 1", "y = 2") == "edited /a" and box.files["/a"] == "x = 1\ny = 2\ncafé = 1\n".encode()
        assert m.edit_file("/a", "= 1", "= 3").startswith("old text appears 2 times") and b"y = 2" in box.files["/a"]  # untouched
        assert m.edit_file("/a", "nope", "x") == "old text not found in /a"
        assert m.edit_file("/a", "café", "tea") == "edited /a" and box.files["/a"].endswith(b"tea = 1\n")
        assert m.edit_file("/a", "", "x").startswith("old must not be empty")
        assert "not UTF-8" in m.edit_file("/bin", "a", "b") and box.files["/bin"] == b"\xff\xfe"
        assert "bigger than" in m.edit_file("/big", "a", "b") and "cannot edit /nope" in m.edit_file("/nope", "a", "b")
    m._sb = None


def test_mcp_python():
    import sys
    import tempfile

    from agvisor import mcp_server as m

    d = tempfile.mkdtemp(dir="/tmp")  # short: a unix socket path is limited to ~100 characters
    env = {**os.environ, "AGVISOR_PYDIR": f"{d}/py", "PATH": f"{os.path.dirname(sys.executable)}:{os.environ['PATH']}"}

    class Fake:  # runs the real command locally with sh, as exec() would inside the sandbox
        idle_ttl = 600

        def exec(self, cmd, *, input=None, timeout):
            try:
                return subprocess.run(["sh", "-c", cmd], input=input, capture_output=True, text=True, env=env, timeout=timeout)
            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess([], 124, "", "")

    box = Fake()
    m._sb = None
    try:
        with mock.patch.object(m, "create_sandbox", lambda *a, **kw: box):
            out = m.python("x = 41\nprint('hi')\nimport sys; print('err', file=sys.stderr)")
            assert out.startswith("[new python session") and out.endswith("hi\nerr\n"), out
            assert m.python("x + 1") == "42\n"  # same interpreter: no new-session notice, the last expression is printed
            assert m.python("def f(n): return n * 2\n") == "(no output)" and m.python("f(x)") == "82\n"
            out = m.python("1 / 0")
            assert "ZeroDivisionError" in out and 'File "<python>"' in out and "_pysession" not in out, out
            assert "SyntaxError" in m.python("def f(:") and m.python("x") == "41\n"  # an error doesn't end the session
            assert "SystemExit" in m.python("raise SystemExit(3)") and m.python("x") == "41\n"
            out = m.python("while True: pass", timeout=1)  # the alarm interrupts it, the variables stay
            assert "TimeoutError: timed out after 1 s" in out and m.python("x") == "41\n", out
            out = m.python("print('a' * 100_000)")  # bounded: the tail is all the agent gets
            assert len(out) <= m._MAX + 1, len(out)
            assert m.python("x", restart=True).startswith("[new python session") and "NameError" in m.python("y")
            # C code that never returns to the interpreter, here the alarm ignored: exec() gives up and the session is killed
            out = m.python("import signal; signal.signal(signal.SIGALRM, signal.SIG_IGN)\nwhile True: pass", timeout=1)
            assert out.startswith("timed out after 1 s and the python session was killed"), out
            assert m.python("1").startswith("[new python session")
            box.exec(m._py("kill"), timeout=30)
            assert not os.path.exists(f"{d}/py/sock")
    finally:
        subprocess.run(["sh", "-c", m._py("kill")], env=env, capture_output=True)
        shutil.rmtree(d, ignore_errors=True)
        m._sb = None

    # no python3 in the image: say so, with the exit code
    class NoPython:
        idle_ttl = 600

        def exec(self, cmd, *, input=None, timeout):
            return subprocess.run(["/bin/sh", "-c", cmd], input=input, capture_output=True, text=True, env={"PATH": "/nonexistent"}, timeout=timeout)

    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: NoPython()):
        assert "python3 is not in this sandbox image" in m.python("1") and m.python("1").endswith("[exit 127]")
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
    test_snapshot_and_restore()
    test_list_and_delete_snapshot()
    test_failed_resume_keeps_the_container()
    test_idle_ttl()
    test_list_sandboxes()
    test_owner()
    test_exec_raises_when_the_sandbox_is_gone()
    test_exec_streams_output()
    test_put_and_get()
    test_egress_args()
    test_egress_proxy()
    test_jobs()
    test_put_dir_and_get_dir()
    test_cli()
    test_mcp_server()
    test_mcp_write_file()
    test_mcp_edit_file()
    test_mcp_python()
    test_mcp_jobs()
    test_skill_frontmatter_is_strict_yaml()
    print("ok")
