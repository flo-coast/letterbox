#!/bin/bash
# Installs letterbox for the current user (run as your user, not with sudo; it asks for sudo only for
# the GW2 logger service): files to ~/.local/share/letterbox, "letterbox" and "letterbox-settings"
# commands to ~/.local/bin, the settings app to the app menu, the hotkey KWin script, the GW2 event data.
set -eu
[ "$(id -u)" != 0 ] || { echo "run as your normal user (it asks for sudo itself)"; exit 1; }
cd "$(dirname "$0")"
SRC=$PWD
DEST=~/.local/share/letterbox
BIN=~/.local/bin

missing=""
python3 -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk' 2>/dev/null || missing+=" GTK4-for-Python(python-gobject+gtk4)"
python3 -c 'import cairo' 2>/dev/null || missing+=" python-cairo"
python3 -c 'import numpy' 2>/dev/null || missing+=" python-numpy"
for c in qdbus6 kscreen-doctor ss; do command -v $c >/dev/null || missing+=" $c"; done
[ -z "$missing" ] || { echo "missing:$missing"; echo "(Arch: sudo pacman -S python-gobject gtk4 python-cairo python-numpy qt6-tools kscreen iproute2)"; exit 1; }

mkdir -p "$DEST/data" "$BIN" ~/.local/share/applications ~/.local/share/kwin/scripts
install -m755 letterbox bar.py settings.py "$DEST/"
install -m644 sensors.py gw2events.py capture.py placement.js "$DEST/"
ln -sf "$DEST/letterbox" "$BIN/letterbox"
ln -sf "$DEST/settings.py" "$BIN/letterbox-settings"
sed "s|@EXEC@|$DEST/settings.py|" letterbox.settings.desktop > ~/.local/share/applications/letterbox.settings.desktop

echo "Downloading the GW2 event timer data..."
python3 "$DEST/gw2events.py" --update || echo "  failed: try again later with: python3 $DEST/gw2events.py --update"

# hotkeys (Meta+Shift+B bar mode, Meta+Shift+M lag marker): a KWin script that is always loaded
rm -rf ~/.local/share/kwin/scripts/letterbox-hotkeys
cp -r kwin/letterbox-hotkeys ~/.local/share/kwin/scripts/
kwriteconfig6 --file kwinrc --group Plugins --key letterbox-hotkeysEnabled true
qdbus6 org.kde.KWin /KWin reconfigure >/dev/null 2>&1 || true

# MangoHud logs give the FPS tiles: use the folder an existing GW2 MangoHud config already logs to
mf=""
for f in ~/.config/MangoHud/wine-Gw2-64.conf ~/.config/MangoHud/MangoHud.conf; do
    [ -f "$f" ] && mf=$(sed -n 's/^[[:space:]]*output_folder[[:space:]]*=[[:space:]]*//p' "$f" | tail -1)
    [ -n "$mf" ] && break
done
if [ -n "$mf" ]; then
    python3 - "$mf" <<'PY'
import json, os, sys
p = os.path.expanduser("~/.config/letterbox/config.json")
try: c = json.load(open(p))
except Exception: c = {}
c["mangohud_dir"] = sys.argv[1]
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump(c, open(p, "w"), indent=1)
PY
    echo "FPS tiles: reading MangoHud logs from $mf"
else
    echo "FPS tiles: let MangoHud log GW2's frames, e.g. in ~/.config/MangoHud/wine-Gw2-64.conf:"
    echo "  output_folder=$HOME/.local/share/letterbox/mangohud"
    echo "  autostart_log=5"
    echo "  log_interval=0"
    mkdir -p ~/.local/share/letterbox/mangohud
fi

echo
read -r -p "Install the GW2 logger service (map, combat, character, thread tiles; needs sudo)? [Y/n] " a
if [ "${a:-y}" != n ] && [ "${a:-y}" != N ]; then
    sudo install -Dm755 "$SRC/logger/letterbox-gw2log.py" /usr/local/lib/letterbox/letterbox-gw2log.py
    sudo install -Dm644 "$SRC/logger/letterbox-gw2log.service" /etc/systemd/system/letterbox-gw2log.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now letterbox-gw2log.service
fi

echo
echo "Done. Next:"
echo "  1. Steam → Guild Wars 2 → Properties → Launch options:  letterbox run %command%"
echo "     (with other options, e.g.:  letterbox run mangohud %command%)"
echo "  2. Close GW2, then turn letterbox mode on:  letterbox on   (or the switch in 'Letterbox settings')"
