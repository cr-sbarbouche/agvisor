import os
import subprocess
import threading
from unittest import mock

from agvisor import Limits, Sandbox
from agvisor.sandbox import create_sandbox, list_sandboxes


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
    assert s.rdp_port == 49153 and calls[-1][1] == f"gnome:{s.password}\n"


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


def fake_docker(inspect_out="", fail=(), ps_out=""):
    """Patchable subprocess.run: records commands; `fail` = words that make a command exit 1."""
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        out = {"inspect": inspect_out, "port": "127.0.0.1:49153\n", "ps": ps_out}.get(cmd[1], "")
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
    assert [c[1] for c in calls] == ["inspect", "rm", "run", "rm", "volume"]
    assert calls[-1][1:] == ["volume", "rm", "-f", "agvisor-proj"]

    calls, run = fake_docker(inspect_out="true\n")  # left running: re-attach, never docker run
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        sb = create_sandbox(name="proj")
    assert [c[1] for c in calls] == ["inspect"] and sb._running


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

    calls, run = fake_docker(ps_out="agvisor-proj running proj\nagvisor-ab12cd34 exited \n")
    with mock.patch("agvisor.sandbox.subprocess.run", run):
        named, anon = list_sandboxes()
        anon.stop()  # a leaked anonymous sandbox can be found and killed
    assert calls[0][1:5] == ["ps", "-a", "--filter", "label=agvisor"]
    assert (named.name, named.persistent, named._running) == ("agvisor-proj", True, True)
    assert (anon.name, anon.persistent, anon._running) == ("agvisor-ab12cd34", False, False)
    assert calls[-1][1:] == ["rm", "-f", "agvisor-ab12cd34"]


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


if __name__ == "__main__":
    test_headless()
    test_gui_start()
    test_create_sandbox_starts_once()
    test_limits_reject_unlimited()
    test_lifetime_and_validation()
    test_named_sandbox_mounts_volume()
    test_resume_and_delete()
    test_failed_resume_keeps_the_container()
    test_idle_ttl()
    test_list_sandboxes()
    test_exec_streams_output()
    print("ok")
