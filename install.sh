#!/usr/bin/env bash
# LiveBridge installer: per-user, no root needed.
#   ./install.sh            install / update
#   ./install.sh --uninstall
set -euo pipefail

APP_ID="io.github.cpstest_tech.LiveBridge"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$DATA/livebridge"
BIN="$HOME/.local/bin"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor/scalable/apps"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

if [[ "${1:-}" == "--uninstall" ]]; then
  bold "Uninstalling LiveBridge"
  rm -rf "$PREFIX" "$BIN/livebridge" "$APPS/$APP_ID.desktop" "$APPS/$APP_ID.UrlHandler.desktop" "$ICONS/$APP_ID.svg"
  command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
  ok "Removed. Your login is kept in ~/.config/livebridge (delete it to log out completely)."
  exit 0
fi

bold "Checking dependencies"
missing=0
command -v python3 >/dev/null || { warn "python3 not found"; missing=1; }
if ! python3 - <<'EOF' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5)
EOF
then
  missing=1
  warn "GTK 4 + libadwaita >= 1.5 for Python not found. Install them with:"
  if   command -v pacman  >/dev/null; then echo "      sudo pacman -S --needed python-gobject gtk4 libadwaita"
  elif command -v apt     >/dev/null; then echo "      sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
  elif command -v dnf     >/dev/null; then echo "      sudo dnf install python3-gobject gtk4 libadwaita"
  elif command -v zypper  >/dev/null; then echo "      sudo zypper install python3-gobject typelib-1_0-Adw-1 typelib-1_0-Gtk-4_0"
  else echo "      (install PyGObject, GTK 4 and libadwaita with your package manager)"; fi
fi
[[ $missing == 0 ]] && ok "Python, GTK 4 and libadwaita"
if command -v obs >/dev/null || flatpak info com.obsproject.Studio >/dev/null 2>&1; then
  ok "OBS Studio"
else
  warn "OBS Studio not found (optional, but you need something to stream with)"
fi
[[ $missing == 0 ]] || { echo; echo "Install the missing packages and run ./install.sh again."; exit 1; }

bold "Installing"
mkdir -p "$PREFIX" "$BIN" "$APPS" "$ICONS"
rm -rf "$PREFIX/livebridge"
cp -r "$SRC/livebridge" "$PREFIX/"
find "$PREFIX" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cat > "$BIN/livebridge" <<EOF
#!/bin/sh
export PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m livebridge "\$@"
EOF
chmod +x "$BIN/livebridge"
ok "livebridge command -> $BIN/livebridge"

sed "s|^Exec=.*|Exec=$BIN/livebridge %u|" "$SRC/data/$APP_ID.desktop" > "$APPS/$APP_ID.desktop"
cp "$SRC/data/icons/$APP_ID.svg" "$ICONS/"
rm -f "$APPS/$APP_ID.UrlHandler.desktop"
xdg-mime default "$APP_ID.desktop" x-scheme-handler/live-studio-app 2>/dev/null || true
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
ok "App menu entry and login link handler"

command -v livebridge >/dev/null || warn "$BIN is not in your PATH; the app menu entry works anyway"
echo
bold "Done! Open LiveBridge from your app menu, or run: livebridge"
