"""Live chat reader: polls /webcast/im/fetch/ (protobuf) like TikTok LIVE Studio does.

Field numbers come from LIVE Studio's generated protobuf code (proto.webcast.im.*):
  Response     1 messages[]  2 cursor  3 fetch_interval  5 internal_ext
  Message      1 method      2 payload
  ChatMessage  2 user        3 content
  GiftMessage  2 gift_id 4 group_count 5 repeat_count 7 user 9 repeat_end 11 group_id
               15 gift (GiftStruct: 5 id, 11 type (1 = streakable), 12 diamonds, 16 name)
  MemberMessage 2 user  10 action (1 = joined)
  SocialMessage 2 user  4 action (1 = follow, 3 = share)
  LikeMessage   2 count 5 user
  User          1 id  3 nickname  38 display_id (@username)
"""
import base64
import gzip
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import zlib
from dataclasses import dataclass

from .api import USER_AGENT, ApiError, NotLoggedIn


# ------------------------------------------------------------------ minimal protobuf reader
def _varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("bad varint")


def pb_decode(buf):
    """bytes -> {field_number: [values]} (varints as int, length-delimited as bytes)."""
    out = {}
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wt = key >> 3, key & 7
        if wt == 0:
            val, i = _varint(buf, i)
        elif wt == 1:
            val, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            val, i = bytes(buf[i:i + ln]), i + ln
        elif wt == 5:
            val, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.setdefault(field, []).append(val)
    return out


def _first(d, f, default=None):
    v = d.get(f)
    return v[0] if v else default


def _str(d, f):
    v = _first(d, f, b"")
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""


def _msg(d, f):
    v = _first(d, f)
    try:
        return pb_decode(v) if isinstance(v, bytes) else {}
    except (ValueError, IndexError):
        return {}


# ------------------------------------------------------------------ events
@dataclass
class ChatEvent:
    kind: str          # chat | gift | join | follow | share | like
    nickname: str
    username: str
    text: str = ""
    gift: str = ""
    count: int = 0
    diamonds: int = 0
    total: int = 0     # likes: room total so far
    streak_key: str = ""   # gifts: same key while a combo is running
    streaking: bool = False  # gifts: combo still running (count will grow)


def _user(d):
    return _str(d, 3), _str(d, 38)


def parse_message(method, payload):
    try:
        p = pb_decode(payload)
    except (ValueError, IndexError):
        return None
    if method == "WebcastChatMessage":
        nick, uname = _user(_msg(p, 2))
        return ChatEvent("chat", nick, uname, text=_str(p, 3))
    if method == "WebcastGiftMessage":
        gift = _msg(p, 15)
        user = _msg(p, 7)
        nick, uname = _user(user)
        # "streakable" gifts (type 1, e.g. Rose) arrive repeatedly with a growing repeat_count while the
        # viewer keeps tapping; repeat_end = 1 marks the last one.
        streakable = _first(gift, 11, 0) == 1
        count = int(_first(p, 5, 0) or _first(p, 4, 0) or 1)
        key = f"{_first(user, 1, 0)}:{_first(gift, 5, 0) or _first(p, 2, 0)}:{_first(p, 11, 0)}"
        return ChatEvent("gift", nick, uname, gift=_str(gift, 16) or "gift", count=max(1, count),
                         diamonds=int(_first(gift, 12, 0) or 0), streak_key=key,
                         streaking=streakable and not _first(p, 9, 0))
    if method == "WebcastMemberMessage":
        if _first(p, 10, 0) != 1:
            return None
        nick, uname = _user(_msg(p, 2))
        return ChatEvent("join", nick, uname)
    if method == "WebcastSocialMessage":
        action = _first(p, 4, 0)
        kind = {1: "follow", 3: "share"}.get(action)
        if not kind:
            return None
        nick, uname = _user(_msg(p, 2))
        return ChatEvent(kind, nick, uname)
    if method == "WebcastLikeMessage":
        nick, uname = _user(_msg(p, 5))
        return ChatEvent("like", nick, uname, count=int(_first(p, 2, 0) or 0), total=int(_first(p, 3, 0) or 0))
    return None


def parse_response(raw):
    """-> (events, cursor, internal_ext, interval_seconds)"""
    r = pb_decode(raw)
    events = []
    for m in r.get(1, []):
        try:
            md = pb_decode(m)
        except (ValueError, IndexError):
            continue
        ev = parse_message(_str(md, 1), _first(md, 2, b""))
        if ev:
            events.append(ev)
    interval = (_first(r, 3, 1000) or 1000) / 1000
    return events, _str(r, 2), _str(r, 5), min(max(interval, 0.5), 5.0)


# ------------------------------------------------------------------ protobuf writer (PushFrame only)
def _enc_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _enc_field(field, value):
    if isinstance(value, int):
        return _enc_varint(field << 3) + _enc_varint(value)
    if isinstance(value, str):
        value = value.encode()
    return _enc_varint(field << 3 | 2) + _enc_varint(len(value)) + value


def push_frame(payload_type, payload=b"", logid=0):
    """PushFrame: 2 logid, 7 payload_type, 8 payload"""
    out = b""
    if logid:
        out += _enc_field(2, logid)
    out += _enc_field(7, payload_type)
    if payload:
        out += _enc_field(8, payload)
    return out


