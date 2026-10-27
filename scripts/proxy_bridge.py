#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
proxy_bridge.py —— 本地 HTTP(CONNECT) → SOCKS5 桥

什么时候用
    机器上只有 SOCKS5 代理（例如 127.0.0.1:10808），而 **Git Credential Manager (GCM)
    是 .NET 写的、只支持 HTTP 代理、不认 socks5 方案**（报
    `ServicePointManager 不支持具有 socks5 方案的代理`）。
    在「还没有缓存凭据」的那一次授权里，GCM 必须能联网，这时就需要这层桥：

        python proxy_bridge.py --print-only      # 只启动桥并打印代理地址
        python proxy_bridge.py --push            # 启动桥 + 直接执行 git push

    凭据缓存成功之后，就可以改回 `git config http.proxy socks5://127.0.0.1:10808`，
    后续 push 不再需要桥。

参数
    --socks   SOCKS5 地址，默认 127.0.0.1:10808（也可用环境变量 SOCKS_PROXY）
    --repo    仓库目录，默认当前目录
    --port    本地监听端口，默认自动挑空闲端口
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
import subprocess
import sys
import threading

DEFAULT_SOCKS = os.environ.get("SOCKS_PROXY", "socks5://127.0.0.1:10808")


def parse_socks(url: str):
    s = url.replace("socks5h://", "").replace("socks5://", "")
    host, _, port = s.rpartition(":")
    return host or "127.0.0.1", int(port or 10808)


SOCKS = parse_socks(DEFAULT_SOCKS)


def socks_connect(host: str, port: int) -> socket.socket:
    s = socket.create_connection(SOCKS, timeout=30)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        raise RuntimeError("socks5 握手失败")
    h = host.encode("idna")
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
    rep = s.recv(4)
    if len(rep) < 4 or rep[1] != 0:
        raise RuntimeError("socks5 CONNECT 被拒: %r" % (rep,))
    atyp = rep[3]
    n = 4 if atyp == 1 else 16 if atyp == 4 else s.recv(1)[0]
    s.recv(n + 2)
    return s


def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except Exception:
        pass
    finally:
        for x in (a, b):
            try:
                x.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


def handle(conn):
    try:
        conn.settimeout(30)
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = conn.recv(4096)
            if not d:
                return
            buf += d
        method, target, _ = buf.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
        if method.upper() != "CONNECT":
            conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, port = target.rsplit(":", 1)
        up = socks_connect(host, int(port))
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        conn.settimeout(None)
        threading.Thread(target=pipe, args=(up, conn), daemon=True).start()
        pipe(conn, up)
    except Exception as e:
        print("[bridge] %r" % e, file=sys.stderr, flush=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve(srv):
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=handle, args=(c,), daemon=True).start()


def start(port: int = 0) -> str:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    real = srv.getsockname()[1]
    srv.listen(64)
    threading.Thread(target=serve, args=(srv,), daemon=True).start()
    return "http://127.0.0.1:%d" % real


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 HTTP(CONNECT) → SOCKS5 桥")
    ap.add_argument("--socks", default=DEFAULT_SOCKS)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--print-only", action="store_true", help="只起桥并打印地址，然后常驻")
    ap.add_argument("--push", action="store_true", help="起桥后执行 git push -u origin <branch>")
    ap.add_argument("--branch", default="main")
    a = ap.parse_args()

    global SOCKS
    SOCKS = parse_socks(a.socks)
    proxy = start(a.port)
    print("bridge ready: %s -> socks5://%s:%d" % (proxy, SOCKS[0], SOCKS[1]), flush=True)
    if a.print_only:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            return 0
        return 0

    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(k, None)
    env.update({"GCM_INTERACTIVE": "always", "GIT_TERMINAL_PROMPT": "1"})
    subprocess.run(["git", "config", "--local", "http.proxy", proxy], cwd=a.repo, check=True)
    print("git http.proxy = %s" % proxy, flush=True)
    if not a.push:
        return 0
    print("--- git push（如弹窗，用你的 GitHub 账号登录）---", flush=True)
    rc = subprocess.run(["git", "push", "-u", "origin", a.branch], cwd=a.repo, env=env).returncode
    print("push exit=%d" % rc, flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
