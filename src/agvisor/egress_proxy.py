"""Allowlist HTTP(S) proxy that agvisor runs in a container next to an `egress=` sandbox. Stdlib only, one file: agvisor
passes this source to `python -c` in a stock python image, and imports it to validate `egress=` entries.

Environment: AGVISOR_ALLOW (comma-separated entries), AGVISOR_TOKEN (the proxy password), AGVISOR_WATCH (optional: a
name to resolve every 5 s; once it has resolved and then stops resolving twice, this process exits).

Entry grammar, `host[:port]`: port defaults to 80 and 443, `*` = any port.
  example.com      exactly that name         *.example.com   its subdomains, not example.com itself
  1.2.3.4          an IPv4 address           10.0.0.0/24     an IPv4 network
A name only gets through if it resolves to public addresses: a name is not a way to reach 10.x, the host or the cloud
metadata endpoint. Reaching a private address takes an entry that says so (an IP or a network)."""
import asyncio
import base64
import hmac
import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit

PORT = 3128
_MAX = 256  # concurrent connections; the container's memory is the real limit
_NAME = re.compile(r"(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


def parse_rules(entries):
    """[(kind, value, ports)]: kind "net" (value an ip_network) or "name"; ports a tuple, or None for any port."""
    rules = []
    for e in entries:
        host, sep, port = e.lower().partition(":")
        if sep and port != "*" and not (re.fullmatch(r"[0-9]{1,5}", port) and 0 < int(port) < 65536):
            raise ValueError(f"invalid egress entry {e!r}: bad port")
        ports = None if port == "*" else (int(port),) if sep else (80, 443)
        if re.fullmatch(r"[0-9./]+", host):
            try:
                rules.append(("net", ipaddress.ip_network(host, strict=False), ports))
            except ValueError:
                raise ValueError(f"invalid egress entry {e!r}: not an IPv4 address or network") from None
        elif _NAME.fullmatch(host) and len(host) <= 253:
            rules.append(("name", host, ports))
        else:
            raise ValueError(f"invalid egress entry {e!r}: expected host[:port], *.host[:port], an IPv4 address or network")
    return rules


def match(rules, host, port):
    """"net" or "name" if a rule lets (host, port) through, else None. `host` is lowercase, without a trailing dot."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    for kind, val, ports in rules:
        if ports is not None and port not in ports:
            continue
        if ip is not None:
            if kind == "net" and ip in val:  # an address of the other IP version is never "in"
                return "net"
        elif kind == "name" and (host == val or val.startswith("*.") and host.endswith(val[1:])):
            return "name"


def _public(addr):
    ip = ipaddress.ip_address(addr.split("%")[0])
    return (getattr(ip, "ipv4_mapped", None) or ip).is_global  # ::ffff:10.0.0.1 is 10.0.0.1


class _Denied(Exception):
    pass


async def _dial(host, port, kind):
    """Connect to what we checked, not to what a second lookup might say: no DNS rebinding."""
    if kind == "net":
        addrs = [host]
    else:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addrs = [i[4][0] for i in infos if _public(i[4][0])]
        if not addrs:
            raise _Denied(f"{host} resolves to no public address")
    err = OSError("no address")
    for a in addrs:
        try:
            return await asyncio.wait_for(asyncio.open_connection(a, port), 10)
        except OSError as e:  # includes TimeoutError
            err = e
    raise err


async def _reply(w, code, why, body="", extra=""):
    b = body.encode()
    w.write(f"HTTP/1.1 {code} {why}\r\n{extra}Content-Type: text/plain\r\nContent-Length: {len(b)}\r\nConnection: close\r\n\r\n".encode() + b)
    await w.drain()


async def _pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
        if w.can_write_eof():
            w.write_eof()  # the peer half-closed: pass it on, the answer may still be coming
    except OSError:
        w.close()


async def serve(rules, token, host="0.0.0.0", port=PORT):
    want = b"Basic " + base64.b64encode(f"agvisor:{token}".encode())
    live = 0

    async def handle(cr, cw):
        nonlocal live
        live += 1
        try:
            if live > _MAX:
                return await _reply(cw, 503, "Service Unavailable", "too many connections")
            head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 15)
            first, *lines = head[:-4].decode("latin-1").split("\r\n")
            hdr = [(l.partition(":")[0].strip().lower(), l.partition(":")[2].strip(), l) for l in lines]
            if not hmac.compare_digest(next((v for k, v, _ in hdr if k == "proxy-authorization"), "").encode("latin-1"), want):
                return await _reply(cw, 407, "Proxy Authentication Required", extra='Proxy-Authenticate: Basic realm="agvisor"\r\n')
            try:
                method, target, version = first.split(" ")
                if method == "CONNECT":
                    host, _, p = target.rpartition(":")
                    host, port, path = host.strip("[]"), int(p), None
                else:
                    u = urlsplit(target)
                    if u.scheme != "http":
                        raise ValueError("a proxy is asked for http:// URLs or CONNECT")
                    host, port = u.hostname, u.port or 80
                    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                host = host.lower().rstrip(".")
                if not (_NAME.fullmatch(host) or ipaddress.ip_address(host)):  # a literal address raises ValueError if it isn't one
                    raise ValueError("bad host")
            except (ValueError, AttributeError):
                return await _reply(cw, 400, "Bad Request", "agvisor egress proxy: send CONNECT host:port or an absolute http:// URL")
            kind = match(rules, host, port)
            try:
                if not kind:
                    raise _Denied(f"{host}:{port} is not in this sandbox's egress allowlist")
                ur, uw = await _dial(host, port, kind)
            except _Denied as e:
                print("deny", host, port, flush=True)
                return await _reply(cw, 403, "Forbidden", f"agvisor egress: {e}")
            except OSError as e:
                print("fail", host, port, e, flush=True)
                return await _reply(cw, 502, "Bad Gateway", f"agvisor egress: cannot connect to {host}:{port}: {e}")
            print("allow", host, port, flush=True)
            if path is None:
                cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:  # one request per connection: `Connection: close`, so a later request can't be sent anywhere else
                drop = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}
                uw.write(f"{method} {path} {version}\r\n".encode("latin-1")
                         + "".join(f"{l}\r\n" for k, _, l in hdr if k not in drop).encode("latin-1") + b"Connection: close\r\n\r\n")
            await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))  # ponytail: no idle timeout on a tunnel
            uw.close()
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass  # a client that vanished or sent junk
        finally:
            live -= 1
            cw.close()

    return await asyncio.start_server(handle, host, port, limit=2**14)


async def _watch(name):
    loop, seen, misses, give_up = asyncio.get_running_loop(), False, 0, 0
    give_up = loop.time() + 1800  # the sandbox never showed up (its start failed): don't linger
    while True:
        await asyncio.sleep(5)
        try:
            await loop.getaddrinfo(name, None)
            seen, misses = True, 0
        except OSError:
            misses += 1
        if (seen and misses >= 2) or (not seen and loop.time() > give_up):
            os._exit(0)


async def main():
    server = await serve(parse_rules(os.environ["AGVISOR_ALLOW"].split(",")), os.environ["AGVISOR_TOKEN"])
    if os.environ.get("AGVISOR_WATCH"):
        asyncio.create_task(_watch(os.environ["AGVISOR_WATCH"]))
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
