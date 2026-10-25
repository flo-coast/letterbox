#!/bin/bash
# Removes letterbox: the program, the hotkeys, the GW2 logger, the Nexus bridge addon and "letterbox run" in
# GW2's Steam launch options (Steam closed). Kept unless you pass --purge: your settings (~/.config/letterbox)
# and your data (lag markers, MangoHud frame logs, the window tile's Firefox profile with its logins, caches).
set -u
[ "$(id -u)" != 0 ] || { echo "run as your normal user"; exit 1; }
L=~/.local/share/letterbox
[ -f ~/.config/letterbox/enabled ] && "$L/letterbox" off
"$L/letterbox" stop >/dev/null 2>&1
if [ -f "$L/setup.py" ]; then
    python3 "$L/setup.py" launch remove ||
        echo "Remove 'letterbox run' from GW2's Steam launch options by hand (or close Steam and run this again)."
    g=$(python3 -c "import sys; sys.path.insert(0, '$L'); import setup; print(setup.game_dir() or '')" 2>/dev/null)
    if [ -n "$g" ] && [ -e "$g/addons/letterbox_bridge.dll" ]; then
        rm -rf "$g/addons/letterbox_bridge.dll" "$g/addons/letterbox" && echo "removed the Nexus bridge addon"
    fi
fi
rm -f ~/.local/bin/letterbox ~/.local/bin/letterbox-settings ~/.local/share/applications/letterbox.settings.desktop
kwriteconfig6 --file kwinrc --group Plugins --key letterbox-hotkeysEnabled false
rm -rf ~/.local/share/kwin/scripts/letterbox-hotkeys
gdbus call --session --dest org.kde.KWin --object-path /KWin --method org.kde.KWin.reconfigure >/dev/null 2>&1
# the program itself; your data in the same folder stays
rm -rf "$L"/*.py "$L/letterbox" "$L/placement.js" "$L/data" "$L/__pycache__"
if [ -e /etc/systemd/system/letterbox-gw2log.service ]; then
    sudo systemctl disable --now letterbox-gw2log.service
    sudo rm -rf /etc/systemd/system/letterbox-gw2log.service /usr/local/lib/letterbox /var/lib/letterbox
    sudo systemctl daemon-reload
    echo "GW2 logs kept in /var/log/letterbox (delete with: sudo rm -r /var/log/letterbox)"
fi
if [ "${1:-}" = --purge ]; then
    rm -rf ~/.config/letterbox "$L" ~/.cache/letterbox
    echo "settings and data removed too"
else
    echo "kept: settings in ~/.config/letterbox, data in $L (markers, MangoHud logs, Firefox tile profile)"
fi
echo "letterbox removed. A MangoHud config it made or extended stays in ~/.config/MangoHud (the original, if"
echo "there was one, is next to it with .before-letterbox at the end)."
