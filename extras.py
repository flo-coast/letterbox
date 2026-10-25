# extras.py: the extra tiles. Each feature further down registers itself with tile(): its title, colour
# group, a draw function and optionally a click action, config defaults and fields for the "Extras" tab of
# the settings. bar.py and settings.py only know this registry.
# Network data comes from Poller threads that only run while their tile is drawn. GW2 items, currencies and
# coins are shown with their official icons from the GW2 API (render.guildwars2.com), cached locally.
import hashlib, json, os, re, subprocess, threading, time, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

import cairo
from gi.repository import Gio, GLib

API = "https://api.guildwars2.com/v2"
CACHE = os.path.expanduser("~/.cache/letterbox")
os.makedirs(CACHE, exist_ok=True)

TILES = {}        # key -> dict(title, group, draw(d), click(ex, cfg) or None)
DEFAULTS = {}     # config defaults of the features
SETTINGS = []     # fields for the settings' Extras tab: dict(key, label, kind, hint, ...)
ACTIONS = {}      # D-Bus Action(name) of the bar, e.g. hotkeys: name -> fn(extras)
AFTER_DRAW = []   # fn(bar) after every frame of the real bar (not the settings preview)


def tile(key, title, group="game", click=None):
    def reg(fn):
        TILES[key] = dict(title=title, group=group, draw=fn, click=click)
        return fn
    return reg


