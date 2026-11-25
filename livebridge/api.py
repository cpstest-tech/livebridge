"""Client for the private TikTok LIVE Studio API (protocol of Live Studio 1.40.x for Windows).

Flow:
  login  POST webcast.tiktokv.com/passport/oidc/prepare/      -> state, nonce, ticket
         browser: www.tiktok.com/ucenter_web/onetap_auth      -> live-studio-app://login/continue?id_token&user_id
         POST {assurance}/passport/web/region/                 -> regional passport domain
         POST https://{domain}/passport/oidc/login/            -> session cookies
  live   POST {webcast16-normal-<region>}/webcast/room/create/ -> data.stream_url.rtmp_push_url
         POST /webcast/room/ping/anchor/ every 5 s (status 2 = live, 4 = finish)

The global webcast.tiktokv.com host requires native request signing (403 without it);
the regional webcast16-normal-* node accepts plain session cookies.
"""
import hashlib
import http.client
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

APP_VERSION = "1.40.2"
AID = 8311
APP_NAME = "tiktok_live_studio"
CHANNEL = "studio"
CLIENT_ID = "abad793d-e940-42cb-bc89-e6939b1b3b4d"
URL_SCHEME = "live-studio-app"
REDIRECT_URI = f"{URL_SCHEME}://login/continue"
PREPARE_URL = "https://webcast.tiktokv.com/passport/oidc/prepare/"
AUTH_PAGE = "https://www.tiktok.com/ucenter_web/onetap_auth"
ASSURANCE_DOMAINS = ("api16-normal-useast5.tiktokv.us", "api16-normal-c-alisg.tiktokv.com")
FALLBACK_WEBCAST = "webcast.tiktokv.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              f"TikTokLIVEStudio/{APP_VERSION} Chrome/130.0.6723.191 Electron/33.4.11 Safari/537.36")

STATUS_LIVING = 2
STATUS_FINISH = 4
PING_ROOM_FINISHED = (30003, 0x1C9CF39)
TITLE_MAX = 32


class ApiError(Exception):
    def __init__(self, message, code=None, logid=None):
        super().__init__(message)
        self.code = code
        self.logid = logid

    def __str__(self):
        s = super().__str__()
        if self.code is not None:
            s += f" (code {self.code})"
        return s


class NotLoggedIn(ApiError):
    pass


def webcast_host_for(passport_domain):
    if passport_domain and passport_domain.startswith("api16-normal"):
        return "webcast16-normal" + passport_domain[len("api16-normal"):]
    return FALLBACK_WEBCAST


def split_rtmp(url):
    """rtmp://host/app/stream-123?sign=.. -> ("rtmp://host/app", "stream-123?sign=..")"""
    p = urllib.parse.urlsplit(url)
    app, _, stream = p.path.lstrip("/").partition("/")
    return f"{p.scheme}://{p.netloc}/{app}", stream + (f"?{p.query}" if p.query else "")


def _timezone():
    try:
        return os.path.realpath("/etc/localtime").split("zoneinfo/")[1]
    except (IndexError, OSError):
        return "UTC"


def _language():
    lang = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "en")
    return lang.split(".")[0].split("_")[0] or "en"


