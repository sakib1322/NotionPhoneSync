"""Direct Wi-Fi link for the shared clipboard.

When the phone and laptop are on the same network, copied items go straight from one to
the other over the local network (much faster than going through Notion). Notion stays as
the fallback.

The laptop always dials the phone (so Windows Firewall never has to let anything in):
  1. it finds the phone with a small UDP "where are you?" broadcast (port 47393),
  2. opens one TCP connection to it and keeps it open with pings,
  3. both sides then send items over that connection, either way.

Everything on the wire is encrypted and authenticated with AES-256-GCM, using a key both
apps derive from the Notion secret they share. Each connection mixes in fresh random
values from both sides, so old traffic can't be replayed.
"""
import hashlib
import json
import os
import socket
import struct
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

TCP_PORT = 47392
UDP_PORT = 47393
MAGIC = b"NSL1"
CHUNK = 1024 * 1024
MAX_FRAME = CHUNK + 64
MAX_LAN_FILE = 4 * 1024 * 1024 * 1024  # over Wi-Fi, files up to 4 GB


def key_for(token):
    return hashlib.sha256(("notion-sync-lan-v1|" + token).encode()).digest()


def key_id(key):
    """Short public fingerprint so devices only connect to their own partner."""
    return hashlib.sha256(b"id|" + key).hexdigest()[:16]


def _recvn(sock, n):
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError("closed")
        buf += part
    return bytes(buf)


class Link:
    """One encrypted connection. `initiator` is the laptop (the side that dialled)."""

    def __init__(self, sock, key, initiator):
        self.sock = sock
        self.aes = AESGCM(key)
        self.wlock = threading.Lock()
        mine = os.urandom(16)
        sock.sendall(MAGIC + mine)
        head = _recvn(sock, 20)
        if head[:4] != MAGIC:
            raise ConnectionError("not a Notion Sync device")
        theirs = head[4:]
        phone_n, laptop_n = (theirs, mine) if initiator else (mine, theirs)
        self.sid = phone_n + laptop_n
        self.out_tag = b"L" if initiator else b"P"
        self.in_tag = b"P" if initiator else b"L"
        self.out_ctr = 0
        self.in_ctr = 0

    def _aad(self, tag, ctr):
        return self.sid + tag + struct.pack(">Q", ctr)

    def _send_frame(self, data):
        nonce = os.urandom(12)
        ct = self.aes.encrypt(nonce, data, self._aad(self.out_tag, self.out_ctr))
        self.out_ctr += 1
        self.sock.sendall(struct.pack(">I", len(ct) + 12) + nonce + ct)

    def recv_frame(self):
        (n,) = struct.unpack(">I", _recvn(self.sock, 4))
        if n < 28 or n > MAX_FRAME + 28:
            raise ConnectionError("bad frame")
        raw = _recvn(self.sock, n)
        data = self.aes.decrypt(raw[:12], raw[12:], self._aad(self.in_tag, self.in_ctr))
        self.in_ctr += 1
        return data

    def send_msg(self, meta, paths=(), progress=None):
        """Sends a JSON message, followed by the bytes of each file (in order)."""
        with self.wlock:
            self._send_frame(json.dumps(meta).encode())
            for p in paths:
                with open(p, "rb") as f:
                    while True:
                        b = f.read(CHUNK)
                        if not b:
                            break
                        self._send_frame(b)
                        if progress:
                            progress(len(b))

    def recv_msg(self):
        return json.loads(self.recv_frame().decode())

    def recv_files(self, files, dest_for):
        """Reads the bytes for `files` ([{name, size}]); returns the saved paths."""
        out = []
        for f in files:
            dest = dest_for(f["name"])
            left = int(f["size"])
            with open(dest, "wb") as fh:
                while left > 0:
                    b = self.recv_frame()
                    if len(b) > left:
                        raise ConnectionError("too much data")
                    fh.write(b)
                    left -= len(b)
            out.append(dest)
        return out

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _local_ipv4s():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # the address used to reach the internet / the router
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return [i for i in ips if not i.startswith("127.") and not i.startswith("169.254.")]


