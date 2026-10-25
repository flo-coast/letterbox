#!/bin/bash
# Removes letterbox. Your settings (~/.config/letterbox) stay unless you pass --purge.
set -u
[ "$(id -u)" != 0 ] || { echo "run as your normal user"; exit 1; }
L=~/.local/share/letterbox
[ -f ~/.config/letterbox/enabled ] && "$L/letterbox" off
"$L/letterbox" stop >/dev/null 2>&1
if [ -f "$L/setup.py" ] && ! python3 "$L/setup.py" launch remove; then
    echo "Remove 'letterbox run' from GW2's Steam launch options by hand (or close Steam and run this again)."
fi
rm -f ~/.local/bin/letterbox ~/.local/bin/letterbox-settings ~/.local/share/applications/letterbox.settings.desktop
kwriteconfig6 --file kwinrc --group Plugins --key letterbox-hotkeysEnabled false
rm -rf ~/.local/share/kwin/scripts/letterbox-hotkeys "$L"
gdbus call --session --dest org.kde.KWin --object-path /KWin --method org.kde.KWin.reconfigure >/dev/null 2>&1
if [ -e /etc/systemd/system/letterbox-gw2log.service ]; then
    sudo systemctl disable --now letterbox-gw2log.service
    sudo rm -rf /etc/systemd/system/letterbox-gw2log.service /usr/local/lib/letterbox /var/lib/letterbox
    sudo systemctl daemon-reload
    echo "GW2 logs kept in /var/log/letterbox (delete with: sudo rm -r /var/log/letterbox)"
fi
[ "${1:-}" = --purge ] && rm -rf ~/.config/letterbox
echo "letterbox removed."