def http_json(url, key=None, timeout=10):
    if key:
        url += ("&" if "?" in url else "?") + "access_token=" + key
    req = urllib.request.Request(url, headers={"User-Agent": "letterbox"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def api(path, key=None):
    return http_json(f"{API}/{path}", key)


def load_json(name, default):
    try:
        return json.load(open(os.path.join(CACHE, name)))
    except (OSError, ValueError):
        return default


def save_json(name, data):
    p = os.path.join(CACHE, name)
    with open(p + ".tmp", "w") as fh:
        json.dump(data, fh)
    os.replace(p + ".tmp", p)


class Poller:
    """fetch() every `every` seconds in a thread, while touch() was called in the last two minutes."""

    def __init__(self, fetch, every):
        self.fetch, self.every = fetch, every
        self.value, self.err, self.updated = None, "loading…", 0
        self.last_touch, self.thread = 0, None

    def touch(self):
        self.last_touch = time.monotonic()
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()
        return self

    def loop(self):
        while time.monotonic() - self.last_touch < 120:
            try:
                self.value, self.err = self.fetch(), ""
            except Exception as ex:              # network down, API busy, bad key: show it, try again later
                self.err = str(ex)[:90]
            self.updated = time.time()
            time.sleep(self.every)


class Extras:
    """Per-bar state of the features: pollers and whatever a feature keeps (as attributes)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.started = time.time()
        self.pollers = {}

    def poll(self, name, fetch, every):
        if name not in self.pollers:
            self.pollers[name] = Poller(lambda: fetch(self), every)
        return self.pollers[name].touch()

    @property
    def key(self):
        return self.cfg.get("api_key", "")


# ---------- official icons ----------
class Icons:
    """cairo surfaces of GW2 icons (and other pictures), downloaded once into ~/.cache/letterbox/icons."""

    def __init__(self):
        self.surf, self.pending, self.lock = {}, set(), threading.Lock()
        self.dir = os.path.join(CACHE, "icons")
        os.makedirs(self.dir, exist_ok=True)
        self.files = load_json("gw2-files.json", {})

    def get(self, url):
        if not url:
            return None
        if url in self.surf:
            return self.surf[url]
        path = (urllib.parse.unquote(url[7:]) if url.startswith("file://") else
                os.path.join(self.dir, hashlib.sha1(url.encode()).hexdigest()[:16]))
        if os.path.exists(path):
            self.surf[url] = self._load(path)
            return self.surf[url]
        with self.lock:
            if url not in self.pending and not url.startswith("file://"):
                self.pending.add(url)
                threading.Thread(target=self._fetch, args=(url, path), daemon=True).start()
        return None

    def _load(self, path):
        try:
            with open(path, "rb") as fh:
                png = fh.read(4) == b"\x89PNG"
            if png:
                return cairo.ImageSurface.create_from_png(path)
            from gi.repository import Gdk, GdkPixbuf     # JPEG and others
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(path, 256, 256)
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pb.get_width(), pb.get_height())
            cr = cairo.Context(surf)
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.paint()
            return surf
        except Exception:
            return None

    def _fetch(self, url, path):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "letterbox"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            with open(path + ".part", "wb") as fh:
                fh.write(data)
            os.replace(path + ".part", path)
            self.surf[url] = self._load(path)
        except Exception:
            self.surf[url] = None
        finally:
            self.pending.discard(url)

    def file(self, name):
        """Icons of the API's /v2/files, e.g. ui_coin_gold, ui_coin_silver, ui_coin_copper, ui_gem."""
        if name not in self.files:
            try:
                self.files.update({f["id"]: f["icon"] for f in api("files?ids=ui_coin_gold,ui_coin_silver,"
                                                                    "ui_coin_copper,ui_gem")})
                save_json("gw2-files.json", self.files)
            except Exception:
                return None
        return self.get(self.files.get(name))


ICONS = Icons()


def draw_icon(cr, surf, x, y, size):
    if surf is None:                                      # still loading
        cr.set_source_rgba(1, 1, 1, 0.12)
        cr.rectangle(x, y, size, size)
        cr.fill()
        return
    cr.save()
    cr.translate(x, y)
    s = size / max(surf.get_width(), surf.get_height())
    cr.scale(s, s)
    cr.set_source_surface(surf, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_GOOD)
    cr.paint()
    cr.restore()


def rich(r, cr, x, base, width, size, parts, color):
    """One line of text pieces and icons, scaled down together until it fits width.
    parts: ("t", text[, colour[, font]]) or ("i", surface). -> x where it ends."""
    def measure(sz):
        total = 0
        for p in parts:
            if p[0] == "t":
                cr.select_font_face(p[3] if len(p) > 3 and p[3] else r.f_text)
                cr.set_font_size(sz)
                total += cr.text_extents(p[1]).x_advance
            else:
                total += sz * 1.15
        return total
    w = measure(size)
    if w > width > 0:
        size = max(size * 0.45, size * width / w)
    cx = x
    for p in parts:
        if p[0] == "t":
            cr.select_font_face(p[3] if len(p) > 3 and p[3] else r.f_text)
            cr.set_font_size(size)
            cr.set_source_rgb(*(p[2] if len(p) > 2 and p[2] else color))
            cr.move_to(cx, base)
            cr.show_text(p[1])
            cx += cr.text_extents(p[1]).x_advance
        else:
            draw_icon(cr, p[1], cx + size * 0.05, base - size * 0.88, size * 1.05)
            cx += size * 1.15
    return cx


def coin_parts(copper, font=None, sign=False):
    """12 [gold] 34 [silver] 56 [copper] with the official coin icons."""
    neg, copper = copper < 0, abs(int(copper))
    g, s, c = copper // 10000, copper // 100 % 100, copper % 100
    out = [("t", "-" if neg else "+", None, font)] if (neg or sign) else []
    for val, name in ((g, "gold"), (s, "silver"), (c, "copper")):
        if val or (name == "copper" and not g and not s):
            if out and out[-1][0] == "i":
                out.append(("t", " "))
            out += [("t", str(val), None, font), ("i", ICONS.file(f"ui_coin_{name}"))]
    return out


def short(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


def hm(seconds):
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class Draw:
    """What a feature's draw function gets: the tile's area and helpers. r: the bar's Renderer,
    p: palette (dim, num, accent = the tile's colour), ex: the Extras state."""
    GREEN, RED, GOLD, ORANGE = (0.45, 0.82, 0.5), (0.95, 0.42, 0.38), (1.0, 0.8, 0.3), (0.98, 0.6, 0.3)

    def __init__(self, r, cr, x, y, w, h, fh, ssz, cfg, data, p):
        self.r, self.cr, self.x, self.y, self.w, self.h = r, cr, x, y, w, h
        self.fh, self.ssz, self.cfg, self.data, self.p = fh, ssz, cfg, data, p
        self.ex = data.extras

    def msg(self, text):
        self.r.text(self.cr, text, self.x, self.y + self.h * 0.4, self.w, self.ssz, self.p["dim"])

    def lines(self, rows, top=0, size=None):
        """Rows of rich() parts from the top down, as many as fit."""
        size = size or max(12, self.ssz * 1.15)
        lh = size * 1.55
        ty = self.y + top
        for parts in rows:
            if ty + lh > self.y + self.h + 1:
                break
            rich(self.r, self.cr, self.x, ty + size * 1.05, self.w, size, parts, self.p["num"])
            ty += lh
        return ty

    def big(self, parts, size=None):
        """A large first line. -> its height, for lines(top=...)."""
        size = size or min(self.fh * 0.22, self.h * 0.3)
        rich(self.r, self.cr, self.x, self.y + size, self.w, size, parts, self.p["num"])
        return size * 1.35

    def need_key(self):
        if not self.ex.key:
            self.msg("needs a GW2 API key (settings → System)")
            return True
        return False

    def waiting(self, poller):
        """True (and a message) while the poller has nothing yet."""
        if poller.value is None:
            self.msg(poller.err or "loading…")
            return True
        return False


# ---------- world bosses done today (marks in the event timer) ----------
DEFAULTS["bosses_done"] = True
SETTINGS += [dict(kind="heading", label="Event timer"),
             dict(key="bosses_done", kind="check", label="Grey out bosses done today",
                  hint="World bosses you've already looted today, from the GW2 API (needs the API key).")]
BOSS_ALIASES = {"golem mark ii": "inquest_golem_mark_ii", "evolved jungle wurm": "triple_trouble_wurm",
                "drakkar and spirits of the wild": "drakkar", "a titanic voyage": "mists_and_monsters_titans"}


def boss_id(name):
    """Event timer name -> the API's world boss id ("Fire Elemental" -> "fire_elemental")."""
    n = name.lower().strip()
    return BOSS_ALIASES.get(n, re.sub(r"[^a-z0-9]+", "_", n).strip("_"))


def bosses_done(ex):
    """Ids of the world bosses done today (empty without the API key or with the option off)."""
    if not ex.cfg.get("bosses_done", True) or not ex.key:
        return set()
    return ex.poll("bosses", lambda e: set(api("account/worldbosses", e.key)), 300).value or set()
