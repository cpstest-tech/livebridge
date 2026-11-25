"""Registers LiveBridge as the handler for live-studio-app:// so TikTok's login page can hand the token back."""
import os
import shutil
import subprocess
import sys

from . import APP_ID, config
from .api import URL_SCHEME

MIME = f"x-scheme-handler/{URL_SCHEME}"
APPS_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "applications")
INSTALLED_DESKTOP = f"{APP_ID}.desktop"
HANDLER_DESKTOP = f"{APP_ID}.UrlHandler.desktop"
PENDING = os.path.join(config.CONFIG_DIR, "pending-login.url")
CLI_WAITING = os.path.join(config.CONFIG_DIR, "cli-login.pid")


def _exec_line():
    exe = shutil.which("livebridge")
    if exe:
        return f"{exe} %u"
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return f'env PYTHONPATH="{pkg_parent}" {sys.executable} -m livebridge %u'


def current_handler():
    try:
        return subprocess.run(["xdg-mime", "query", "default", MIME], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def ensure_registered():
    """Make sure the browser can return to us. Safe to call on every start."""
    installed = os.path.join(APPS_DIR, INSTALLED_DESKTOP)
    target = INSTALLED_DESKTOP if os.path.exists(installed) else HANDLER_DESKTOP
    if target == HANDLER_DESKTOP:
        os.makedirs(APPS_DIR, exist_ok=True)
        with open(os.path.join(APPS_DIR, HANDLER_DESKTOP), "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=LiveBridge login\n"
                    f"Exec={_exec_line()}\nNoDisplay=true\nTerminal=false\nMimeType={MIME};\n")
    if current_handler() != target:
        subprocess.run(["xdg-mime", "default", target, MIME], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", APPS_DIR], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- hand-off between the browser-launched process and a waiting CLI login
def set_pending(uri):
    config.ensure_dir()
    with open(PENDING, "w") as f:
        f.write(uri)
    os.chmod(PENDING, 0o600)


def pop_pending():
    try:
        with open(PENDING) as f:
            uri = f.read().strip()
        os.remove(PENDING)
        return uri or None
    except OSError:
        return None


def clear_pending():
    try:
        os.remove(PENDING)
    except OSError:
        pass


def mark_cli_waiting(on=True):
    config.ensure_dir()
    if on:
        with open(CLI_WAITING, "w") as f:
            f.write(str(os.getpid()))
    else:
        try:
            os.remove(CLI_WAITING)
        except OSError:
            pass


def cli_is_waiting():
    try:
        with open(CLI_WAITING) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False
