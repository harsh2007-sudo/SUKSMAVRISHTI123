"""
https_proxy.py — TLS-terminating reverse proxy for local/dev use.

Streamlit (Tornado) cannot set custom response headers, so CSP, HSTS,
clickjacking protection and cookie flags have to come from a proxy. Production
uses deploy/nginx.conf.example; this script is the same job for a Windows/dev
box where nginx is not available. Python stdlib only.

    python deploy/https_proxy.py                 # https://127.0.0.1:8443 -> 127.0.0.1:8501
    python deploy/https_proxy.py --hsts           # opt-in HSTS (see below)

What it does
* Terminates TLS with a self-signed certificate (auto-generated on first run
  into deploy/certs/, owner-only).
* Injects security headers on every response: CSP, X-Content-Type-Options,
  X-Frame-Options, Referrer-Policy, Permissions-Policy, COOP (never duplicating
  one the upstream already sent).
* Hardens Set-Cookie: adds Secure + SameSite=Lax, plus HttpOnly for every
  cookie except st_xsrf (Streamlit's own JS must read that one for its XSRF
  token, so it stays readable).
* Adds X-Forwarded-Proto/For/Host so app-side logic (e.g. rate-limit client
  keys with SV_TRUST_PROXY=1) sees the real client.
* Passes WebSocket upgrades through untouched, so the Streamlit app works.

HSTS is OFF by default on purpose: a Strict-Transport-Security header for
localhost would make the browser force *every* http://localhost:8501 request to
https and break the plain dev URL. Only enable --hsts behind a real domain with
a trusted certificate.

Run the app itself bound to loopback while this is in front of it:
    streamlit run App.py --server.address 127.0.0.1 --server.port 8501
"""

from __future__ import annotations

import argparse
import os
import select
import socket
import ssl
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.path.join(HERE, "certs")
DEFAULT_CERT = os.path.join(CERT_DIR, "localhost.pem")
DEFAULT_KEY = os.path.join(CERT_DIR, "localhost.key")

MAX_HEAD = 64 * 1024            # request/response head cap
IDLE_HTTP = 60.0                # seconds of silence before an HTTP conn closes
IDLE_WS = 300.0                 # long-lived Streamlit websocket

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; object-src 'none'; "
        "frame-ancestors 'self'; form-action 'self'; img-src 'self' data: blob:; "
        "font-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "connect-src 'self' ws: wss:; frame-src 'self'; media-src 'self' blob:"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
HSTS_HEADER = "Strict-Transport-Security: max-age=15552000; includeSubDomains"
# Streamlit reads/writes st_xsrf from JavaScript, so it must stay readable.
JS_READ_COOKIES = {b"st_xsrf"}

DEBUG = os.environ.get("SV_PROXY_DEBUG", "").strip().lower() in {"1", "true", "yes"}


def _dbg(*args) -> None:
    if DEBUG:
        print("[https_proxy]", *args, flush=True)


# --------------------------------------------------------------------------- #
# Certificate
# --------------------------------------------------------------------------- #
def ensure_certificate(cert_path: str, key_path: str) -> None:
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
        "-keyout", key_path, "-out", cert_path,
        "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or str(exc)
        raise SystemExit(
            "Could not generate a self-signed certificate with openssl.\n"
            f"  {detail!r}\n"
            "Install openssl, or pass --cert/--key pointing at existing files."
        )
    try:
        os.chmod(key_path, 0o600)
    except Exception:
        pass
    print(f"[https_proxy] generated self-signed certificate: {cert_path}")


# --------------------------------------------------------------------------- #
# Header helpers
# --------------------------------------------------------------------------- #
def _split_head(head: bytes):
    lines = head.split(b"\r\n")
    while lines and lines[-1] == b"":     # drop the blank-line terminator
        lines.pop()
    return lines[0], lines[1:]


def _headers_of(lines):
    """[(lower_name, value_bytes)] for the header lines."""
    out = []
    for line in lines:
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        out.append((name.strip().lower(), value.strip()))
    return out


