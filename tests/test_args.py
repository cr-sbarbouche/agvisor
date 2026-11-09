import base64
import contextlib
import io
import os
import shlex
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
    ps = ("agvisor-old\trunning\told\tu42\t2026-11-06 09:28:33 +0000 UTC\n"
          f"agvisor-new\texited\tnew\t\t{now}\n")

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
        def __init__(self, gone):
            self.gone, self.puts, self.stopped = gone, [], False

        def put(self, path, data):
            if self.gone:
                raise SandboxGone("agvisor-x is gone")
            self.puts.append((path, data))

        def stop(self):
            self.stopped = True

    boxes = []
    m._sb = None
    with mock.patch.object(m, "create_sandbox", lambda *a, **kw: boxes.append(Fake(len(boxes) == 1)) or boxes[-1]):
        assert m.write_file("/tmp/a.py", "caf\u00e9") == "wrote 5 bytes to /tmp/a.py"  # bytes, not characters
        assert boxes[0].puts == [("/tmp/a.py", "caf\u00e9".encode())]
        m.reset()
        assert m.write_file("/tmp/b", "x").startswith("sandbox was reset") and boxes[1].stopped and m._sb is None
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
    test_cli()
    test_mcp_server()
    test_mcp_write_file()
    test_skill_frontmatter_is_strict_yaml()
    print("ok")
