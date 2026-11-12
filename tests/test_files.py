"""Needs a docker host (the shell commands behind put() and get() run in a real container; the `gvisor` job in ci.yml runs it):

    AGVISOR_HOST=ssh://user@vm uv run python tests/test_files.py
    AGVISOR_RUNTIME=runc uv run python tests/test_files.py      # e.g. Docker Desktop, where there is no runsc

AGVISOR_HOST unset -> docker on this machine. AGVISOR_RUNTIME defaults to runsc. The image needs GNU coreutils
(AGVISOR_IMAGE, default ubuntu:24.04).
"""
import os

from agvisor import create_sandbox

with create_sandbox(os.environ.get("AGVISOR_IMAGE"), host=os.environ.get("AGVISOR_HOST"), network="none", lifetime=300,
                    runtime=os.environ.get("AGVISOR_RUNTIME", "runsc")) as sb:
    blob = bytes(range(256)) * 4096  # 1 MB with every byte value, newlines and NULs included
    for path in ("/tmp/new/dir/blob.bin", "/tmp/it's a \"file\"; $(id)"):  # nested dirs are created; the shell sees nothing special
        sb.put(path, blob)
        assert sb.get(path) == blob, path
    sb.put("/tmp/new/dir/blob.bin", b"short")  # replaced, not appended
    assert sb.get("/tmp/new/dir/blob.bin") == b"short"
    sb.put("/tmp/empty", b"")
    assert sb.get("/tmp/empty") == b""

    for bad in ("/tmp/missing", "/tmp"):  # no such file, a directory
        try:
            sb.get(bad)
        except RuntimeError as e:
            assert "not a file" in str(e), e
        else:
            raise AssertionError(f"read {bad}")
    sb.exec("head -c 5000000 /dev/zero > /tmp/big")
    try:
        sb.get("/tmp/big", max_bytes=1000)
    except ValueError:
        pass
    else:
        raise AssertionError("read a file over max_bytes")
    try:
        sb.put("/proc/nope/x", b"1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("a failed write must raise")
    # directories: a tar through put()/get(); links stay links, owners are reset, what comes out is extracted safely
    import tarfile
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "proj")
        (src / "sub dir").mkdir(parents=True)
        (src / "a.txt").write_text("a")
        (src / "sub dir" / "b'.bin").write_bytes(blob)
        (src / ".hidden").write_text("h")
        (src / "link").symlink_to("/etc/hostname")  # a link on our side must stay a link: no host file travels
        sb.put_dir(src, "/tmp/new dir/proj")
        assert sb.get("/tmp/new dir/proj/sub dir/b'.bin") == blob and sb.get("/tmp/new dir/proj/.hidden") == b"h"
        assert sb.exec("readlink '/tmp/new dir/proj/link'; stat -c %u '/tmp/new dir/proj/a.txt'").stdout.split() == ["/etc/hostname", "0"]
        sb.exec("echo extra > '/tmp/new dir/proj/extra'")
        out = Path(d, "back")
        out.mkdir()
        (out / "keep").write_text("keep")  # merged into, not wiped
        assert sb.get_dir("/tmp/new dir/proj", out) == ["./link"]  # an absolute link points out of `out`: left out, and said so
        assert (out / "a.txt").read_text() == "a" and (out / "sub dir" / "b'.bin").read_bytes() == blob
        assert (out / "extra").read_text() == "extra\n" and (out / "keep").read_text() == "keep" and not (out / "link").exists()
        assert sb.exec("ls /var/tmp | grep -c '^.agvisor-.*tar$'").stdout.strip() == "0"  # no tarballs left behind
        try:
            sb.get_dir("/tmp/new dir/proj", Path(d, "small"), max_bytes=1000)
        except ValueError:
            pass
        else:
            raise AssertionError("read a directory over max_bytes")
        # a sandbox that hands back a hostile tar (here: it replaced `tar`): nothing lands outside the destination
        evil = "#!/usr/bin/env python3\nimport io, sys, tarfile\nt = tarfile.open(sys.argv[2], 'w')\n" \
               "i = tarfile.TarInfo('../escaped'); i.size = 1; t.addfile(i, io.BytesIO(b'x'))\nt.close()\n"
        if sb.exec("command -v python3").returncode == 0:
            sb.put("/usr/local/bin/tar", evil.encode())  # ahead of /usr/bin in PATH
            sb.exec("chmod +x /usr/local/bin/tar")
            try:
                sb.get_dir("/tmp", Path(d, "evil"))
            except tarfile.FilterError:
                pass
            else:
                raise AssertionError("extracted a ../ member")
            assert not Path(d, "escaped").exists()
            assert sb.exec("ls /var/tmp | grep -c '^.agvisor-.*tar$'").stdout.strip() == "0"  # even then, no tarball left behind
        try:
            sb.get_dir("/nonexistent", Path(d, "x"))
        except RuntimeError as e:
            assert "not a directory" in str(e)
        else:
            raise AssertionError("a missing directory must raise")
print("ok")
