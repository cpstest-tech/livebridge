"""Paths and the small persistent state file (0600, inside ~/.config/livebridge)."""
import json
import os
import random
import threading

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "livebridge")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
COOKIE_FILE = os.path.join(CONFIG_DIR, "cookies.txt")

_lock = threading.Lock()


def ensure_dir():
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)


class State:
    """Dict-like persistent state. Every write goes straight to disk (atomic)."""

    def __init__(self):
        self._d = {}
        try:
            with open(STATE_FILE) as f:
                self._d = json.load(f)
        except (OSError, ValueError):
            self._d = {}
        if "device_id" not in self._d:
            # Live Studio uses a registered device id; a stable random one per machine works.
            self._d["device_id"] = str(random.randint(7_000_000_000_000_000_000, 7_499_999_999_999_999_999))
            self._d["install_id"] = str(random.randint(7_000_000_000_000_000_000, 7_499_999_999_999_999_999))
            self.save()

    def get(self, k, default=None):
        return self._d.get(k, default)

    def __getitem__(self, k):
        return self._d[k]

    def __contains__(self, k):
        return k in self._d

    def update(self, **kw):
        for k, v in kw.items():
            if v is None:
                self._d.pop(k, None)
            else:
                self._d[k] = v
        self.save()

    def save(self):
        with _lock:
            ensure_dir()
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._d, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, STATE_FILE)

    def clear_account(self):
        for k in ("user", "region", "idc", "passport_domain", "webcast_host", "room", "login_ticket", "login_state"):
            self._d.pop(k, None)
        self.save()
        try:
            os.remove(COOKIE_FILE)
        except OSError:
            pass