def harden_cookie(raw: bytes) -> bytes:
    """Add Secure / SameSite / HttpOnly flags that are not already present."""
    parts = raw.split(b";")
    attrs = {p.strip().split(b"=", 1)[0].lower() for p in parts[1:]}
    name = parts[0].strip().split(b"=", 1)[0].lower()
    extra = b""
    if b"secure" not in attrs:
        extra += b"; Secure"
    if b"samesite" not in attrs:
        extra += b"; SameSite=Lax"
    if b"httponly" not in attrs and name not in JS_READ_COOKIES:
        extra += b"; HttpOnly"
    return raw + extra


def rewrite_request_head(head: bytes, client_ip: str, host: str,
                         is_ws: bool) -> bytes:
    """Forwarded-proxy headers; HTTP requests are closed after one response."""
    first, lines = _split_head(head)
    out = [first]
    for line in lines:
        low = line.strip().lower()
        if not is_ws and low.startswith(b"connection:"):
            continue                       # we replace it below
        out.append(line)
    out.append(f"X-Forwarded-Proto: https".encode())
    out.append(f"X-Forwarded-For: {client_ip}".encode())
    out.append(f"X-Forwarded-Host: {host}".encode())
    if not is_ws:
        out.append(b"Connection: close")
    return b"\r\n".join(out) + b"\r\n\r\n"   # terminate the head properly


