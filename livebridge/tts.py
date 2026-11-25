"""Text-to-speech for the live chat.

Engines, best first:
  piper   neural offline voices (https://github.com/rhasspy/piper). LiveBridge can download it for the user
          into ~/.local/share/livebridge/piper (no root).
  spd-say speech-dispatcher, if installed
  espeak-ng / espeak, if installed
"""
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import tarfile
import threading
import urllib.request

from .i18n import _lang

DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "livebridge")
PIPER_DIR = os.path.join(DATA_DIR, "piper")
VOICES_DIR = os.path.join(DATA_DIR, "voices")

PIPER_RELEASE = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_{arch}.tar.gz"
VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
VOICES = {  # language -> path in the piper-voices repo
    "it": "it/it_IT/paola/medium/it_IT-paola-medium",
    "en": "en/en_US/lessac/medium/en_US-lessac-medium",
    "es": "es/es_ES/davefx/medium/es_ES-davefx-medium",
    "de": "de/de_DE/thorsten/medium/de_DE-thorsten-medium",
    "fr": "fr/fr_FR/siwis/medium/fr_FR-siwis-medium",
    "pt": "pt/pt_BR/faber/medium/pt_BR-faber-medium",
}
PIPER_DOWNLOAD_MB = 90


class TtsError(Exception):
    pass


# ------------------------------------------------------------------ engines
def _voice_lang():
    lang = _lang()
    return lang if lang in VOICES else "en"


def _voice_files(lang=None):
    name = VOICES[lang or _voice_lang()].rsplit("/", 1)[1]
    model = os.path.join(VOICES_DIR, name + ".onnx")
    return model, model + ".json"


def _piper_bin():
    managed = os.path.join(PIPER_DIR, "piper", "piper")
    if os.access(managed, os.X_OK):
        return managed
    return shutil.which("piper")


def _player(rate):
    if shutil.which("pw-cat"):
        return ["pw-cat", "--playback", "--rate", str(rate), "--channels", "1", "--format", "s16", "-"]
    if shutil.which("paplay"):
        return ["paplay", "--raw", f"--rate={rate}", "--channels=1", "--format=s16le"]
    if shutil.which("aplay"):
        return ["aplay", "-q", "-r", str(rate), "-f", "S16_LE", "-c", "1", "-t", "raw"]
    return None


def detect_engine():
    """'piper', 'spd-say', 'espeak-ng', 'espeak' or None."""
    model, cfg = _voice_files()
    if _piper_bin() and os.path.exists(model) and os.path.exists(cfg) and _player(22050):
        return "piper"
    for name in ("spd-say", "espeak-ng", "espeak"):
        if shutil.which(name):
            return name
    return None


def piper_installed():
    model, cfg = _voice_files()
    return bool(_piper_bin()) and os.path.exists(model) and os.path.exists(cfg)


def install_piper(progress=None):
    """Download Piper + a voice for the UI language. progress(fraction 0..1, label)."""
    progress = progress or (lambda f, s: None)
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
        platform.machine().lower())
    if not arch:
        raise TtsError(f"Piper isn't available for {platform.machine()}")
    os.makedirs(PIPER_DIR, exist_ok=True)
    os.makedirs(VOICES_DIR, exist_ok=True)
    jobs = []
    if not _piper_bin():
        jobs.append((PIPER_RELEASE.format(arch=arch), os.path.join(PIPER_DIR, "piper.tar.gz"), "piper"))
    model, cfg = _voice_files()
    base = VOICE_BASE + VOICES[_voice_lang()]
    if not os.path.exists(cfg):
        jobs.append((base + ".onnx.json", cfg, "voice"))
    if not os.path.exists(model):
        jobs.append((base + ".onnx", model, "voice"))
    total = len(jobs) or 1
    for i, (url, dest, what) in enumerate(jobs):
        _download(url, dest, lambda f, i=i: progress((i + f) / total, what))
        if dest.endswith(".tar.gz"):
            with tarfile.open(dest) as t:
                try:
                    t.extractall(PIPER_DIR, filter="data")
                except TypeError:  # Python < 3.12
                    for m in t.getmembers():
                        if m.name.startswith(("/", "..")) or ".." in m.name.split("/"):
                            raise TtsError("unsafe archive")
                    t.extractall(PIPER_DIR)
            os.remove(dest)
            os.chmod(os.path.join(PIPER_DIR, "piper", "piper"), 0o755)
    progress(1.0, "done")


def _download(url, dest, progress):
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "LiveBridge"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        size = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if size:
                progress(got / size)
    os.replace(tmp, dest)


# ------------------------------------------------------------------ text cleanup
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍\U000E0000-\U000E007F]+")
_REPEAT = re.compile(r"(.)\1{3,}")


def clean(text, limit=160):
    text = _URL.sub(" ", text or "")
    text = _EMOJI.sub(" ", text)
    text = _REPEAT.sub(r"\1\1\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def clean_name(nickname, username=""):
    n = clean(nickname, 40)
    if not re.search(r"\w", n):
        n = clean(username, 40)
    return n or "?"


# ------------------------------------------------------------------ speaker
class Speaker:
    """Speaks one line at a time in a background thread. Old lines are dropped when the queue
    gets long, so the voice never lags minutes behind the chat."""

    MAX_PENDING = 6

    def __init__(self, rate=1.0):
        self.rate = rate
        self._q = queue.Queue()
        self._proc = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.engine = detect_engine()
        self._thread = threading.Thread(target=self._run, name="livebridge-tts", daemon=True)
        self._thread.start()

    def refresh_engine(self):
        self.engine = detect_engine()
        return self.engine

    def say(self, text):
        text = clean(text, 220)
        if not text or not self.engine:
            return
        while self._q.qsize() >= self.MAX_PENDING:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._q.put(text)

    def skip(self):
        """Stop the current line and drop everything queued."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        with self._lock:
            for p in self._proc:
                try:
                    p.kill()
                except OSError:
                    pass

    def close(self):
        self._stop.set()
        self.skip()
        self._q.put(None)

    def _run(self):
        while not self._stop.is_set():
            text = self._q.get()
            if text is None:
                return
            try:
                self._speak(text)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass

    def _spawn(self, cmd, **kw):
        p = subprocess.Popen(cmd, stdout=kw.pop("stdout", subprocess.DEVNULL), stderr=subprocess.DEVNULL, **kw)
        with self._lock:
            self._proc.append(p)
        return p

    def _done(self, *procs):
        with self._lock:
            for p in procs:
                if p in self._proc:
                    self._proc.remove(p)

    def _speak(self, text):
        lang = _voice_lang()
        if self.engine == "piper":
            model, cfg = _voice_files()
            with open(cfg) as f:
                rate = json.load(f)["audio"]["sample_rate"]
            length = f"{1.0 / max(0.5, min(2.0, self.rate)):.2f}"
            synth = self._spawn([_piper_bin(), "--model", model, "--output_raw", "--length_scale", length],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            play = self._spawn(_player(rate), stdin=synth.stdout)
            synth.stdout.close()
            synth.stdin.write(text.encode())
            synth.stdin.close()
            play.wait()
            synth.wait()
            self._done(synth, play)
        elif self.engine == "spd-say":
            p = self._spawn(["spd-say", "-w", "-l", lang, "-r", str(int((self.rate - 1) * 60)), text])
            p.wait()
            self._done(p)
        elif self.engine in ("espeak-ng", "espeak"):
            p = self._spawn([self.engine, "-v", lang, "-s", str(int(170 * self.rate)), text])
            p.wait()
            self._done(p)
