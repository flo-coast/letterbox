#!/bin/bash
# Installs letterbox for the current user. Run it as your user, it asks for sudo only where needed
# (missing packages, the GW2 logger service). It installs the files to ~/.local/share/letterbox, the
# "letterbox" command, the settings app and the hotkeys, then sets up what it can find on its own
# (MangoHud logging, GW2's Steam launch option, letterbox mode) and shows what it detected.
set -eu
[ "$(id -u)" != 0 ] || { echo "run as your normal user (it asks for sudo itself)"; exit 1; }
cd "$(dirname "$0")"
SRC=$PWD
DEST=~/.local/share/letterbox
BIN=~/.local/bin
ask() { local a; read -r -p "$1 [Y/n] " a; [ "${a:-y}" != n ] && [ "${a:-y}" != N ]; }

# ---------- packages ----------
pymods_ok() {
    python3 -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk; import cairo, numpy' 2>/dev/null
}
if ! pymods_ok; then
    if command -v pacman >/dev/null; then pkgs=(sudo pacman -S --needed python-gobject gtk4 python-cairo python-numpy)
    elif command -v dnf >/dev/null; then pkgs=(sudo dnf install python3-gobject gtk4 python3-cairo python3-numpy)
    elif command -v apt-get >/dev/null; then pkgs=(sudo apt-get install python3-gi gir1.2-gtk-4.0 python3-gi-cairo python3-numpy)
    elif command -v zypper >/dev/null; then pkgs=(sudo zypper install python3-gobject python3-gobject-cairo typelib-1_0-Gtk-4_0 python3-numpy)
    else pkgs=(); fi
    echo "letterbox needs GTK 4 for Python, pycairo and NumPy."
    if [ ${#pkgs[@]} -gt 0 ] && ask "Install them with: ${pkgs[*]} ?"; then
        "${pkgs[@]}"
    fi
    pymods_ok || { echo "Still missing. Install PyGObject with GTK 4, pycairo and NumPy, then run ./install.sh again."; exit 1; }
fi
for c in gdbus kscreen-doctor kwriteconfig6 ss; do
    command -v $c >/dev/null || { echo "missing: $c (comes with GLib, KDE Plasma 6 and iproute2)"; exit 1; }
done

# ---------- files ----------
mkdir -p "$DEST/data" "$BIN" ~/.local/share/applications ~/.local/share/kwin/scripts
install -m755 letterbox bar.py settings.py setup.py "$DEST/"
install -m644 sensors.py gw2events.py capture.py placement.js extras.py "$DEST/"
ln -sf "$DEST/letterbox" "$BIN/letterbox"
ln -sf "$DEST/settings.py" "$BIN/letterbox-settings"
sed "s|@EXEC@|$DEST/settings.py|" letterbox.settings.desktop > ~/.local/share/applications/letterbox.settings.desktop

echo "Downloading the GW2 event timer data..."
python3 "$DEST/gw2events.py" --update >/dev/null && echo "  done" ||
    echo "  failed, try again later with the 'Update event data' button in the settings"

# hotkeys (Meta+Shift+B bar mode, Meta+Shift+M lag marker): a KWin script that is always loaded
rm -rf ~/.local/share/kwin/scripts/letterbox-hotkeys
cp -r kwin/letterbox-hotkeys ~/.local/share/kwin/scripts/
kwriteconfig6 --file kwinrc --group Plugins --key letterbox-hotkeysEnabled true
gdbus call --session --dest org.kde.KWin --object-path /KWin --method org.kde.KWin.reconfigure >/dev/null 2>&1 || true

# ---------- setup ----------
echo
python3 "$DEST/setup.py" mangohud || true
[ -n "$("$DEST/letterbox" gfx-path)" ] || python3 "$DEST/setup.py" gfx || true

echo
echo "The GW2 logger service reads the game's MumbleLink (the official overlay interface) for the map,"
echo "combat, character and thread tiles. Under Proton that needs root."
if ask "Install it (sudo)?"; then
    sudo install -Dm755 "$SRC/logger/letterbox-gw2log.py" /usr/local/lib/letterbox/letterbox-gw2log.py
    sudo install -Dm644 "$SRC/logger/letterbox-gw2log.service" /etc/systemd/system/letterbox-gw2log.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now letterbox-gw2log.service
fi

echo
if [ -n "$(python3 -c "import sys; sys.path.insert(0, '$DEST'); import setup, os; g = setup.game_dir(); print(g if g and os.path.isdir(g + '/addons/Nexus') else '')" 2>/dev/null)" ]; then
    echo "You use Nexus. The letterbox bridge addon hands your squad (size, commander, professions) to the bar."
    if ask "Put it into GW2's addons folder?"; then python3 "$DEST/setup.py" bridge || true; fi
    echo
fi

rc=0
python3 "$DEST/setup.py" launch add || rc=$?
if [ $rc = 2 ] && ask "Close Steam now so letterbox can set GW2's launch option?"; then
    steam -shutdown >/dev/null 2>&1 &
    for _ in $(seq 30); do sleep 1; pgrep -x steam >/dev/null || break; done
    python3 "$DEST/setup.py" launch add || true
elif [ $rc = 2 ]; then
    echo "Later, with Steam closed:  letterbox setup-launch"
fi

if [ ! -f ~/.config/letterbox/enabled ] && [ -n "$("$DEST/letterbox" gfx-path)" ] && ! pgrep -x -i gw2-64.exe >/dev/null; then
    echo
    if ask "Turn letterbox mode on now?"; then "$DEST/letterbox" on || true; fi
fi

echo
"$DEST/letterbox" doctor || true
echo
echo "Start GW2 through Steam as usual. 'Letterbox settings' in the app menu sets up the bar."
