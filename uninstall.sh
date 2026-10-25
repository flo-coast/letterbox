#!/bin/bash
# Removes letterbox. Your settings (~/.config/letterbox) stay unless you pass --purge.
set -u
[ "$(id -u)" != 0 ] || { echo "run as your normal user"; exit 1; }
[ -f ~/.config/letterbox/enabled ] && ~/.local/share/letterbox/letterbox off
~/.local/share/letterbox/letterbox stop >/dev/null 2>&1
rm -f ~/.local/bin/letterbox ~/.local/bin/letterbox-settings ~/.local/share/applications/letterbox.settings.desktop
kwriteconfig6 --file kwinrc --group Plugins --key letterbox-hotkeysEnabled false
rm -rf ~/.local/share/kwin/scripts/letterbox-hotkeys ~/.local/share/letterbox
qdbus6 org.kde.KWin /KWin reconfigure >/dev/null 2>&1
if [ -e /etc/systemd/system/letterbox-gw2log.service ]; then
    sudo systemctl disable --now letterbox-gw2log.service
    sudo rm -rf /etc/systemd/system/letterbox-gw2log.service /usr/local/lib/letterbox /var/lib/letterbox
    sudo systemctl daemon-reload
    echo "GW2 logs kept in /var/log/letterbox (delete with: sudo rm -r /var/log/letterbox)"
fi
[ "${1:-}" = --purge ] && rm -rf ~/.config/letterbox
echo "letterbox removed. Remove 'letterbox run' from GW2's Steam launch options."
