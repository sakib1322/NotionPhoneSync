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


def find_phones(key, timeout=1.2, also=()):
    """Asks the local network where your phones are. Returns [(ip, port, device id)]."""
    kid = key_id(key)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found = {}
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
                found[addr[0]] = (addr[0], int(j.get("port") or TCP_PORT), j.get("dev") or addr[0])
        return list(found.values())
    finally:
        s.close()


def find_phone(key, timeout=1.2, also=()):
    got = find_phones(key, timeout, also)
    return (got[0][0], got[0][1]) if got else None


class LanClient:
    """The laptop's side: keeps a link to each of your phones on the same network.

    on_item(meta, link, peer) is called (on that link's thread) for each item a phone sends;
    it must read the item's files from the link and return True when it's applied.
    on_fetch(meta, peer) is called when a phone asks for an offer's files."""

    def __init__(self, get_token, enabled, on_item, log=lambda m: None, on_fetch=lambda msg, peer=None: None,
                 me=lambda: ("", "")):
        self.get_token = get_token
        self.enabled = enabled
        self.on_item = on_item
        self.on_fetch = on_fetch
        self.log = log
        self.me = me  # () -> (device id, device name)
        self.links = {}     # phone device id -> Link
        self.names = {}     # phone device id -> name
        self.peers = {}     # phone device id -> (ip, port)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.acks = {}
        self.acks_lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def connected(self):
        return bool(self.links)

    @property
    def link(self):  # (older callers) any link
        with self.lock:
            return next(iter(self.links.values()), None)

    @property
    def peer(self):
        with self.lock:
            return next(iter(self.peers.values()), None)

    @peer.setter
    def peer(self, v):
        with self.lock:
            self.peers["?"] = v

    def connected_ids(self):
        with self.lock:
            return set(self.links)

    def connected_names(self):
        with self.lock:
            return [self.names.get(i) or "phone" for i in self.links]

    def _run(self):
        while not self.stop.is_set():
            token = self.get_token()
            if not token or not self.enabled():
                self.stop.wait(3)
                continue
            key = key_for(token)
            with self.lock:
                also = [p[0] for p in self.peers.values()]
            for ip, port, dev in find_phones(key, also=also):
                with self.lock:
                    busy = dev in self.links or any(dev == d for d in self.links) or \
                        any(self.peers.get(d, (None,))[0] == ip for d in self.links)
                if not busy:
                    threading.Thread(target=self._connect, args=(key, ip, port, dev), daemon=True).start()
            self.stop.wait(4 if self.links else 2.5)

    def _connect(self, key, ip, port, dev):
        try:
            sock = socket.create_connection((ip, port), timeout=3)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            link = Link(sock, key, initiator=True)
            mid, mname = self.me()
            link.send_msg({"t": "hello", "from": "Laptop", "id": mid, "name": mname, "type": "laptop"})
            sock.settimeout(15)
            hello = link.recv_msg()
            if hello.get("t") != "hello":
                raise ConnectionError("unexpected reply")
        except Exception as e:
            self.log(f"Shared clipboard: found a phone on Wi-Fi but couldn't connect ({e}).")
            return
        pid = hello.get("id") or dev
        name = hello.get("name") or "phone"
        with self.lock:
            if pid in self.links:
                link.close()
                return
            self.links[pid] = link
            self.names[pid] = name
            self.peers[pid] = (ip, port)
            self.peers.pop("?", None)
        self.log(f"Shared clipboard: connected to {name} over Wi-Fi ({ip}) — fast mode.")
        try:
            self._serve(link, pid)
        finally:
            with self.lock:
                if self.links.get(pid) is link:
                    del self.links[pid]
            link.close()
            self.log(f"Shared clipboard: Wi-Fi link to {name} closed; using Notion for it until it's back.")

    def _serve(self, link, pid):
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
            elif t == "fetch":
                # A phone wants the files of an offer: send them from another thread.
                threading.Thread(target=self.on_fetch, args=(msg, pid), daemon=True).start()
            elif t == "item":
                try:
                    link.sock.settimeout(60)
                    ok = self.on_item(msg, link, pid)
                    link.sock.settimeout(15)
                    link.send_msg({"t": "ack", "id": msg.get("id"), "ok": bool(ok)})
                except Exception as e:
                    self.log(f"Shared clipboard: couldn't take an item from a phone over Wi-Fi ({e}).")
                    return

    def _targets(self, to):
        with self.lock:
            if to:
                return [(to, self.links[to])] if to in self.links else []
            return list(self.links.items())

    def note(self, msg, to=None):
        """Sends a small message (offer / gone) to one phone or all. Returns the ids reached."""
        reached = set()
        for pid, link in self._targets(to):
            try:
                link.send_msg(msg)
                reached.add(pid)
            except OSError:
                link.close()
        return reached

    def send(self, kind, text=None, paths=(), for_id=None, to=None, item=None):
        """Sends over Wi-Fi to one phone or all of them. Returns the ids that confirmed it
        (empty = use Notion)."""
        files = [{"name": os.path.basename(p), "size": os.path.getsize(p)} for p in paths]
        total = sum(f["size"] for f in files)
        mid, mname = self.me()
        reached = set()
        for pid, link in self._targets(to):
            iid = os.urandom(8).hex()
            ev = threading.Event()
            with self.acks_lock:
                self.acks[iid] = ev
            try:
                meta = {"t": "item", "id": iid, "kind": kind, "text": text or "", "files": files,
                        "item": item or iid, "from_id": mid, "from_name": mname}
                if for_id:
                    meta["for"] = for_id
                link.send_msg(meta, paths)
                if ev.wait(4 + total / (1024 * 1024)):
                    reached.add(pid)
                else:
                    link.close()  # no answer: drop it so the next item goes through Notion right away
            except OSError:
                link.close()
            finally:
                with self.acks_lock:
                    self.acks.pop(iid, None)
        return reached
