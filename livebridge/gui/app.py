import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, APP_NAME, __version__, urlhandler  # noqa: E402
from ..api import URL_SCHEME  # noqa: E402
from ..i18n import _  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ICONS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "data", "icons")


class LiveBridgeApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name(APP_NAME)
        self.window = None
        self._pending_uri = None
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)
        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *_: self.window and self.window.close())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if os.path.isdir(REPO_ICONS):
            Gtk.IconTheme.get_for_display(display).add_search_path(REPO_ICONS)
        Gtk.Window.set_default_icon_name(APP_ID)
        try:
            urlhandler.ensure_registered()
        except OSError:
            pass

    def _ensure_window(self):
        if not self.window:
            from .window import MainWindow
            self.window = MainWindow(application=self)
        return self.window

    def do_activate(self):
        self._ensure_window().present()

    def do_open(self, files, n_files, hint):
        win = self._ensure_window()
        win.present()
        for f in files:
            uri = f.get_uri()
            if uri.startswith(f"{URL_SCHEME}://"):
                win.handle_login_uri(uri)

    def _on_about(self, *_):
        d = Adw.AboutDialog(
            application_name=APP_NAME, application_icon=APP_ID, version=__version__,
            developer_name="LiveBridge contributors", license_type=Gtk.License.MIT_X11,
            comments=_("Go LIVE on TikTok from Linux") + "\n\n" +
            _("Unofficial. Not affiliated with TikTok or ByteDance."),
            website="https://github.com/cpstest-tech/livebridge",
            issue_url="https://github.com/cpstest-tech/livebridge/issues")
        d.present(self.window)


def main():
    uris = [a for a in sys.argv[1:] if a.startswith(f"{URL_SCHEME}://")]
    app = LiveBridgeApp()
    if uris:
        # Launched by the browser after login: hand the link to the running app or to a waiting CLI.
        app.register(None)
        if not app.get_is_remote() and urlhandler.cli_is_waiting():
            urlhandler.set_pending(uris[0])
            return 0
    return app.run(sys.argv)
