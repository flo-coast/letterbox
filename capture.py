# capture.py: grab a screen area through KWin's ScreenShot2 D-Bus API (used by gw2-bar.py for the
# "ambient" background). Returns (width, height, stride, bytes in QImage ARGB32/RGBA layout) or raises.
import os, threading

from gi.repository import Gio, GLib


def capture_area(conn, x, y, w, h):
    r, wfd = os.pipe()
    fdl = Gio.UnixFDList.new()
    idx = fdl.append(wfd)
    os.close(wfd)
    chunks = []

    def reader():
        with os.fdopen(r, "rb") as f:
            while True:
                b = f.read(1 << 16)
                if not b:
                    break
                chunks.append(b)
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        res, _ = conn.call_with_unix_fd_list_sync(
            "org.kde.KWin", "/org/kde/KWin/ScreenShot2", "org.kde.KWin.ScreenShot2", "CaptureArea",
            GLib.Variant("(iiuua{sv}h)", (x, y, w, h, {"native-resolution": GLib.Variant("b", False)}, idx)),
            GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NONE, 2000, fdl, None)
    finally:
        fdl = None
    t.join(2)
    info = res.unpack()[0]
    return info["width"], info["height"], info["stride"], b"".join(chunks), info.get("format")