def find_phone(key, timeout=1.2, also=()):
    """Asks the local network where the phone is. Returns (ip, port) or None."""
    kid = key_id(key)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", 0))
        probe = json.dumps({"t": "probe", "id": kid}).encode()
        targets = {"255.255.255.255"} | set(also)
        for ip in _local_ipv4s():
            a = ip.split(".")
            targets.add(".".join(a[:3] + ["255"]))
        for t in targets:
            try:
                s.sendto(probe, (t, UDP_PORT))
            except OSError:
                pass
        end = time.time() + timeout
        while time.time() < end:
            s.settimeout(max(0.05, end - time.time()))
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                break
            except OSError:
                continue
            try:
                j = json.loads(data.decode())
            except ValueError:
                continue
            if j.get("t") == "here" and j.get("id") == kid:
                return addr[0], int(j.get("port") or TCP_PORT)
        return None
    finally:
        s.close()


class LanClient:
    """The laptop's side: keeps a link to the phone whenever it's on the same network.

    on_item(meta, link) is called (on the link's thread) for each item the phone sends;
    it must read the item's files from the link and return True when it's applied."""

    def __init__(self, get_token, enabled, on_item, log=lambda m: None):
        self.get_token = get_token
        self.enabled = enabled
        self.on_item = on_item
        self.log = log
        self.link = None
        self.peer = None
        self.stop = threading.Event()
        self.acks = {}
        self.acks_lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def connected(self):
        return self.link is not None

    def _run(self):
        backoff = 1.0
        while not self.stop.is_set():
            token = self.get_token()
            if not token or not self.enabled():
                self.stop.wait(3)
                continue
            key = key_for(token)
            found = find_phone(key, also=[self.peer[0]] if self.peer else [])
            if not found:
                self.stop.wait(min(backoff, 5))
                backoff = min(backoff * 1.5, 5)
                continue
            backoff = 1.0
            try:
                sock = socket.create_connection(found, timeout=3)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                link = Link(sock, key, initiator=True)
                link.send_msg({"t": "hello", "from": "Laptop"})
                sock.settimeout(15)
                hello = link.recv_msg()
                if hello.get("t") != "hello":
                    raise ConnectionError("unexpected reply")
            except Exception as e:
                self.log(f"Shared clipboard: found the phone on Wi-Fi but couldn't connect ({e}).")
                self.stop.wait(3)
                continue
            self.peer = found
            self.link = link
            self.log(f"Shared clipboard: connected to the phone over Wi-Fi ({found[0]}) — fast mode.")
            self._serve(link)
            self.link = None
            link.close()
            self.log("Shared clipboard: Wi-Fi link to the phone closed; using Notion until it's back.")

    def _serve(self, link):
        missed = 0
        while not self.stop.is_set() and self.enabled():
            try:
                msg = link.recv_msg()
            except socket.timeout:
                missed += 1
                if missed >= 3:
                    return
                try:
                    link.send_msg({"t": "ping"})
                except OSError:
                    return
                continue
            except Exception:
                return
            missed = 0
            t = msg.get("t")
            if t == "ping":
                try:
                    link.send_msg({"t": "pong"})
                except OSError:
                    return
            elif t == "ack":
                with self.acks_lock:
                    ev = self.acks.get(msg.get("id"))
                if ev:
                    ev.set()
            elif t == "item":
                try:
                    link.sock.settimeout(60)
                    ok = self.on_item(msg, link)
                    link.sock.settimeout(15)
                    link.send_msg({"t": "ack", "id": msg.get("id"), "ok": bool(ok)})
                except Exception as e:
                    self.log(f"Shared clipboard: couldn't take an item from the phone over Wi-Fi ({e}).")
                    return

    def send(self, kind, text=None, paths=()):
        """Sends over Wi-Fi. True if the phone confirmed it; False means use Notion."""
        link = self.link
        if link is None:
            return False
        iid = os.urandom(8).hex()
        files = [{"name": os.path.basename(p), "size": os.path.getsize(p)} for p in paths]
        ev = threading.Event()
        with self.acks_lock:
            self.acks[iid] = ev
        try:
            link.send_msg({"t": "item", "id": iid, "kind": kind, "text": text or "", "files": files}, paths)
            total = sum(f["size"] for f in files)
            if ev.wait(4 + total / (1024 * 1024)):
                return True
            link.close()  # no answer: drop the link so the next item goes through Notion right away
            return False
        except OSError:
            link.close()
            return False
        finally:
            with self.acks_lock:
                self.acks.pop(iid, None)
