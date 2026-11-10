"""Runs INSIDE the sandbox, stdlib only: mcp_server.python() sends this file as `python3 -c "$S" <seconds>` with the code on
stdin. The code runs in one long-lived interpreter, so variables, imports and functions survive between calls.
`serve` is that interpreter (the client starts it when it isn't up: a unix socket, one call at a time); `kill` stops it."""
import ast
import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
import traceback

DIR = os.environ.get("AGVISOR_PYDIR", "/var/tmp/.agvisor-py")  # ponytail: the env var is only for the tests
SOCK, PID = f"{DIR}/sock", f"{DIR}/pid"


def _connect():
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(SOCK)
    except OSError:  # no socket, or a stale one from a server that died
        s.close()
        return None
    return s


class _Out:  # print() goes straight back to the caller: nothing is buffered, so a huge print can't fill memory here
    def __init__(self, conn):
        self.conn = conn

    def write(self, text):
        self.conn.sendall(text.encode(errors="replace"))
        return len(text)

    def flush(self):
        pass


def serve():
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(SOCK)
    srv.listen()
    with open(PID, "w") as f:
        f.write(str(os.getpid()))
    g, secs, busy = {"__name__": "__main__"}, 0, False

    def alarm(*_):
        if busy:  # not while we are between two calls
            raise TimeoutError(f"timed out after {secs} s (the session and its variables are kept)")

    signal.signal(signal.SIGALRM, alarm)
    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                head, _, code = b"".join(iter(lambda: conn.recv(65536), b"")).partition(b"\n")
                secs, out = int(head), _Out(conn)
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    try:  # like a notebook cell: the value of a last expression is printed
                        tree = ast.parse(code.decode(errors="replace"), "<python>")
                        last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
                        body = compile(tree, "<python>", "exec")
                        tail = compile(ast.Expression(last.value), "<python>", "eval") if last else None
                    except SyntaxError as e:
                        print("".join(traceback.format_exception_only(type(e), e)), end="")
                        continue
                    try:
                        busy = True
                        signal.setitimer(signal.ITIMER_REAL, secs)
                        exec(body, g)
                        if tail and (v := eval(tail, g)) is not None:
                            print(repr(v))
                    except BaseException as e:  # SystemExit too: it ends the cell, not the session
                        traceback.print_exception(type(e), e, e.__traceback__.tb_next)  # tb_next: skip this file's frame
                    finally:
                        busy = False
                        signal.setitimer(signal.ITIMER_REAL, 0)
            except (OSError, ValueError):  # the caller went away, or sent nonsense: next call
                pass


def client(secs):
    code = sys.stdin.buffer.read()
    s = _connect()
    if s is None:
        print("[new python session: variables from earlier calls are gone]", flush=True)
        subprocess.Popen([sys.executable, "-c", os.environ["S"], "serve"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            time.sleep(0.1)
            if s := _connect():
                break
        else:
            sys.exit("could not start the python session")
    s.sendall(f"{secs}\n".encode() + code)
    s.shutdown(socket.SHUT_WR)
    while chunk := s.recv(65536):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()


def kill():
    with contextlib.suppress(OSError, ValueError):  # ponytail: trusts the pid file; hitting a recycled pid needs a wrap-around
        os.kill(int(open(PID).read()), signal.SIGKILL)  # inside one container, after the server died by itself
    for p in (SOCK, PID):
        with contextlib.suppress(OSError):
            os.unlink(p)


if __name__ == "__main__":
    {"serve": serve, "kill": kill}.get(sys.argv[1], lambda: client(int(sys.argv[1])))()