def rewrite_response_head(head: bytes, hsts: bool) -> bytes:
    """Inject security headers + hardened cookies; hide the upstream banner."""
    first, lines = _split_head(head)
    out = [first]
    seen = set()
    for line in lines:
        if b":" not in line:
            out.append(line)
            continue
        name, _, value = line.partition(b":")
        low = name.strip().lower()
        if low == b"server":               # do not advertise Tornado/version
            continue
        if low == b"connection":
            continue                       # replaced below
        if low == b"set-cookie":
            out.append(b"Set-Cookie: " + harden_cookie(value.strip()))
            seen.add(low)
            continue
        seen.add(low)
        out.append(line)
    for name, value in SECURITY_HEADERS.items():
        if name.lower().encode() not in seen:
            out.append(f"{name}: {value}".encode())
    if hsts and b"strict-transport-security" not in seen:
        out.append(HSTS_HEADER.encode())
    out.append(b"Connection: close")
    return b"\r\n".join(out)


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #
def recv_head(sock: socket.socket) -> bytes:
    """Read until CRLF CRLF (returns everything including the terminator)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_HEAD:
            raise ValueError("head too large")
    idx = buf.find(b"\r\n\r\n")
    return buf[:idx + 4] if idx >= 0 else buf


def send_all(sock: socket.socket, data: bytes) -> None:
    view = memoryview(data)
    while view:
        sent = sock.send(view)
        if sent == 0:
            raise BrokenPipeError("peer closed")
        view = view[sent:]


def pump(client: ssl.SSLSocket, upstream: socket.socket,
         rewrite: bool, hsts: bool, idle: float) -> None:
    """Relay bytes both ways; rewrite the (first) response head when asked."""
    pending = b""
    head_done = not rewrite
    client_open, upstream_open = True, True
    last = time.time()

    while True:
        rlist = []
        if client_open:
            rlist.append(client)
        if upstream_open:
            rlist.append(upstream)
        if not rlist:
            return
        try:
            readable, _, _ = select.select(rlist, [], [], 10.0)
        except (OSError, ValueError):
            return
        if not readable:
            if time.time() - last > idle:
                return
            continue

        for sock in readable:
            try:
                data = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                continue               # TLS control record only — not EOF
            except (ssl.SSLError, OSError) as e:
                _dbg("recv error:", type(e).__name__, str(e))
                data = b""
            if not data:
                if sock is client:
                    client_open = False
                    try:
                        upstream.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                else:
                    upstream_open = False
                continue
            last = time.time()
            try:
                if sock is client:
                    send_all(upstream, data)
                elif head_done:
                    send_all(client, data)
                else:
                    pending += data
                    idx = pending.find(b"\r\n\r\n")
                    if idx >= 0:
                        head, tail = pending[:idx], pending[idx + 4:]
                        send_all(client, rewrite_response_head(head, hsts) + b"\r\n\r\n" + tail)
                        pending = b""
                        head_done = True
                    elif len(pending) > MAX_HEAD:
                        return
            except (OSError, ssl.SSLError) as e:
                _dbg("pump forward failed:", type(e).__name__, str(e))
                return
        if not upstream_open:
            return


# --------------------------------------------------------------------------- #
# Connection handling
# --------------------------------------------------------------------------- #
class Proxy:
    def __init__(self, args) -> None:
        self.args = args
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(args.cert, args.key)
        self.tls = ctx

    def handle(self, client: socket.socket, addr) -> None:
        client_ip = addr[0] if addr else "127.0.0.1"
        try:
            client.settimeout(30)
            tls = self.tls.wrap_socket(client, server_side=True)
        except (ssl.SSLError, OSError):
            try:
                client.close()
            except OSError:
                pass
            return

        upstream = None
        try:
            tls.settimeout(30)
            head = recv_head(tls)
            if not head:
                return
            first, lines = _split_head(head)
            if b" HTTP/1." not in first:      # request line, e.g. "GET / HTTP/1.1"
                _dbg("bad request line:", first[:80])
                return
            _dbg("<-", first.decode("latin-1"))
            hdrs = _headers_of(lines)
            upgrade = b" ".join(v for k, v in hdrs if k == b"upgrade").lower()
            is_ws = b"websocket" in upgrade

            rewritten = rewrite_request_head(
                head, client_ip,
                self.args.forwarded_host or "localhost",
                is_ws,
            )

            try:
                upstream = socket.create_connection(
                    (self.args.upstream_host, self.args.upstream_port), timeout=15)
            except OSError:
                send_all(tls, (
                    b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n"
                    b"Content-Length: 0\r\n\r\n"
                ))
                return

            upstream.settimeout(None)
            tls.settimeout(None)
            send_all(upstream, rewritten)
            _dbg("-> upstream head", len(rewritten), "bytes, ws=", is_ws)

            if is_ws:
                pump(tls, upstream, rewrite=False, hsts=self.args.hsts, idle=IDLE_WS)
            else:
                pump(tls, upstream, rewrite=True, hsts=self.args.hsts, idle=IDLE_HTTP)
        except (OSError, ssl.SSLError, ValueError):
            try:
                send_all(tls, (
                    b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n"
                    b"Content-Length: 0\r\n\r\n"
                ))
            except Exception:
                pass
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            for sock in (upstream, tls):
                try:
                    if sock is not None:
                        sock.close()
                except OSError:
                    pass

    def serve(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.args.listen_host, self.args.listen_port))
        srv.listen(128)
        print(f"[https_proxy] https://{self.args.listen_host}:{self.args.listen_port}"
              f" -> http://{self.args.upstream_host}:{self.args.upstream_port}"
              f"  (HSTS {'on' if self.args.hsts else 'off'})", flush=True)
        try:
            while True:
                try:
                    conn, addr = srv.accept()
                except KeyboardInterrupt:
                    break
                threading.Thread(target=self.handle, args=(conn, addr),
                                 daemon=True).start()
        finally:
            srv.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8443)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=8501)
    parser.add_argument("--cert", default=DEFAULT_CERT)
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument("--forwarded-host", default="",
                        help="Host sent to the upstream (defaults to localhost)")
    parser.add_argument("--hsts", action="store_true",
                        help="Send HSTS — only for a real domain with a trusted cert")
    args = parser.parse_args(argv)

    ensure_certificate(args.cert, args.key)
    Proxy(args).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
