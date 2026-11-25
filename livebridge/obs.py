"""OBS Studio integration through obs-websocket v5 (built into OBS >= 28). Standard library only."""
import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import time

FLATPAK_ID = "com.obsproject.Studio"
NATIVE_CFG = os.path.expanduser("~/.config/obs-studio")
FLATPAK_CFG = os.path.expanduser(f"~/.var/app/{FLATPAK_ID}/config/obs-studio")


class ObsError(Exception):
    pass


# ---------------------------------------------------------------------------- discovery
def _flatpak_installed():
    if not shutil.which("flatpak"):
        return False
    r = subprocess.run(["flatpak", "info", FLATPAK_ID], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def install_kind():
    """'native', 'flatpak' or None."""
    if shutil.which("obs"):
        return "native"
    if _flatpak_installed():
        return "flatpak"
    return None


def config_dir(kind=None):
    kind = kind or install_kind()
    return FLATPAK_CFG if kind == "flatpak" else NATIVE_CFG


def ws_config_path(kind=None):
    return os.path.join(config_dir(kind), "plugin_config", "obs-websocket", "config.json")


def is_running():
    for name in ("obs", "obs-studio"):
        if subprocess.run(["pgrep", "-x", name], stdout=subprocess.DEVNULL).returncode == 0:
            return True
    return False


def read_ws_config(kind=None):
    try:
        with open(ws_config_path(kind)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def enable_websocket(kind=None):
    """Turns on obs-websocket with a random password. OBS must be closed (it rewrites the file on exit)."""
    if is_running():
        raise ObsError("Close OBS first, then try again")
    path = ws_config_path(kind)
    cfg = read_ws_config(kind)
    cfg.setdefault("server_port", 4455)
    cfg.setdefault("alerts_enabled", False)
    cfg["server_enabled"] = True
    cfg["auth_required"] = True
    if not cfg.get("server_password"):
        cfg["server_password"] = secrets.token_urlsafe(16)
    cfg["first_load"] = False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return cfg


def launch(kind=None):
    kind = kind or install_kind()
    if kind == "native":
        cmd = ["obs"]
    elif kind == "flatpak":
        cmd = ["flatpak", "run", FLATPAK_ID]
    else:
        raise ObsError("OBS Studio is not installed")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True)


# ---------------------------------------------------------------------------- websocket client
class ObsWebSocket:
    def __init__(self, host="127.0.0.1", port=None, password=None, timeout=3):
        cfg = read_ws_config()
        self.host = host
        self.port = int(port or cfg.get("server_port", 4455))
        if password is None:
            password = cfg.get("server_password", "") if cfg.get("auth_required", True) else ""
        self.password = password
        self.timeout = timeout
        self.sock = None
        self._buf = b""
        self._n = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def connect(self):
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise ObsError(f"Can't reach obs-websocket on port {self.port}") from e
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   "Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ObsError("obs-websocket handshake failed")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise ObsError("obs-websocket refused the connection")
        self.sock, self._buf = s, buf.split(b"\r\n\r\n", 1)[1]
        hello = self._recv()
        ident = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = (hello.get("d") or {}).get("authentication")
        if auth:
            secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest())
            ident["authentication"] = base64.b64encode(
                hashlib.sha256(secret + auth["challenge"].encode()).digest()).decode()
        self._send({"op": 1, "d": ident})
        try:
            msg = self._recv()
        except ObsError as e:
            raise ObsError("OBS rejected the websocket password") from e
        if msg.get("op") != 2:
            raise ObsError("OBS rejected the websocket password")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ObsError("OBS closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv(self):
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            op = b1 & 0x0F
            if op == 8:
                raise ObsError("OBS closed the connection: " + payload[2:].decode("utf-8", "replace"))
            if op == 9:  # ping -> pong
                self._frame(0xA, payload)
            elif op == 1:
                return json.loads(payload)

    def _frame(self, opcode, data):
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _send(self, obj):
        self._frame(0x1, json.dumps(obj).encode())

    def call(self, request_type, data=None, check=True):
        self._n += 1
        rid = str(self._n)
        self._send({"op": 6, "d": {"requestType": request_type, "requestId": rid, "requestData": data or {}}})
        while True:
            m = self._recv()
            if m.get("op") == 7 and m["d"].get("requestId") == rid:
                st = m["d"].get("requestStatus") or {}
                if check and not st.get("result"):
                    raise ObsError(f"{request_type} failed: {st.get('comment') or st.get('code')}")
                return m["d"].get("responseData") or {}


# ---------------------------------------------------------------------------- high level
NOT_INSTALLED, CLOSED, NEEDS_SETUP, NOT_REACHABLE, READY = (
    "not_installed", "closed", "needs_setup", "not_reachable", "ready")


def status():
    """One of NOT_INSTALLED, CLOSED, NEEDS_SETUP (websocket off), NOT_REACHABLE (running, can't connect), READY."""
    kind = install_kind()
    if not kind:
        return NOT_INSTALLED
    cfg = read_ws_config(kind)
    running = is_running()
    if not cfg.get("server_enabled"):
        return NEEDS_SETUP
    if not running:
        return CLOSED
    try:
        with ObsWebSocket(timeout=1.5):
            return READY
    except ObsError:
        return NOT_REACHABLE


def wait_ready(timeout=40):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            ws = ObsWebSocket(timeout=2)
            ws.connect()
            return ws
        except ObsError as e:
            last = e
            time.sleep(1)
    raise ObsError(f"OBS didn't become ready in time ({last})")


def start_streaming(server, key, launch_if_closed=True):
    """Puts server/key into OBS and starts the stream. Launches OBS if needed."""
    st = status()
    if st == NOT_INSTALLED:
        raise ObsError("OBS Studio is not installed")
    if st == NEEDS_SETUP:
        if is_running():
            raise ObsError("Enable the OBS WebSocket server (Tools > WebSocket Server Settings) or close OBS "
                           "and let LiveBridge set it up")
        enable_websocket()
        st = CLOSED
    if st == CLOSED:
        if not launch_if_closed:
            raise ObsError("OBS is not running")
        launch()
    elif st == NOT_REACHABLE:
        raise ObsError("OBS is running but its WebSocket server can't be reached. Restart OBS.")
    ws = wait_ready()
    try:
        if ws.call("GetStreamStatus").get("outputActive"):
            ws.call("StopStream", check=False)
            for _ in range(20):
                if not ws.call("GetStreamStatus").get("outputActive"):
                    break
                time.sleep(0.5)
        ws.call("SetStreamServiceSettings", {
            "streamServiceType": "rtmp_custom",
            "streamServiceSettings": {"server": server, "key": key, "use_auth": False, "bwtest": False}})
        ws.call("StartStream")
    finally:
        ws.close()


def stop_streaming():
    try:
        with ObsWebSocket(timeout=2) as ws:
            if ws.call("GetStreamStatus").get("outputActive"):
                ws.call("StopStream", check=False)
    except ObsError:
        pass


def stream_stats():
    """{'active', 'bytes', 'skipped', 'total', 'reconnecting'} or None when OBS can't be reached."""
    try:
        with ObsWebSocket(timeout=1.5) as ws:
            s = ws.call("GetStreamStatus")
            return {"active": bool(s.get("outputActive")), "reconnecting": bool(s.get("outputReconnecting")),
                    "bytes": int(s.get("outputBytes") or 0), "skipped": int(s.get("outputSkippedFrames") or 0),
                    "total": int(s.get("outputTotalFrames") or 0)}
    except ObsError:
        return None
