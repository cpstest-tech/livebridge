"""A running LIVE: keeps the TikTok heartbeat alive and collects stats in a background thread."""
import threading
import time

from . import obs
from .api import PING_ROOM_FINISHED, ApiError, split_rtmp

PING_EVERY = 5
VIEWERS_EVERY = 15
# Live Studio considers the room dead after a few minutes without pings; warn well before that.
PING_FAIL_WARN = 3


class LiveSession:
    """
    on_update(info: dict) is called from the worker thread with:
      heartbeat_ok (bool), ping_failures (int), viewers (int|None),
      obs (dict|None from obs.stream_stats), bitrate_kbps (float|None)
    on_ended(reason: str) is called once: 'user', 'server', 'expired'
    """

    def __init__(self, client, room, on_update=None, on_ended=None, control_obs=True):
        self.client = client
        self.room = room
        self.server, self.key = split_rtmp(room["push_url"])
        self.on_update = on_update or (lambda info: None)
        self.on_ended = on_ended or (lambda reason: None)
        self.control_obs = control_obs
        self._stop = threading.Event()
        self._ended = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="livebridge-heartbeat", daemon=True)

    @property
    def started(self):
        return self.room.get("started") or time.time()

    def start(self):
        self._thread.start()

    def _finish(self, reason):
        with self._lock:
            if self._ended:
                return
            self._ended = True
        self._stop.set()
        if self.control_obs:
            obs.stop_streaming()
        self.on_ended(reason)

    def end(self):
        """End the LIVE on TikTok (blocking network call; call from a worker thread)."""
        self._stop.set()
        try:
            self.client.end_live(self.room)
        finally:
            self._finish("user")

    def _run(self):
        failures = 0
        viewers = None
        last_viewers = 0
        last_bytes = None
        while not self._stop.is_set():
            t0 = time.monotonic()
            ok = False
            try:
                code = self.client.ping(self.room)
                if code in PING_ROOM_FINISHED:
                    self.client.state.update(room=None)
                    self._finish("server")
                    return
                ok = True
                failures = 0
            except ApiError:
                failures += 1

            if time.monotonic() - last_viewers >= VIEWERS_EVERY:
                last_viewers = time.monotonic()
                v = self.client.viewers(self.room)
                if v is not None:
                    viewers = v

            stats = obs.stream_stats() if self.control_obs else None
            bitrate = None
            if stats and stats["active"]:
                if last_bytes is not None and stats["bytes"] >= last_bytes[0]:
                    dt = time.monotonic() - last_bytes[1]
                    if dt > 0:
                        bitrate = (stats["bytes"] - last_bytes[0]) * 8 / 1000 / dt
                last_bytes = (stats["bytes"], time.monotonic())
            else:
                last_bytes = None

            self.on_update({"heartbeat_ok": ok, "ping_failures": failures, "viewers": viewers,
                            "obs": stats, "bitrate_kbps": bitrate})
            self._stop.wait(max(0.5, PING_EVERY - (time.monotonic() - t0)))
