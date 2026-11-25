import threading
import time
import urllib.request

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .. import APP_ID, obs
from ..api import TITLE_MAX, URL_SCHEME, ApiError, NotLoggedIn, TikTokClient, split_rtmp
from ..i18n import _, fmt_int
from .. import tts
from ..chat import ChatReader
from ..session import LiveSession


def bg(fn, done=None):
    """Run fn() in a thread, then done(result, error) on the GTK main loop."""
    def worker():
        try:
            res, err = fn(), None
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            res, err = None, e
        if done:
            GLib.idle_add(lambda: (done(res, err), False)[1])
    threading.Thread(target=worker, daemon=True).start()


def fmt_duration(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def status_icon(name, css):
    img = Gtk.Image.new_from_icon_name(name)
    img.add_css_class(css)
    return img


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.set_title("LiveBridge")
        self.set_default_size(460, 760)
        self.set_size_request(360, 560)

        self.client = TikTokClient()
        self.speaker = tts.Speaker(rate=(self.client.state.get("tts") or {}).get("rate", 1.0))
        self.session = None
        self._obs_state = None
        self._obs_polling = False
        self._timer_id = 0
        self._closing_after_end = False
        self._login_in_progress = False

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="LiveBridge")
        header.set_title_widget(self.title_widget)
        menu = Gio.Menu()
        menu.append(_("About LiveBridge"), "app.about")
        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(self.menu_button)
        view.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=200)
        self.stack.add_named(self._build_login(), "login")
        self.stack.add_named(self._build_waiting(), "waiting")
        self.stack.add_named(self._build_busy(), "busy")
        self.stack.add_named(self._build_setup(), "setup")
        self.stack.add_named(self._build_live(), "live")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.connect("close-request", self._on_close_request)
        self.connect("destroy", lambda *_: self.speaker.close())
        self._set_tts_toggle(self._tts_settings()["enabled"] and self.speaker.engine is not None)
        GLib.timeout_add_seconds(3, self._poll_obs)

        if self.client.logged_in:
            self.show_setup()
        else:
            self.stack.set_visible_child_name("login")

    # ======================================================================== helpers
    def toast(self, text, timeout=3):
        t = Adw.Toast.new(text)
        t.set_timeout(timeout)
        self.toasts.add_toast(t)

    def error(self, err, heading=None):
        if isinstance(err, NotLoggedIn):
            self.client.logout()
            self.stack.set_visible_child_name("login")
            self.title_widget.set_subtitle("")
            self.toast(_("Your TikTok session has expired, please log in again"), 5)
            return
        body = _(str(err.args[0])) if err.args else str(err)
        if isinstance(err, ApiError) and err.code is not None:
            body += f" ({err.code})"
        if isinstance(err, ApiError) and err.logid:
            body += f"\n\nlogid: {err.logid}"
        d = Adw.AlertDialog.new(heading or _("Something went wrong"), body)
        d.add_response("ok", "OK")
        d.present(self)

    def busy(self, text):
        self.busy_label.set_label(text)
        self.stack.set_visible_child_name("busy")

    def copy(self, text):
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(GObject.Value(str, text)))
        self.toast(_("Copied"), 1)

    # ======================================================================== login
    def _build_login(self):
        page = Adw.StatusPage(icon_name=APP_ID, title=_("Go LIVE on TikTok from Linux"),
                              description=_("Log in with the TikTok account you want to stream with. "
                                            "Your browser will open TikTok's authorization page."))
        page.add_css_class("hero")
        btn = Gtk.Button(label=_("Log in with TikTok"), halign=Gtk.Align.CENTER)
        btn.add_css_class("golive-button")
        btn.connect("clicked", lambda *_: self.start_login())
        self.login_button = btn
        page.set_child(btn)
        return page

    def _build_waiting(self):
        page = Adw.StatusPage(title=_("Waiting for your browser…"),
                              description=_("Approve the request on TikTok. "
                                            "When the browser asks to open LiveBridge, allow it."))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        spinner = Gtk.Spinner(spinning=True, width_request=48, height_request=48, halign=Gtk.Align.CENTER)
        box.append(spinner)

        clamp = Adw.Clamp(maximum_size=420)
        grp = Adw.PreferencesGroup(title=_("Browser didn't come back?"))
        self.paste_row = Adw.EntryRow(title=_("Paste the live-studio-app:// link here"),
                                      show_apply_button=True)
        self.paste_row.connect("apply", lambda r: self.handle_login_uri(r.get_text()))
        grp.add(self.paste_row)
        clamp.set_child(grp)
        box.append(clamp)

        btns = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        again = Gtk.Button(label=_("Open the page again"))
        again.add_css_class("pill")
        again.connect("clicked", lambda *_: self._open_login_page())
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.add_css_class("pill")
        cancel.connect("clicked", lambda *_: self.stack.set_visible_child_name("login"))
        btns.append(again)
        btns.append(cancel)
        box.append(btns)
        page.set_child(box)
        return page

    def _build_busy(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, valign=Gtk.Align.CENTER,
                      halign=Gtk.Align.CENTER)
        box.append(Gtk.Spinner(spinning=True, width_request=48, height_request=48))
        self.busy_label = Gtk.Label()
        self.busy_label.add_css_class("title-3")
        box.append(self.busy_label)
        return box

    def start_login(self):
        self.login_button.set_sensitive(False)

        def done(url, err):
            self.login_button.set_sensitive(True)
            if err:
                return self.error(err)
            self._login_url = url
            self.paste_row.set_text("")
            self.stack.set_visible_child_name("waiting")
            self._open_login_page()
        bg(self.client.begin_login, done)

    def _open_login_page(self):
        url = getattr(self, "_login_url", None)
        if url:
            Gtk.UriLauncher.new(url).launch(self, None, None, None)

    def handle_login_uri(self, uri):
        uri = (uri or "").strip()
        if not uri.startswith(f"{URL_SCHEME}://") or self._login_in_progress:
            if uri:
                self.toast(_("Paste the live-studio-app:// link here"))
            return
        if self.session:
            return
        self._login_in_progress = True
        self.busy(_("Logging in…"))

        def done(user, err):
            self._login_in_progress = False
            if err:
                if self.client.logged_in:
                    self.show_setup()
                else:
                    self.stack.set_visible_child_name("login")
                return self.error(err)
            self.show_setup()
        bg(lambda: self.client.finish_login(uri), done)

    def confirm_logout(self):
        d = Adw.AlertDialog.new(_("Log out of TikTok?"), _("You'll need to log in again to go LIVE."))
        d.add_response("cancel", _("Cancel"))
        d.add_response("logout", _("Log out"))
        d.set_response_appearance("logout", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_close_response("cancel")

        def on_resp(_d, resp):
            if resp == "logout":
                self.client.logout()
                self.title_widget.set_subtitle("")
                self.stack.set_visible_child_name("login")
        d.connect("response", on_resp)
        d.present(self)

    # ======================================================================== setup
    def _build_setup(self):
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page = Adw.PreferencesPage(vexpand=True)

        # account
        acc = Adw.PreferencesGroup()
        self.account_row = Adw.ActionRow()
        self.avatar = Adw.Avatar(size=44, show_initials=True)
        self.account_row.add_prefix(self.avatar)
        logout = Gtk.Button(icon_name="system-log-out-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_("Log out"))
        logout.add_css_class("flat")
        logout.connect("clicked", lambda *_: self.confirm_logout())
        self.account_row.add_suffix(logout)
        acc.add(self.account_row)
        page.add(acc)

        # leftover LIVE
        self.resume_group = Adw.PreferencesGroup(visible=False)
        row = Adw.ActionRow(title=_("A previous LIVE is still open"))
        row.add_prefix(status_icon("dialog-warning-symbolic", "status-warn"))
        end_btn = Gtk.Button(label=_("End it"), valign=Gtk.Align.CENTER)
        end_btn.add_css_class("destructive-action")
        end_btn.connect("clicked", lambda *_: self.end_stale_live())
        res_btn = Gtk.Button(label=_("Resume"), valign=Gtk.Align.CENTER)
        res_btn.add_css_class("suggested-action")
        res_btn.connect("clicked", lambda *_: self.go_live(resume=True))
        row.add_suffix(end_btn)
        row.add_suffix(res_btn)
        self.resume_row = row
        self.resume_group.add(row)
        page.add(self.resume_group)

        # details
        det = Adw.PreferencesGroup(title=_("LIVE details"))
        self.title_row = Adw.EntryRow(title=_("Title"))
        self.char_counter = Gtk.Label(label=f"0/{TITLE_MAX}", valign=Gtk.Align.CENTER)
        self.char_counter.add_css_class("char-counter")
        self.title_row.add_suffix(self.char_counter)
        self.title_row.connect("changed", self._on_title_changed)
        self.title_row.connect("entry-activated", lambda *_: self.go_live())
        det.add(self.title_row)
        self.comments_row = Adw.SwitchRow(title=_("Allow comments"), active=True)
        self.gifts_row = Adw.SwitchRow(title=_("Allow gifts"), active=True)
        self.mature_row = Adw.SwitchRow(title=_("Mature content (18+)"), active=False)
        for r in (self.comments_row, self.gifts_row, self.mature_row):
            det.add(r)
        adv = Adw.ExpanderRow(title=_("Advanced"))
        self.topic_row = Adw.SpinRow.new_with_range(0, 2**53, 1)
        self.topic_row.set_title(_("Topic ID"))
        self.topic_row.set_subtitle(_("Optional, leave 0 if unsure"))
        adv.add_row(self.topic_row)
        det.add(adv)
        page.add(det)

        # OBS
        og = Adw.PreferencesGroup(title=_("OBS Studio"))
        self.obs_switch = Adw.SwitchRow(title=_("Control OBS automatically"),
                                        subtitle=_("LiveBridge sets the stream key and starts/stops streaming for you"),
                                        active=True)
        self.obs_switch.connect("notify::active", lambda *_: self._render_obs())
        og.add(self.obs_switch)
        self.obs_row = Adw.ActionRow(title=_("OBS"))
        self.obs_icon = Gtk.Image()
        self.obs_row.add_prefix(self.obs_icon)
        self.obs_setup_btn = Gtk.Button(label=_("Set up"), valign=Gtk.Align.CENTER, visible=False)
        self.obs_setup_btn.add_css_class("suggested-action")
        self.obs_setup_btn.connect("clicked", lambda *_: self.setup_obs())
        self.obs_row.add_suffix(self.obs_setup_btn)
        og.add(self.obs_row)
        page.add(og)

        tb.append(page)
        bottom = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=12, margin_bottom=24)
        self.golive_btn = Gtk.Button(label=_("Go LIVE"))
        self.golive_btn.add_css_class("golive-button")
        self.golive_btn.connect("clicked", lambda *_: self.go_live())
        bottom.append(self.golive_btn)
        tb.append(bottom)
        return tb

    def _on_title_changed(self, row):
        text = row.get_text()
        if len(text) > TITLE_MAX:
            row.set_text(text[:TITLE_MAX])
            row.set_position(-1)
            return
        self.char_counter.set_label(f"{len(text)}/{TITLE_MAX}")
        if len(text) >= TITLE_MAX:
            self.char_counter.add_css_class("full")
        else:
            self.char_counter.remove_css_class("full")
        self.golive_btn.set_sensitive(bool(text.strip()))

    def show_setup(self):
        u = self.client.user
        name = u.get("name") or u.get("username") or u.get("id", "")
        self.account_row.set_title(GLib.markup_escape_text(name))
        self.account_row.set_subtitle(f"@{u['username']}" if u.get("username") else f"ID {u.get('id', '')}")
        self.avatar.set_text(name)
        self.title_widget.set_subtitle(name)
        self._load_avatar(u.get("avatar"))
        self.resume_group.set_visible(bool(self.client.state.get("room")))
        if not self.title_row.get_text():
            self.title_row.set_text(self.client.state.get("last_title") or "")
        self._on_title_changed(self.title_row)
        self.stack.set_visible_child_name("setup")
        self._poll_obs()

        def done(info, err):
            if err:
                if isinstance(err, NotLoggedIn):
                    return self.error(err)
                return self.toast(str(err), 5)
            if not self.title_row.get_text() and info.get("title"):
                self.title_row.set_text(info["title"][:TITLE_MAX])
            self._cover_uri = (info.get("cover") or {}).get("uri", "")
            notice = info.get("block_prompt") or info.get("go_live_prompt")
            if (info.get("ban_status") or {}).get("is_ban") or notice:
                self.toast(notice or "LIVE ban active on this account", 8)
        bg(self.client.create_info, done)

    def _load_avatar(self, url):
        if not url:
            return

        def fetch():
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read()

        def done(data, err):
            if data:
                try:
                    self.avatar.set_custom_image(Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)))
                except GLib.Error:
                    pass
        bg(fetch, done)

    # ------------------------------------------------------------------ OBS status
    def _poll_obs(self):
        visible = self.stack.get_visible_child_name()
        if visible == "setup" and not self._obs_polling:
            self._obs_polling = True

            def done(st, err):
                self._obs_polling = False
                self._obs_state = st if not err else obs.NOT_REACHABLE
                self._render_obs()
            bg(obs.status, done)
        return True

    def _render_obs(self):
        st = self._obs_state
        if not self.obs_switch.get_active():
            self.obs_row.set_subtitle(_("Manual mode"))
            self.obs_icon.set_from_icon_name("edit-paste-symbolic")
            self.obs_icon.set_css_classes([])
            self.obs_setup_btn.set_visible(False)
            return
        texts = {
            obs.READY: (_("Connected, ready to stream"), "object-select-symbolic", "status-ok"),
            obs.CLOSED: (_("Closed — LiveBridge will open it"), "media-playback-start-symbolic", "status-ok"),
            obs.NEEDS_SETUP: (_("WebSocket server is off") + "\n" +
                              _("Close OBS and press Set up, or enable it in Tools › WebSocket Server Settings"),
                              "dialog-warning-symbolic", "status-warn"),
            obs.NOT_REACHABLE: (_("Can't connect — restart OBS"), "dialog-warning-symbolic", "status-warn"),
            obs.NOT_INSTALLED: (_("Not installed") + "\n" +
                                _("Install OBS Studio, or turn off automatic control and paste the key yourself"),
                                "dialog-error-symbolic", "status-bad"),
        }
        sub, icon, css = texts.get(st, ("…", "content-loading-symbolic", "dim-label"))
        self.obs_row.set_subtitle(sub)
        self.obs_icon.set_from_icon_name(icon)
        self.obs_icon.set_css_classes([css])
        self.obs_setup_btn.set_visible(st == obs.NEEDS_SETUP)

    def setup_obs(self):
        def done(_res, err):
            if err:
                return self.error(err)
            self.toast(_("OBS configured"))
            self._poll_obs()
        bg(obs.enable_websocket, done)

    # ------------------------------------------------------------------ go live
    def go_live(self, resume=False):
        if self.session or self.stack.get_visible_child_name() == "busy":
            return
        title = self.title_row.get_text().strip()
        if not resume and not title:
            return
        control = self.obs_switch.get_active()
        if control and self._obs_state == obs.NOT_INSTALLED:
            control = False
        self.busy(_("Starting your LIVE…"))
        cover = getattr(self, "_cover_uri", "")

        def work():
            room = self.client.state.get("room") if resume else None
            if not room:
                room = self.client.create_room(
                    title, topic_id=int(self.topic_row.get_value()), cover_uri=cover,
                    age_restricted=self.mature_row.get_active(), allow_comments=self.comments_row.get_active(),
                    allow_gifts=self.gifts_row.get_active())
                self.client.state.update(last_title=title)
            obs_err = None
            if control:
                GLib.idle_add(lambda: (self.busy_label.set_label(_("Opening OBS…")), False)[1])
                server, key = split_rtmp(room["push_url"])
                try:
                    obs.start_streaming(server, key)
                except obs.ObsError as e:
                    obs_err = e
            return room, obs_err

        def done(res, err):
            if err:
                self.stack.set_visible_child_name("setup")
                return self.error(err)
            room, obs_err = res
            self._start_session(room, control and not obs_err)
            if obs_err:
                self.error(obs_err, heading=_("OBS"))
        bg(work, done)

    def end_stale_live(self):
        def done(_r, err):
            self.resume_group.set_visible(False)
            if err and not isinstance(err, ApiError):
                return self.error(err)
            self.toast(_("LIVE ended"))
        bg(self.client.end_live, done)

    # ======================================================================== live
    def _build_live(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        clamp = Adw.Clamp(maximum_size=560, margin_start=12, margin_end=12, vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=14, margin_bottom=14)

        # compact header: LIVE · timer ............ viewers · likes
        head = Gtk.Box(spacing=12)
        pill = Gtk.Label(label=_("LIVE"), valign=Gtk.Align.CENTER)
        pill.add_css_class("live-pill")
        head.append(pill)
        self.timer_label = Gtk.Label(label="00:00", valign=Gtk.Align.CENTER)
        self.timer_label.add_css_class("live-timer")
        head.append(self.timer_label)
        head.append(Gtk.Box(hexpand=True))
        stats = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.viewers_label = Gtk.Label(label="", xalign=1)
        self.viewers_label.add_css_class("viewers-label")
        self.likes_label = Gtk.Label(label="", xalign=1)
        self.likes_label.add_css_class("viewers-label")
        stats.append(self.viewers_label)
        stats.append(self.likes_label)
        head.append(stats)
        box.append(head)
        self.live_title = Gtk.Label(wrap=True, xalign=0)
        self.live_title.add_css_class("heading")
        box.append(self.live_title)

        # tabs: Chat | Stream
        self.live_tabs = Adw.ViewStack(vexpand=True)
        switcher = Adw.InlineViewSwitcher(stack=self.live_tabs) if hasattr(Adw, "InlineViewSwitcher") \
            else Adw.ViewSwitcher(stack=self.live_tabs, policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_halign(Gtk.Align.CENTER)
        box.append(switcher)

        # --- chat tab
        chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.chat_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.chat_scroller.add_css_class("card")
        self.chat_scroller.add_css_class("chat-card")
        self.chat_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.chat_list.add_css_class("chat-list")
        self.chat_placeholder = Gtk.Label(label=_("Comments will show up here"), margin_top=40)
        self.chat_placeholder.add_css_class("dim-label")
        self.chat_list.set_placeholder(self.chat_placeholder)
        self.chat_scroller.set_child(self.chat_list)
        chat_box.append(self.chat_scroller)
        opts = Gtk.Box(spacing=6)
        self.tts_toggle = Gtk.ToggleButton(tooltip_text=_("Read the chat aloud"))
        tcontent = Adw.ButtonContent(icon_name="audio-speakers-symbolic", label=_("Read aloud"))
        self.tts_toggle.set_child(tcontent)
        self.tts_toggle_content = tcontent
        self.tts_toggle.add_css_class("flat")
        self._tts_toggle_handler = self.tts_toggle.connect("toggled", self._on_tts_toggled)
        opts.append(self.tts_toggle)
        self.tts_menu = Gtk.MenuButton(icon_name="pan-down-symbolic", tooltip_text=_("Voice settings"),
                                       popover=self._build_tts_popover())
        self.tts_menu.add_css_class("flat")
        opts.append(self.tts_menu)
        opts.append(Gtk.Box(hexpand=True))
        self.show_joins = Gtk.CheckButton(label=_("Show who joins"), active=False)
        opts.append(self.show_joins)
        chat_box.append(opts)
        page = self.live_tabs.add_titled(chat_box, "chat", _("Chat"))
        page.set_icon_name("chat-message-new-symbolic")

        # --- stream tab
        stream = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        st = Adw.PreferencesGroup()
        self.hb_row = Adw.ActionRow(title=_("Heartbeat"))
        self.hb_icon = Gtk.Image()
        self.hb_row.add_prefix(self.hb_icon)
        st.add(self.hb_row)
        self.obs_live_row = Adw.ActionRow(title=_("OBS"))
        self.obs_live_icon = Gtk.Image()
        self.obs_live_row.add_prefix(self.obs_live_icon)
        st.add(self.obs_live_row)
        stream.append(st)

        self.keys_group = Adw.PreferencesGroup(title=_("Stream settings"),
                                               description=_("Paste these in OBS › Settings › Stream › Custom"))
        self.server_row = Adw.ActionRow(title=_("Server"), subtitle_selectable=True)
        b = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_("Copy"))
        b.add_css_class("flat")
        b.connect("clicked", lambda *_: self.copy(self.session.server if self.session else ""))
        self.server_row.add_suffix(b)
        self.keys_group.add(self.server_row)
        self.key_row = Adw.ActionRow(title=_("Stream key"))
        self.key_show = Gtk.ToggleButton(icon_name="view-reveal-symbolic", valign=Gtk.Align.CENTER,
                                         tooltip_text=_("Show"))
        self.key_show.add_css_class("flat")
        self.key_show.connect("toggled", lambda *_: self._render_key())
        kb = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_("Copy"))
        kb.add_css_class("flat")
        kb.connect("clicked", lambda *_: self.copy(self.session.key if self.session else ""))
        self.key_row.add_suffix(self.key_show)
        self.key_row.add_suffix(kb)
        self.keys_group.add(self.key_row)
        stream.append(self.keys_group)
        sscroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sscroll.set_child(stream)
        page = self.live_tabs.add_titled(sscroll, "stream", _("Stream"))
        page.set_icon_name("network-transmit-receive-symbolic")
        box.append(self.live_tabs)

        clamp.set_child(box)
        outer.append(clamp)
        bottom = Gtk.Box(halign=Gtk.Align.CENTER, margin_bottom=18)
        self.end_btn = Gtk.Button(label=_("End LIVE"))
        self.end_btn.add_css_class("pill")
        self.end_btn.add_css_class("end-button")
        self.end_btn.connect("clicked", lambda *_: self.confirm_end())
        bottom.append(self.end_btn)
        outer.append(bottom)
        return outer

    # ------------------------------------------------------------------ chat
    CHAT_MAX_ROWS = 400
    GIFT_STREAK_TIMEOUT = 5  # seconds without updates before a combo counts as finished

    def _chat_clear(self):
        while (row := self.chat_list.get_row_at_index(0)) is not None:
            self.chat_list.remove(row)
        for _lbl, _n, tid in getattr(self, "_gift_rows", {}).values():
            if tid:
                GLib.source_remove(tid)
        self._gift_rows = {}
        self._chat_rows = 0
        self._likes_total = 0
        self._diamonds = 0
        self._render_stats()

    def _render_stats(self):
        parts = []
        if self._likes_total:
            parts.append(f"❤️ {fmt_int(self._likes_total)}")
        if self._diamonds:
            parts.append(f"💎 {fmt_int(self._diamonds)}")
        self.likes_label.set_label("  ".join(parts))

    def _chat_row(self, markup, css=None):
        adj = self.chat_scroller.get_vadjustment()
        at_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 48
        lbl = Gtk.Label(xalign=0, wrap=True, selectable=True, use_markup=True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_markup(markup)
        lbl.add_css_class("chat-line")
        if css:
            lbl.add_css_class(css)
        self.chat_list.append(lbl)
        self._chat_rows += 1
        if self._chat_rows > self.CHAT_MAX_ROWS:
            first = self.chat_list.get_row_at_index(0)
            if first:
                self.chat_list.remove(first)
                self._chat_rows -= 1
        if at_bottom:
            GLib.idle_add(lambda: (adj.set_value(adj.get_upper() - adj.get_page_size()), False)[1])
        return lbl

    @staticmethod
    def _gift_markup(e):
        who = GLib.markup_escape_text(e.nickname or e.username or "?")
        n = f" <b>×{e.count}</b>" if e.count > 1 else ""
        return "🎁 " + _("{who} sent {gift}", who=f"<b>{who}</b>", gift=GLib.markup_escape_text(e.gift)) + n

    def _on_gift(self, e):
        entry = self._gift_rows.get(e.streak_key)
        if entry:
            lbl, _n, tid = entry
            if tid:
                GLib.source_remove(tid)
            lbl.set_markup(self._gift_markup(e))
        else:
            lbl = self._chat_row(self._gift_markup(e), "chat-gift")
        if e.streaking:
            tid = GLib.timeout_add_seconds(self.GIFT_STREAK_TIMEOUT, self._finish_gift, e.streak_key)
            self._gift_rows[e.streak_key] = (lbl, e, tid)
        else:
            self._gift_rows[e.streak_key] = (lbl, e, 0)
            self._finish_gift(e.streak_key)

    def _finish_gift(self, key):
        entry = self._gift_rows.pop(key, None)
        if entry:
            _lbl, e, _tid = entry
            self._diamonds += e.diamonds * e.count
            self._render_stats()
            if self._tts_wants("gifts"):
                self.speaker.say(_("{who} sent {n} {gift}", who=tts.clean_name(e.nickname, e.username),
                                   n=e.count, gift=e.gift))
        return False

    def _on_chat_events(self, events):
        if not self.session:
            return
        esc = GLib.markup_escape_text
        for e in events:
            who = esc(e.nickname or e.username or "?")
            name = tts.clean_name(e.nickname, e.username)
            if e.kind == "chat":
                self._chat_row(f"<b>{who}</b>  {esc(e.text)}")
                if self._tts_wants("comments") and tts.clean(e.text):
                    self.speaker.say(_("{who} says: {text}", who=name, text=e.text))
            elif e.kind == "gift":
                self._on_gift(e)
            elif e.kind == "follow":
                self._chat_row("➕ " + _("{who} followed you", who=f"<b>{who}</b>"), "chat-event")
                if self._tts_wants("follows"):
                    self.speaker.say(_("{who} followed you", who=name))
            elif e.kind == "share":
                self._chat_row("↗ " + _("{who} shared the LIVE", who=f"<b>{who}</b>"), "chat-event")
            elif e.kind == "join":
                if self.show_joins.get_active():
                    self._chat_row("👋 " + _("{who} joined", who=who), "chat-join")
            elif e.kind == "like":
                self._likes_total = max(self._likes_total + e.count, e.total)
                self._render_stats()

    def _on_chat_error(self, err):
        if self.session:
            self._chat_row(GLib.markup_escape_text(_("Chat unavailable: {err}", err=_(str(err.args[0]) if err.args else str(err)))),
                           "chat-join")

    # ------------------------------------------------------------------ text to speech
    def _tts_settings(self):
        d = {"enabled": False, "comments": True, "gifts": True, "follows": True, "rate": 1.0}
        d.update(self.client.state.get("tts") or {})
        return d

    def _tts_save(self, **kw):
        d = self._tts_settings()
        d.update(kw)
        self.client.state.update(tts=d)

    def _tts_wants(self, what):
        st = self._tts_settings()
        return st["enabled"] and st.get(what, True) and self.speaker.engine is not None

    def _build_tts_popover(self):
        st = self._tts_settings()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8, margin_bottom=8,
                      margin_start=8, margin_end=8)
        title = Gtk.Label(label=_("Read aloud"), xalign=0)
        title.add_css_class("heading")
        box.append(title)
        for key, label in (("comments", _("Comments")), ("gifts", _("Gifts")), ("follows", _("New followers"))):
            cb = Gtk.CheckButton(label=label, active=st.get(key, True))
            cb.connect("toggled", lambda b, k=key: self._tts_save(**{k: b.get_active()}))
            box.append(cb)
        box.append(Gtk.Label(label=_("Speed"), xalign=0, margin_top=6))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.6, 1.8, 0.1)
        scale.set_value(st.get("rate", 1.0))
        scale.set_size_request(200, -1)
        scale.connect("value-changed", lambda sc: (self._tts_save(rate=round(sc.get_value(), 1)),
                                                   setattr(self.speaker, "rate", sc.get_value())))
        box.append(scale)
        row = Gtk.Box(spacing=6, margin_top=6)
        skip = Gtk.Button(label=_("Skip"), tooltip_text=_("Stop reading and clear the queue"))
        skip.connect("clicked", lambda *_: self.speaker.skip())
        row.append(skip)
        test = Gtk.Button(label=_("Test"))
        test.connect("clicked", lambda *_: self._tts_test())
        row.append(test)
        box.append(row)
        self.tts_engine_label = Gtk.Label(xalign=0, wrap=True, margin_top=6)
        self.tts_engine_label.add_css_class("dim-label")
        self.tts_engine_label.add_css_class("caption")
        box.append(self.tts_engine_label)
        self.tts_download_btn = Gtk.Button(label=_("Download natural voice"))
        self.tts_download_btn.connect("clicked", lambda *_: self._tts_confirm_download())
        box.append(self.tts_download_btn)
        pop = Gtk.Popover(child=box)
        pop.connect("show", lambda *_: self._render_tts_engine())
        return pop

    def _render_tts_engine(self):
        eng = self.speaker.refresh_engine()
        names = {"piper": _("Natural voice (Piper)"), "spd-say": "Speech Dispatcher",
                 "espeak-ng": "eSpeak NG", "espeak": "eSpeak", None: _("No voice installed")}
        self.tts_engine_label.set_label(_("Voice: {name}", name=names.get(eng, eng)))
        self.tts_download_btn.set_visible(eng != "piper" and not getattr(self, "_tts_downloading", False))

    def _set_tts_toggle(self, active):
        self.tts_toggle.handler_block(self._tts_toggle_handler)
        self.tts_toggle.set_active(active)
        self.tts_toggle.handler_unblock(self._tts_toggle_handler)

    def _on_tts_toggled(self, btn):
        on = btn.get_active()
        if on and not self.speaker.refresh_engine():
            self._set_tts_toggle(False)
            self._tts_confirm_download(enable_after=True)
            return
        self._tts_save(enabled=on)
        if not on:
            self.speaker.skip()

    def _tts_test(self):
        if not self.speaker.refresh_engine():
            return self._tts_confirm_download()
        self.speaker.say(_("Chat reading is on. {who} says: hello!", who="LiveBridge"))

    def _tts_confirm_download(self, enable_after=False):
        if getattr(self, "_tts_downloading", False):
            return
        d = Adw.AlertDialog.new(_("Download a natural voice?"),
                                _("LiveBridge will download Piper and a voice for your language "
                                  "(about {mb} MB) into your home folder. It works offline afterwards.",
                                  mb=tts.PIPER_DOWNLOAD_MB))
        d.add_response("cancel", _("Cancel"))
        d.add_response("download", _("Download"))
        d.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: r == "download" and self._tts_download(enable_after))
        d.present(self)

    def _tts_download(self, enable_after):
        self._tts_downloading = True
        self.tts_toggle.set_sensitive(False)

        def progress(frac, _what):
            GLib.idle_add(lambda: (self.tts_toggle_content.set_label(
                _("Downloading… {p}%", p=int(frac * 100))), False)[1])

        def done(_r, err):
            self._tts_downloading = False
            self.tts_toggle.set_sensitive(True)
            self.tts_toggle_content.set_label(_("Read aloud"))
            if err:
                return self.error(err, heading=_("Voice download failed"))
            self.speaker.refresh_engine()
            self.toast(_("Natural voice ready"))
            if enable_after:
                self._set_tts_toggle(True)
                self._tts_save(enabled=True)
            self._tts_test()
        bg(lambda: tts.install_piper(progress), done)

    def _render_key(self):
        if not self.session:
            return
        k = self.session.key
        self.key_row.set_subtitle(GLib.markup_escape_text(k) if self.key_show.get_active() else "•" * 24)
        self.key_row.set_subtitle_selectable(self.key_show.get_active())

    def _start_session(self, room, control_obs):
        self.session = LiveSession(
            self.client, room, control_obs=control_obs,
            on_update=lambda info: GLib.idle_add(lambda: (self._on_live_update(info), False)[1]),
            on_ended=lambda reason: GLib.idle_add(lambda: (self._on_live_ended(reason), False)[1]))
        self.live_title.set_label(room.get("title") or "")
        self.server_row.set_subtitle(GLib.markup_escape_text(self.session.server))
        self.key_show.set_active(False)
        self._render_key()
        self.viewers_label.set_label("")
        self._chat_clear()
        self.live_tabs.set_visible_child_name("chat")
        self.chat_reader = ChatReader(
            self.client, room["id"],
            on_events=lambda evs: GLib.idle_add(lambda: (self._on_chat_events(evs), False)[1]),
            on_error=lambda err: GLib.idle_add(lambda: (self._on_chat_error(err), False)[1]))
        self.end_btn.set_sensitive(True)
        self.end_btn.set_label(_("End LIVE"))
        self.hb_row.set_subtitle(_("Connected to TikTok"))
        self.hb_icon.set_from_icon_name("object-select-symbolic")
        self.hb_icon.set_css_classes(["status-ok"])
        if control_obs:
            self.obs_live_row.set_subtitle("…")
            self.obs_live_icon.set_from_icon_name("content-loading-symbolic")
        else:
            self.obs_live_row.set_subtitle(_("Manual mode"))
            self.obs_live_icon.set_from_icon_name("edit-paste-symbolic")
        self.obs_live_icon.set_css_classes([])
        self.stack.set_visible_child_name("live")
        self._tick()
        self._timer_id = GLib.timeout_add_seconds(1, self._tick)
        self.session.start()
        self.chat_reader.start()

    def _tick(self):
        if not self.session:
            self._timer_id = 0
            return False
        self.timer_label.set_label(fmt_duration(time.time() - self.session.started))
        return True

    def _on_live_update(self, info):
        if not self.session:
            return
        if info["heartbeat_ok"]:
            self.hb_row.set_subtitle(_("Connected to TikTok"))
            self.hb_icon.set_from_icon_name("object-select-symbolic")
            self.hb_icon.set_css_classes(["status-ok"])
        else:
            self.hb_row.set_subtitle(_("Connection problems, retrying… ({n})", n=info["ping_failures"]))
            self.hb_icon.set_from_icon_name("dialog-warning-symbolic")
            self.hb_icon.set_css_classes(["status-warn"])
        if info["viewers"] is not None:
            self.viewers_label.set_label(f"👁 {fmt_int(info['viewers'])} {_('viewers')}")
        if self.session.control_obs:
            s = info["obs"]
            if s is None:
                self.obs_live_row.set_subtitle(_("Not reachable"))
                icon, css = "dialog-warning-symbolic", "status-warn"
            elif s["reconnecting"]:
                self.obs_live_row.set_subtitle(_("Reconnecting…"))
                icon, css = "view-refresh-symbolic", "status-warn"
            elif s["active"]:
                kbps = f"{info['bitrate_kbps']:.0f}" if info["bitrate_kbps"] else "…"
                self.obs_live_row.set_subtitle(_("Streaming · {kbps} kbps", kbps=kbps))
                icon, css = "media-record-symbolic", "status-ok"
            else:
                self.obs_live_row.set_subtitle(_("Not streaming — press Start Streaming in OBS"))
                icon, css = "dialog-warning-symbolic", "status-warn"
            self.obs_live_icon.set_from_icon_name(icon)
            self.obs_live_icon.set_css_classes([css])

    def confirm_end(self, then_quit=False):
        if then_quit:
            d = Adw.AlertDialog.new(_("Quit and end your LIVE?"),
                                    _("Closing LiveBridge ends the LIVE, because TikTok needs a heartbeat "
                                      "while you stream."))
            d.add_response("cancel", _("Keep streaming"))
            d.add_response("end", _("End and quit"))
        else:
            d = Adw.AlertDialog.new(_("End your LIVE?"), _("Your viewers will see that the LIVE has ended."))
            d.add_response("cancel", _("Keep streaming"))
            d.add_response("end", _("End LIVE"))
        d.set_response_appearance("end", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")

        def on_resp(_d, resp):
            if resp == "end":
                self._closing_after_end = then_quit
                self.end_live()
        d.connect("response", on_resp)
        d.present(self)

    def end_live(self):
        if not self.session:
            return
        self.end_btn.set_sensitive(False)
        self.end_btn.set_label(_("Ending…"))
        s = self.session

        def done(_r, err):
            if err:
                self.toast(str(err), 5)
        bg(s.end, done)

    def _on_live_ended(self, reason):
        self.session = None
        self.speaker.skip()
        if getattr(self, "chat_reader", None):
            self.chat_reader.stop()
            self.chat_reader = None
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        if self._closing_after_end:
            self.destroy()
            return
        self.toast(_("TikTok ended the LIVE") if reason == "server" else _("LIVE ended"), 4)
        self.show_setup()

    def _on_close_request(self, *_):
        if self.session:
            self.confirm_end(then_quit=True)
            return True
        return False
