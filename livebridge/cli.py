"""Command line interface: livebridge login | status | start | stop | logout"""
import argparse
import select
import signal
import sys
import threading
import time
import webbrowser

from . import __version__, obs, urlhandler
from .api import ApiError, TikTokClient, split_rtmp
from .session import LiveSession

COMMANDS = ("login", "status", "start", "stop", "logout")


def cmd_login(a):
    cl = TikTokClient()
    urlhandler.ensure_registered()
    urlhandler.clear_pending()
    url = cl.begin_login()
    print("Opening TikTok in your browser. Approve the request and allow the browser to open LiveBridge.")
    print(f"If nothing opens, visit:\n  {url}\n")
    webbrowser.open(url)
    print("Waiting… (you can also paste the live-studio-app:// link here and press Enter)")
    deadline = time.time() + 600
    uri = None
    urlhandler.mark_cli_waiting(True)
    try:
        while time.time() < deadline and not uri:
            uri = urlhandler.pop_pending()
            if uri:
                break
            r, _, _ = select.select([sys.stdin], [], [], 1.0)
            if r:
                line = sys.stdin.readline().strip()
                if line.startswith("live-studio-app://"):
                    uri = line
    finally:
        urlhandler.mark_cli_waiting(False)
    if not uri:
        sys.exit("Timed out waiting for the browser.")
    user = cl.finish_login(uri)
    print(f"Logged in as {user['name'] or user['id']} (region {cl.state.get('region') or '?'}).")


def cmd_status(a):
    cl = TikTokClient()
    if not cl.logged_in:
        print("Not logged in. Run: livebridge login")
        return
    u = cl.user
    print(f"Account : {u.get('name')} {('@' + u['username']) if u.get('username') else ''} (id {u.get('id')})")
    print(f"API host: {cl.state.get('webcast_host')}")
    info = cl.create_info()
    print(f"Banned  : {info.get('ban_status', {}).get('is_ban', False)}")
    if info.get("go_live_prompt") or info.get("block_prompt"):
        print(f"Notice  : {info.get('go_live_prompt') or info.get('block_prompt')}")
    print(f"OBS     : {obs.status()}")
    if cl.state.get("room"):
        print(f"Open LIVE: room {cl.state.get('room')['id']} (run 'livebridge stop' to end it)")


def cmd_start(a):
    cl = TikTokClient()
    if not cl.logged_in:
        sys.exit("Not logged in. Run: livebridge login")
    room = cl.state.get("room")
    if room:
        print(f"Resuming the open LIVE (room {room['id']}).")
    else:
        room = cl.create_room(a.title, topic_id=a.topic_id, age_restricted=a.mature,
                              allow_comments=not a.no_comments, allow_gifts=not a.no_gifts)
        print(f"LIVE created (room {room['id']}).")
    server, key = split_rtmp(room["push_url"])
    print(f"  Server: {server}\n  Key:    {key}")
    control = not a.no_obs
    if control:
        try:
            obs.start_streaming(server, key)
            print("OBS is streaming.")
        except obs.ObsError as e:
            print(f"OBS: {e}\nPaste the server and key into OBS manually.")
            control = False

    done = threading.Event()

    def on_update(info):
        if info["ping_failures"]:
            print(f"heartbeat failed ({info['ping_failures']}x), retrying…", file=sys.stderr)

    def on_ended(reason):
        print({"server": "TikTok ended the LIVE.", "user": "LIVE ended."}.get(reason, reason))
        done.set()

    s = LiveSession(cl, room, on_update=on_update, on_ended=on_ended, control_obs=control)
    s.start()
    print("\nYou're LIVE. Keep this running; press Ctrl+C to end the LIVE.")
    signal.signal(signal.SIGTERM, lambda *_: s.end())
    try:
        while not done.wait(0.5):
            pass
    except KeyboardInterrupt:
        s.end()


def cmd_stop(a):
    cl = TikTokClient()
    if not cl.state.get("room"):
        print("No open LIVE.")
        return
    cl.end_live()
    obs.stop_streaming()
    print("LIVE ended.")


def cmd_logout(a):
    TikTokClient().logout()
    print("Logged out.")


def main():
    ap = argparse.ArgumentParser(prog="livebridge", description="Go LIVE on TikTok from Linux with OBS. "
                                 "Run without arguments to open the app.")
    ap.add_argument("--version", action="version", version=f"LiveBridge {__version__}")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("login", help="log in to TikTok in your browser").set_defaults(fn=cmd_login)
    sp.add_parser("status", help="show account, permissions and OBS status").set_defaults(fn=cmd_status)
    p = sp.add_parser("start", help="go LIVE (stays running for the heartbeat)")
    p.add_argument("-t", "--title", default="LIVE", help="title, max 32 characters")
    p.add_argument("--topic-id", type=int, default=0)
    p.add_argument("--mature", action="store_true", help="mark as 18+")
    p.add_argument("--no-comments", action="store_true")
    p.add_argument("--no-gifts", action="store_true")
    p.add_argument("--no-obs", action="store_true", help="only print server and key")
    p.set_defaults(fn=cmd_start)
    sp.add_parser("stop", help="end the open LIVE").set_defaults(fn=cmd_stop)
    sp.add_parser("logout", help="forget the TikTok session").set_defaults(fn=cmd_logout)
    a = ap.parse_args()
    try:
        a.fn(a)
    except ApiError as e:
        msg = f"Error: {e}"
        if e.logid:
            msg += f" [logid {e.logid}]"
        sys.exit(msg)


if __name__ == "__main__":
    main()