# ------------------------------------------------------------------ TLS websocket (binary frames)
class _WebSocket:
    def __init__(self, url, headers, timeout=15):
        u = urllib.parse.urlsplit(url)
        raw = socket.create_connection((u.hostname, u.port or 443), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + ("?" + u.query if u.query else "")
        lines = [f"GET {path} HTTP/1.1", f"Host: {u.hostname}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("chat websocket handshake failed")
            buf += chunk
        status = buf.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise ConnectionError("chat websocket refused: " + status.decode("latin-1", "replace"))
        self._buf = buf.split(b"\r\n\r\n", 1)[1]
        self._wlock = threading.Lock()

    def settimeout(self, t):
        self.sock.settimeout(t)

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("chat websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """Next complete binary/text message (handles fragmentation, ping/pong)."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            op = b1 & 0x0F
            if op == 8:
                raise ConnectionError("chat websocket closed by server")
            if op == 9:
                self._send(0xA, data)
                continue
            if op == 0xA:
                continue
            parts.append(data)
            if b1 & 0x80:
                return b"".join(parts)

    def _send(self, opcode, data):
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        with self._wlock:
            self.sock.sendall(hdr + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def send_binary(self, data):
        self._send(0x2, data)

    def close(self):
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ reader
IM_SDK_VERSION = "1.3.0"


class ChatReader:
    """Reads the LIVE chat in a thread, like LIVE Studio: one HTTP fetch for the push server,
    then a websocket. on_events(list[ChatEvent]) and on_error(exc) are called from that thread."""

    def __init__(self, client, room_id, on_events, on_error=None):
        self.client = client
        self.room_id = room_id
        self.on_events = on_events
        self.on_error = on_error or (lambda e: None)
        self._stop = threading.Event()
        self._ws = None
        self._thread = threading.Thread(target=self._run, name="livebridge-chat", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        ws = self._ws
        if ws:
            ws.close()

    def _emit(self, events):
        if events and not self._stop.is_set():
            self.on_events(events)

    def _run(self):
        failures = 0
        first = True
        while not self._stop.is_set():
            try:
                raw = self.client.fetch_chat(self.room_id)
                r = pb_decode(raw)
                events, cursor, ext, _ = parse_response(raw)
                # the first fetch replays recent history; keep only chat/gifts from it
                self._emit([e for e in events if e.kind in ("chat", "gift")] if first else events)
                first = False
                push = _str(r, 10)
                if not push:
                    raise ConnectionError("TikTok didn't provide a chat server")
                routes = {}
                for kv in r.get(7, []):
                    try:
                        m = pb_decode(kv)
                    except (ValueError, IndexError):
                        continue
                    routes[_str(m, 1)] = _str(m, 2)
                self._websocket(push, routes, cursor, ext, int(_first(r, 8, 0) or 10000))
                failures = 0
            except NotLoggedIn as e:
                self.on_error(e)
                return
            except (ApiError, OSError, ConnectionError, ValueError, IndexError, zlib.error) as e:
                if self._stop.is_set():
                    return
                failures += 1
                if failures == 3:
                    self.on_error(e)
            self._stop.wait(min(30, 2 * max(1, failures)))

    def _websocket(self, push, routes, cursor, ext, hb_ms):
        c = self.client
        q = dict(c._params())
        q.update(routes)
        q.update({
            "app_name": "tiktok_live_studio", "sup_ws_ds_opt": 1, "version_code": "300400",
            "webcast_sdk_version": IM_SDK_VERSION, "update_version_code": IM_SDK_VERSION, "compress": "gzip",
            "live_id": 12, "did_rule": 0, "identity": "anchor", "room_id": self.room_id,
            "heartbeatDuration": hb_ms, "cursor": cursor, "internal_ext": ext,
        })
        url = push + ("&" if "?" in push else "?") + urllib.parse.urlencode(q)
        ws = _WebSocket(url, {"User-Agent": USER_AGENT, "Cookie": c._cookie_header(),
                              "Origin": "https://www.tiktok.com"})
        self._ws = ws
        hb_every = max(10.0, hb_ms / 1000)
        ws.settimeout(hb_every)
        last_hb = time.monotonic()
        last_rx = time.monotonic()
        try:
            ws.send_binary(push_frame("hb"))
            while not self._stop.is_set():
                if time.monotonic() - last_hb >= hb_every:
                    ws.send_binary(push_frame("hb"))
                    last_hb = time.monotonic()
                try:
                    data = ws.recv()
                except (socket.timeout, TimeoutError):
                    if time.monotonic() - last_rx > 45:
                        raise ConnectionError("chat websocket timed out")
                    continue
                last_rx = time.monotonic()
                frame = pb_decode(data)
                headers = {}
                for h in frame.get(5, []):
                    hd = pb_decode(h)
                    headers[_str(hd, 1)] = _str(hd, 2)
                ptype = _str(frame, 7)
                if ptype == "close":
                    raise ConnectionError("chat websocket closed by server")
                if ptype != "msg":
                    continue
                payload = _first(frame, 8, b"")
                if headers.get("compress_type") == "gzip":
                    payload = gzip.decompress(payload)
                resp = pb_decode(payload)
                if _first(resp, 9, 0):  # need_ack
                    ws.send_binary(push_frame("ack", _str(resp, 5).encode(), _first(frame, 2, 0)))
                events, *_ = parse_response(payload)
                self._emit(events)
        finally:
            self._ws = None
            ws.close()