class TikTokClient:
    def __init__(self, state=None):
        self.state = state or config.State()
        config.ensure_dir()
        self.jar = http.cookiejar.MozillaCookieJar(config.COOKIE_FILE)
        if os.path.exists(config.COOKIE_FILE):
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError):
                pass
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    # ------------------------------------------------------------------ plumbing
    @property
    def logged_in(self):
        return bool(self.state.get("user")) and any(c.name == "sessionid" for c in self.jar)

    @property
    def user(self):
        return self.state.get("user") or {}

    def _save_cookies(self):
        self.jar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(config.COOKIE_FILE, 0o600)

    def _cookie_header(self):
        # Session cookies are set on the regional passport domain (.tiktokv.eu, .tiktokv.us...);
        # Live Studio syncs them to every webcast host, so do the same.
        return "; ".join(f"{k}={v}" for k, v in {c.name: c.value for c in self.jar}.items())

    def _params(self):
        lang = _language()
        p = {
            "aid": AID, "app_name": APP_NAME, "device_id": self.state["device_id"],
            "install_id": self.state["install_id"], "channel": CHANNEL, "version_code": APP_VERSION,
            "device_platform": "windows", "webcast_sdk_version": APP_VERSION.replace(".", ""),
            "live_mode": 6, "language": lang, "app_language": lang, "webcast_language": lang,
            "timezone_name": _timezone(), "screen_width": 1920, "screen_height": 1080,
            "browser_language": lang, "browser_platform": "Win32", "browser_name": "Mozilla",
            "browser_version": USER_AGENT.replace("Mozilla/", ""),
        }
        if self.state.get("region"):
            p["priority_region"] = self.state["region"]
        return p

    def _request(self, method, url, params=None, data=None, cookies=True, timeout=20, raw_body=False):
        q = self._params()
        q.update(params or {})
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(q)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["x-ss-stub"] = hashlib.md5(body).hexdigest().upper()
        if self.state.get("region"):
            headers["x-tt-store-region"] = self.state["region"]
        if cookies and len(self.jar):
            headers["Cookie"] = self._cookie_header()
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        for attempt in range(3):
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    status, raw, hdrs = r.status, r.read(), r.headers
                break
            except urllib.error.HTTPError as e:
                status, raw, hdrs = e.code, e.read(), e.headers
                break
            except (http.client.IncompleteRead, http.client.RemoteDisconnected, ConnectionResetError) as e:
                # TikTok's edge sometimes drops large chunked responses mid-way; retrying is safe here
                # (room/create replaces a room that never got a heartbeat).
                if attempt == 2:
                    raise ApiError("Network error: the connection to TikTok dropped, try again") from e
                time.sleep(0.6 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise ApiError(f"Network error: {getattr(e, 'reason', e)}") from e
        if len(self.jar):
            self._save_cookies()
        logid = hdrs.get("x-tt-logid")
        if raw_body:
            if status != 200:
                raise ApiError(f"HTTP {status} from {urllib.parse.urlsplit(url).netloc}", code=status, logid=logid)
            return raw, hdrs, logid
        try:
            js = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except ValueError:
            raise ApiError(f"Unexpected HTTP {status} response from {urllib.parse.urlsplit(url).netloc}",
                           code=status, logid=logid)
        if status == 403 and not js:
            raise ApiError("Request rejected by TikTok (HTTP 403)", code=403, logid=logid)
        return js, hdrs, logid

    def _webcast(self, method, path, **kw):
        if not self.logged_in:
            raise NotLoggedIn("Not logged in")
        host = self.state.get("webcast_host") or FALLBACK_WEBCAST
        js, hdrs, logid = self._request(method, f"https://{host}{path}", **kw)
        sc = js.get("status_code")
        if sc in (20003, 20001):
            raise NotLoggedIn("Your TikTok session has expired, please log in again", code=sc, logid=logid)
        return js, logid

    @staticmethod
    def _fail(js, what, logid):
        d = js.get("data") if isinstance(js.get("data"), dict) else {}
        msg = d.get("prompts") or d.get("message") or d.get("description") or js.get("message") or "unknown error"
        raise ApiError(f"{what}: {msg}", code=js.get("status_code", d.get("error_code")), logid=logid)

    # ------------------------------------------------------------------ login
    def begin_login(self):
        """Returns the URL to open in the browser."""
        js, _, logid = self._request("POST", PREPARE_URL, cookies=False, data={
            "channel": CHANNEL, "app_name": APP_NAME, "device_type": "PC", "os_version": "10.0.19045"})
        if js.get("message") != "success":
            self._fail(js, "Could not start login", logid)
        d = js["data"]
        self.state.update(login_ticket=d["ticket"], login_state=d["state"])
        return AUTH_PAGE + "?" + urllib.parse.urlencode({
            "state": d["state"], "nonce": d["nonce"], "ticket": d["ticket"],
            "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI})

    def finish_login(self, callback_uri):
        """Consumes the live-studio-app://login/continue?... URI. Returns the user dict."""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(callback_uri.strip()).query)
        id_token, user_id = q.get("id_token", [None])[0], q.get("user_id", [None])[0]
        if not id_token or not user_id:
            raise ApiError("This link doesn't contain a login token. Start the login again.")
        ticket, st = self.state.get("login_ticket"), self.state.get("login_state")
        if not ticket:
            raise ApiError("Login expired. Start the login again.")

        domains = set()
        for dom in ASSURANCE_DOMAINS:
            try:
                js, _, _ = self._request("POST", f"https://{dom}/passport/web/region/", cookies=False,
                                         data={"hashed_id": user_id, "type": 5, "aid": AID})
            except ApiError:
                continue
            dom_res = (js.get("data") or {}).get("domain")
            if dom_res:
                domains.add(dom_res)
        if len(domains) != 1:
            raise ApiError("Could not determine your TikTok region")
        domain = domains.pop()

        self.jar.clear()
        js, hdrs, logid = self._request("POST", f"https://{domain}/passport/oidc/login/", cookies=False,
                                        data={"id_token": id_token, "ticket": ticket, "state": st})
        if js.get("message") != "success":
            self._fail(js, "Login failed", logid)
        d = js.get("data") or {}
        avatar = d.get("avatar_url") or d.get("avatar_uri") or ""
        if isinstance(avatar, dict):
            avatar = (avatar.get("url_list") or [""])[0]
        user = {
            "id": str(d.get("user_id_str") or d.get("user_id") or user_id),
            "name": d.get("screen_name") or d.get("name") or "",
            "username": d.get("unique_id") or "",
            "avatar": avatar,
        }
        self.state.update(
            user=user, passport_domain=domain,
            region=(hdrs.get("x-tt-store-region") or "").lower() or None,
            idc=hdrs.get("x-tt-store-idc") or None,
            webcast_host=webcast_host_for(domain),
            login_ticket=None, login_state=None,
        )
        return user

    def logout(self):
        self.jar.clear()
        self.state.clear_account()

    # ------------------------------------------------------------------ live
    def create_info(self):
        js, logid = self._webcast("GET", "/webcast/room/create_info/")
        if js.get("status_code") != 0:
            self._fail(js, "Could not load LIVE settings", logid)
        return js.get("data") or {}

    def create_room(self, title, *, topic_id=0, cover_uri="", age_restricted=False,
                    allow_comments=True, allow_gifts=True):
        data = {
            "title": title[:TITLE_MAX], "live_studio": 1, "gen_replay": "true",
            "chat_auth": 1 if allow_comments else 0, "age_restricted": 1 if age_restricted else 0,
            "cover_uri": cover_uri or "", "close_room_when_close_stream": "false",
            "hashtag_id": int(topic_id or 0), "game_tag_id": 0, "screenshot_cover_status": 1,
            "multi_stream_scene": 0, "multi_stream_source": 1, "gift_auth": 1 if allow_gifts else 0,
            "chat_l2": 2, "star_comment_switch": "false", "rtc_net_enabled": "false",
            "visible_scope_type": 0,
        }
        js, logid = self._webcast("POST", "/webcast/room/create/", data=data)
        if js.get("status_code") != 0:
            self._fail(js, "TikTok refused to start the LIVE", logid)
        room = js["data"]
        su = room.get("stream_url") or {}
        push = su.get("rtmp_push_url") or (su.get("push_urls") or [None])[0]
        if not push:
            raise ApiError("TikTok didn't return a stream key", logid=logid)
        saved = {
            "id": room.get("id_str", ""),
            "stream_id": room.get("stream_id_str", ""),
            "ping_room_id": (room.get("living_room_attrs") or {}).get("room_id_str") or room.get("id_str", ""),
            "push_url": push,
            "title": title[:TITLE_MAX],
            "started": int(room.get("create_time") or time.time()),
        }
        self.state.update(room=saved)
        return saved

    def ping(self, room, status=STATUS_LIVING):
        js, _ = self._webcast("POST", "/webcast/room/ping/anchor/", timeout=10, data={
            "status": status, "room_id": room["ping_room_id"], "stream_id": room["stream_id"]})
        return js.get("status_code")

    def viewers(self, room):
        """Best effort: current viewer count or None."""
        try:
            js, _ = self._webcast("GET", "/webcast/room/info/", timeout=8, params={"room_id": room["id"]})
            d = js.get("data") or {}
            n = d.get("user_count")
            if n is None:
                n = (d.get("stats") or {}).get("user_count_composite") or (d.get("stats") or {}).get("total_user")
            return int(n) if n is not None else None
        except (ApiError, ValueError, TypeError):
            return None

    def fetch_chat(self, room_id, cursor="", internal_ext=""):
        """Raw protobuf body of /webcast/im/fetch/ (decoded in chat.py)."""
        if not self.logged_in:
            raise NotLoggedIn("Not logged in")
        host = self.state.get("webcast_host") or FALLBACK_WEBCAST
        raw, _, _ = self._request("GET", f"https://{host}/webcast/im/fetch/", timeout=15, raw_body=True, params={
            "room_id": room_id, "resp_content_type": "protobuf", "sup_ws_ds_opt": 1, "did_rule": 0,
            "fetch_rule": 1, "cursor": cursor, "internal_ext": internal_ext,
            # values LIVE Studio's IM SDK sends; without them the server returns no messages
            "live_id": 12, "version_code": "300400", "identity": "anchor"})
        return raw

    def end_live(self, room=None):
        room = room or self.state.get("room")
        if not room:
            return None
        try:
            return self.ping(room, STATUS_FINISH)
        finally:
            self.state.update(room=None)
