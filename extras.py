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
ON_EXIT = []      # fn(bar) when the real bar ends (also on SIGTERM from "letterbox stop")


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
        self.key, self.wake = None, threading.Event()

    def setting(self, key):
        """The setting(s) the data depends on: when they change, old data goes and it fetches right away."""
        if key != self.key:
            self.key = key
            self.value, self.err = None, "loading…"
            self.wake.set()

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
            self.wake.wait(self.every)
            self.wake.clear()


class Extras:
    """Per-bar state of the features: pollers and whatever a feature keeps (as attributes)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.started = time.time()
        self.pollers = {}

    def poll(self, name, fetch, every, key=None):
        """The named poller, running. key: the settings its data depends on (default: the API key)."""
        if name not in self.pollers:
            self.pollers[name] = Poller(lambda: fetch(self), every)
        pl = self.pollers[name]
        pl.setting(self.key if key is None else key)
        return pl.touch()

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


# ---------- Trading Post watchlist ----------
DEFAULTS["tp_watch"] = []                 # [{"id": item id, "target": copper (0 = none)}]


def chat_link_item(text):
    """Item id from an in-game chat link ([&AgH0TQAA]) or a plain number."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    m = re.match(r"^\[&([A-Za-z0-9+/=]+)\]$", text)
    if not m:
        return None
    import base64
    raw = base64.b64decode(m.group(1) + "==")
    return raw[2] | raw[3] << 8 | raw[4] << 16 if len(raw) >= 5 and raw[0] == 2 else None   # 2 = item


def parse_coins(text):
    """'1g 20s 5c', '12s', '1.5g' or plain copper -> copper (None if unreadable)."""
    text = text.strip().lower().replace(",", ".")
    if not text:
        return 0
    if re.fullmatch(r"\d+", text):
        return int(text)
    total, found = 0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([gsc])", text):
        total += float(num) * {"g": 10000, "s": 100, "c": 1}[unit]
        found = True
    return int(round(total)) if found else None


def coins_text(copper):
    g, s, c = copper // 10000, copper // 100 % 100, copper % 100
    return " ".join(f"{v}{u}" for v, u in ((g, "g"), (s, "s"), (c, "c")) if v) or "0c"


def fetch_tp(ex):
    watch = [w for w in ex.cfg.get("tp_watch", []) if w.get("id")]
    out = {"items": [], "delivery": api("commerce/delivery", ex.key) if ex.key else None}
    if not watch:
        return out
    ids = ",".join(str(w["id"]) for w in watch)
    items = {i["id"]: i for i in api(f"items?ids={ids}")}
    prices = {p["id"]: p for p in api(f"commerce/prices?ids={ids}")}
    hist = load_json("tp-history.json", {})              # first price seen each day, for the trend
    today, yday = str(date.today()), str(date.today() - timedelta(days=1))
    for w in watch:
        p = prices.get(w["id"], {})
        buy, sell = p.get("buys", {}).get("unit_price", 0), p.get("sells", {}).get("unit_price", 0)
        hist.setdefault(today, {}).setdefault(str(w["id"]), [buy, sell])
        prev = hist.get(yday, {}).get(str(w["id"]))
        it = items.get(w["id"], {})
        out["items"].append(dict(id=w["id"], name=it.get("name", f"item {w['id']}"), icon=it.get("icon"),
                                 buy=buy, sell=sell, prev=prev[1] if prev else None, target=w.get("target", 0)))
    for d in sorted(hist)[:-8]:
        del hist[d]
    save_json("tp-history.json", hist)
    return out


@tile("tp", "Trading Post")
def draw_tp(d):
    if not d.cfg.get("tp_watch") and not d.ex.key:
        d.msg("add items in the settings (Extras)")
        return
    pl = d.ex.poll("tp", fetch_tp, 120, key=(d.ex.key, json.dumps(d.cfg.get("tp_watch", []))))
    if d.waiting(pl):
        return
    nf, rows = d.r.f_num, []
    for it in pl.value["items"]:
        hit = it["target"] and it["sell"] and it["sell"] <= it["target"]
        trend = []
        if it["prev"]:
            ch = (it["sell"] - it["prev"]) / it["prev"] * 100
            trend = [("t", f"  {'+' if ch >= 0 else ''}{ch:.0f} %", d.GREEN if ch >= 0 else d.RED)]
        rows.append([("i", ICONS.get(it["icon"])), ("t", " " + short(it["name"], 28) + "  ", d.GOLD if hit else None)]
                    + coin_parts(it["sell"], nf) + trend + ([("t", "  below target!", d.GOLD)] if hit else []))
    dl = pl.value.get("delivery")
    if dl and (dl.get("coins") or dl.get("items")):
        rows.append([("t", "to pick up  ", d.p["accent"])] + (coin_parts(dl["coins"], nf) if dl.get("coins") else [])
                    + ([("t", f"  {sum(i.get('count', 1) for i in dl['items'])} items", d.p["dim"])]
                       if dl.get("items") else []))
    if not rows:
        d.msg("add items in the settings (Extras)")
        return
    d.lines(rows)


def tp_settings(win, f):
    """Watchlist editor for the settings: add by id or chat link, target price, remove."""
    from gi.repository import Gtk
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
    names = load_json("tp-names.json", {})

    def name_of(i):
        if str(i) not in names:
            try:
                names[str(i)] = api(f"items/{i}")["name"]
                save_json("tp-names.json", names)
            except Exception:
                return f"item {i}"
        return names[str(i)]

    def save(lst):
        win.c["tp_watch"] = lst
        win.changed()
        render()

    def render():
        while (ch := box.get_first_child()) is not None:
            box.remove(ch)
        add = Gtk.Box(spacing=6)
        entry = Gtk.Entry(placeholder_text="item id or chat link, e.g. [&AgH0TQAA]", hexpand=True)
        btn = Gtk.Button(label="Add")
        note = Gtk.Label(css_classes=["dim-label"])

        def do_add(*_):
            i = chat_link_item(entry.get_text())
            if not i:
                note.set_text("not an item id or item chat link")
                return
            if any(w["id"] == i for w in win.c.get("tp_watch", [])):
                note.set_text("already on the list")
                return
            save(win.c.get("tp_watch", []) + [{"id": i, "target": 0}])
        entry.connect("activate", do_add)
        btn.connect("clicked", do_add)
        add.append(entry)
        add.append(btn)
        add.append(note)
        box.append(add)
        for n, w in enumerate(win.c.get("tp_watch", [])):
            row = Gtk.Box(spacing=6)
            row.append(Gtk.Label(label=name_of(w["id"]), xalign=0, hexpand=True))
            row.append(Gtk.Label(label="alert below", css_classes=["dim-label"]))
            tgt = Gtk.Entry(text=coins_text(w["target"]) if w.get("target") else "", placeholder_text="e.g. 1g 20s",
                            width_chars=10)

            def set_target(text, n=n):
                v = parse_coins(text)
                if v is not None and v != win.c["tp_watch"][n].get("target", 0):
                    win.c["tp_watch"][n]["target"] = v
                    win.changed()
            win.apply_on_enter(tgt, set_target)
            row.append(tgt)
            rm = Gtk.Button(icon_name="edit-delete-symbolic", css_classes=["flat"], tooltip_text="remove")
            rm.connect("clicked", lambda _b, n=n: save([w for k, w in enumerate(win.c["tp_watch"]) if k != n]))
            row.append(rm)
            box.append(row)
    render()
    return box


SETTINGS += [dict(kind="heading", label="Trading Post"),
             dict(key="tp_watch", kind="custom", label="Watchlist", build=tp_settings,
                  hint="Shift+click an item in game, copy the link from the chat and paste it here. The tile shows "
                       "the sell price, the change since yesterday and turns gold below your alert price. Your "
                       "delivery box shows up there too (with the API key).")]


# ---------- session earnings ----------
def fetch_wallet(ex):
    names = load_json("gw2-currencies.json", {})
    if not names:
        names = {str(c["id"]): {"name": c["name"], "icon": c["icon"]} for c in api("currencies?ids=all")}
        save_json("gw2-currencies.json", names)
    now = {str(x["id"]): x["value"] for x in api("account/wallet", ex.key)}
    if not hasattr(ex, "wallet_start"):                    # the first look is the session's start
        ex.wallet_start = (time.time(), now)
    return {"now": now, "names": names}


@tile("earnings", "Session earnings")
def draw_earnings(d):
    if d.need_key():
        return
    pl = d.ex.poll("wallet", fetch_wallet, 60)
    if d.waiting(pl):
        return
    t0, start = d.ex.wallet_start
    now, names = pl.value["now"], pl.value["names"]
    diff = {k: now.get(k, 0) - start.get(k, 0) for k in set(now) | set(start)}
    hours = max(1 / 60, (time.time() - t0) / 3600)
    nf = d.r.f_num
    gold = diff.get("1", 0)
    top = d.big(coin_parts(gold, nf, sign=True))
    rows = [[("t", "per hour  ", d.p["dim"])] + coin_parts(gold / hours, nf) +
            [("t", f"   ·   for {hm(time.time() - t0)} h", d.p["dim"])]]
    for k, val in sorted(diff.items(), key=lambda z: -abs(z[1])):
        if k != "1" and val:
            c = names.get(k, {})
            rows.append([("i", ICONS.get(c.get("icon"))), ("t", f" {val:+,}  ".replace(",", " "), None, nf),
                         ("t", c.get("name", k), d.p["dim"])])
    d.lines(rows, top=top)


# ---------- Wizard's Vault ----------
@tile("vault", "Wizard's Vault")
def draw_vault(d):
    if d.need_key():
        return
    pl = d.ex.poll("vault", lambda e: {p: api(f"account/wizardsvault/{p}", e.key) for p in ("daily", "weekly")}, 300)
    if d.waiting(pl):
        return
    nf, rows = d.r.f_num, []
    for per in ("daily", "weekly"):
        v = pl.value.get(per) or {}
        objs = v.get("objectives", [])
        done = sum(1 for o in objs if o.get("progress_current", 0) >= o.get("progress_complete", 1))
        head = [("t", f"{per.title()}  ", d.p["accent"]), ("t", f"{done}/{len(objs)}", None, nf)]
        if v.get("meta_reward_claimed"):
            head.append(("t", "   reward claimed", d.GREEN))
        elif done == len(objs) and objs:
            head.append(("t", "   reward ready to claim", d.GOLD))
        rows.append(head)
        for o in objs:
            cur, need = o.get("progress_current", 0), o.get("progress_complete", 1)
            if cur < need:
                rows.append([("t", f"   {short(o.get('title', ''), 50)}  ", d.p["dim"]), ("t", f"{cur}/{need}", None, nf)])
    d.lines(rows)


# ---------- character birthdays ----------
def fetch_chars(ex):
    icons = load_json("gw2-professions.json", {})
    if not icons:
        icons = {p["id"]: p["icon"] for p in api("professions?ids=all")}
        save_json("gw2-professions.json", icons)
    return {"chars": api("characters?ids=all", ex.key), "icons": icons}


@tile("birthdays", "Birthdays")
def draw_birthdays(d):
    if d.need_key():
        return
    pl = d.ex.poll("chars", fetch_chars, 3600)
    if d.waiting(pl):
        return
    today, nxt = date.today(), []
    for c in pl.value["chars"]:
        made = datetime.fromisoformat(c["created"].replace("Z", "+00:00")).astimezone().date()
        try:
            bday = made.replace(year=today.year)
        except ValueError:                                # born on 29 February
            bday = date(today.year, 3, 1)
        if bday < today:
            bday = bday.replace(year=today.year + 1)
        if bday.year - made.year > 0:
            nxt.append(((bday - today).days, c["name"], bday.year - made.year, c.get("profession")))
    rows = []
    for days, name, years, prof in sorted(nxt):
        when = "today!" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        col = d.GOLD if days <= 1 else None
        rows.append([("i", ICONS.get(pl.value["icons"].get(prof))), ("t", f" {name}  ", col),
                     ("t", f"turns {years} {when}", col or d.p["dim"])])
    d.lines(rows) if rows else d.msg("no birthdays coming up")


# ---------- gem exchange ----------
def fetch_gems(ex):
    rate = api("commerce/exchange/coins?quantity=1000000")            # what 100 gold buy
    hist = load_json("gem-history.json", {})
    hist.setdefault(str(date.today()), rate["coins_per_gem"])
    for d in sorted(hist)[:-8]:
        del hist[d]
    save_json("gem-history.json", hist)
    return dict(per_gem=rate["coins_per_gem"], for_100g=rate["quantity"],
                yesterday=hist.get(str(date.today() - timedelta(days=1))))


@tile("gems", "Gem exchange")
def draw_gems(d):
    pl = d.ex.poll("gems", fetch_gems, 300, key="")
    if d.waiting(pl):
        return
    v, nf, gem = pl.value, d.r.f_num, ICONS.file("ui_gem")
    top = d.big([("t", "1 ", None, nf), ("i", gem), ("t", "  =  ", d.p["dim"])] + coin_parts(v["per_gem"], nf))
    rows = [[("t", "100 ", None, nf), ("i", ICONS.file("ui_coin_gold")), ("t", "  buy  ", d.p["dim"]), ("t", f"{v['for_100g']} ", None, nf),
             ("i", gem)]]
    if v["yesterday"]:
        ch = (v["per_gem"] - v["yesterday"]) / v["yesterday"] * 100
        rows.append([("t", f"gems {'+' if ch >= 0 else ''}{ch:.1f} % since yesterday", d.RED if ch > 0 else d.GREEN)])
    d.lines(rows, top=top)


# ---------- run timer (click or Meta+Shift+T) ----------
def timer_toggle(ex, _cfg=None):
    runs = getattr(ex, "timer_runs", None)
    if runs is None:
        runs = ex.timer_runs = load_json("timer-runs.json", [])
    if getattr(ex, "timer_start", None) is None:
        ex.timer_start = time.time()
    else:
        ex.timer_runs = ([round(time.time() - ex.timer_start, 1)] + runs)[:10]
        ex.timer_start = None
        save_json("timer-runs.json", ex.timer_runs)


ACTIONS["timer"] = timer_toggle


def clock(s):
    return f"{int(s // 3600)}:{int(s // 60 % 60):02d}:{int(s % 60):02d}" if s >= 3600 else f"{int(s // 60)}:{s % 60:04.1f}"


@tile("timer", "Run timer", click=timer_toggle)
def draw_timer(d):
    ex = d.ex
    runs = getattr(ex, "timer_runs", None)
    if runs is None:
        runs = ex.timer_runs = load_json("timer-runs.json", [])
    start = getattr(ex, "timer_start", None)
    t = time.time() - start if start else (runs[0] if runs else 0)
    size = min(d.fh * 0.3, d.h * 0.42)
    d.r.text(d.cr, clock(t), d.x, d.y + size, d.w, size, d.GREEN if start else d.p["num"], face=d.r.f_num)
    rows = [[("t", ("running, click or Meta+Shift+T to stop" if start else "click or Meta+Shift+T to start"),
              d.p["dim"])]]
    if runs:
        rows.append([("t", "last runs  " + "   ".join(clock(s).split(".")[0] for s in runs[:6]), d.p["dim"])])
    d.lines(rows, top=size * 1.3)


# ---------- now playing (MPRIS: Spotify, Deezer or YouTube in the browser, any player) ----------
def now_playing(ex):
    """The playing (else the paused) player's title, artist, cover, position; looked up at most once a second."""
    if time.monotonic() - getattr(ex, "music_at", 0) < 1:
        return ex.music
    ex.music_at, best = time.monotonic(), None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames",
                              None, None, Gio.DBusCallFlags.NONE, 500, None).unpack()[0]
        for n in (n for n in names if n.startswith("org.mpris.MediaPlayer2.")):
            try:
                pr = bus.call_sync(n, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "GetAll",
                                   GLib.Variant("(s)", ("org.mpris.MediaPlayer2.Player",)), None,
                                   Gio.DBusCallFlags.NONE, 500, None).unpack()[0]
            except GLib.Error:
                continue
            md = pr.get("Metadata", {})
            if not md.get("xesam:title"):
                continue
            cur = dict(player=n, status=pr.get("PlaybackStatus", ""), title=md["xesam:title"],
                       artist=", ".join(md.get("xesam:artist") or []), art=md.get("mpris:artUrl", ""),
                       length=(md.get("mpris:length") or 0) / 1e6, pos=(pr.get("Position") or 0) / 1e6,
                       app=n.split(".")[3])
            if best is None or (cur["status"] == "Playing" and best["status"] != "Playing"):
                best = cur
    except GLib.Error:
        pass
    ex.music = best
    return best


def music_toggle(ex, _cfg=None):
    m = getattr(ex, "music", None)
    if m:
        Gio.bus_get_sync(Gio.BusType.SESSION).call(m["player"], "/org/mpris/MediaPlayer2",
                                                   "org.mpris.MediaPlayer2.Player", "PlayPause", None, None,
                                                   Gio.DBusCallFlags.NONE, 500, None, None)
        ex.music_at = 0


@tile("music", "Now playing", group="mem", click=music_toggle)
def draw_music(d):
    m = now_playing(d.ex)
    if not m:
        d.msg("nothing playing")
        return
    tx = d.x
    if m["art"]:
        a = min(d.h, d.w * 0.3)
        draw_icon(d.cr, ICONS.get(m["art"]), d.x, d.y + (d.h - a) / 2, a)
        tx = d.x + a + d.ssz
    tw = d.x + d.w - tx
    size = min(d.fh * 0.16, d.h * 0.22)
    playing = m["status"] == "Playing"
    d.r.text(d.cr, m["title"], tx, d.y + d.h * 0.28, tw, size, d.p["num"] if playing else d.p["dim"])
    d.r.text(d.cr, m["artist"] or m["app"], tx, d.y + d.h * 0.28 + size * 1.35, tw, size * 0.8, d.p["dim"])
    if m["length"] > 0:
        by, bh = d.y + d.h * 0.7, max(3, d.h * 0.03)
        d.cr.set_source_rgba(1, 1, 1, 0.12)
        d.cr.rectangle(tx, by, tw, bh)
        d.cr.fill()
        d.cr.set_source_rgb(*d.p["accent"])
        d.cr.rectangle(tx, by, tw * min(1, m["pos"] / m["length"]), bh)
        d.cr.fill()
    d.r.text(d.cr, f"{m['app']} · {'playing' if playing else 'paused'} · click to {'pause' if playing else 'play'}",
             tx, d.y + d.h * 0.92, tw, d.ssz, d.p["dim"])


# ---------- device batteries (UPower: mouse, keyboard, headset, controller, laptop) ----------
UPOWER_KINDS = {2: "laptop", 5: "mouse", 6: "keyboard", 12: "controller", 17: "headset", 18: "speakers",
                19: "headphones", 21: "pen", 24: "touchpad"}


def batteries(ex):
    if time.monotonic() - getattr(ex, "batt_at", -99) < 30:
        return ex.batt
    ex.batt_at, out = time.monotonic(), []
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        devs = bus.call_sync("org.freedesktop.UPower", "/org/freedesktop/UPower", "org.freedesktop.UPower",
                             "EnumerateDevices", None, None, Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
        for dev in devs:
            p = bus.call_sync("org.freedesktop.UPower", dev, "org.freedesktop.DBus.Properties", "GetAll",
                              GLib.Variant("(s)", ("org.freedesktop.UPower.Device",)), None,
                              Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
            if p.get("Type") in (0, 1) or not p.get("IsPresent", True):          # unknown, power supply
                continue
            kind = UPOWER_KINDS.get(p.get("Type"), "device")
            charging, full = p.get("State") == 1, p.get("State") == 4
            if kind == "laptop" and (charging or full):
                continue                                                         # on the charger: no news
            out.append(dict(kind=kind, name=p.get("Model") or kind, pct=p.get("Percentage", 0), charging=charging,
                            full=full))
    except GLib.Error:
        pass
    ex.batt = out
    return out


@tile("battery", "Batteries", group="mem")
def draw_battery(d):
    bats = batteries(d.ex)
    if not bats:
        d.msg("no devices with a battery")
        return
    rows = []
    for b in sorted(bats, key=lambda b: b["pct"]):
        low = b["pct"] < 15 and not b["charging"]
        col = d.RED if low else d.ORANGE if b["pct"] < 30 else d.GREEN
        rows.append([("t", f"{b['pct']:.0f} %", col, d.r.f_num),
                     ("t", f"  {short(b['name'], 32)}" + ("  charging" if b["charging"] else "  full" if b["full"]
                                                            else "") +
                      ("  charge me!" if low else ""), col if low else d.p["dim"])])
    d.lines(rows, size=max(12, d.ssz * 1.3))


# ---------- desktop notifications, read along on the session bus ----------
def notifications(ex):
    """The latest desktop notifications (Discord, mail, ...). A second connection to the session bus becomes
    a monitor for Notify calls, like dbus-monitor; nothing is changed or answered."""
    if not hasattr(ex, "notes"):
        ex.notes, ex.notify_conn = [], None
        try:
            addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            c = Gio.DBusConnection.new_for_address_sync(
                addr, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
            c.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.Monitoring",
                        "BecomeMonitor", GLib.Variant("(asu)", (["type='method_call',interface="
                                                                   "'org.freedesktop.Notifications',member='Notify'"], 0)),
                        None, Gio.DBusCallFlags.NONE, 1000, None)
            # only now: the filter keeps every monitored message for itself (a monitor must never answer)
            c.add_filter(lambda _c, msg, _in: _on_notify(ex, msg))
            ex.notify_conn = c
        except GLib.Error as err:
            ex.notify_conn = False
            print("letterbox: can't read notifications:", err.message, flush=True)
    return ex.notes


def _on_notify(ex, msg):
    try:
        if msg.get_member() == "Notify" and msg.get_body() is not None:
            app, _id, _icon, summary, body = msg.get_body().unpack()[:5]
            body = re.sub(r"<[^>]+>", "", body or "").replace("\n", " ").strip()
            ex.notes = ([dict(app=app or "?", summary=summary, body=body, t=time.time())] + ex.notes)[:20]
    except Exception:
        pass
    return None


@tile("notify", "Notifications", group="mem")
def draw_notify(d):
    notes = notifications(d.ex)
    if d.ex.notify_conn is False:
        d.msg("can't read notifications here")
        return
    if not notes:
        d.msg("no notifications since the game started")
        return
    rows = []
    for n in notes:
        age = time.time() - n["t"]
        fresh = age < 20
        when = "now" if age < 60 else f"{int(age // 60)} min" if age < 3600 else f"{int(age // 3600)} h"
        text = f"{n['summary']}: {n['body']}" if n["body"] else n["summary"]
        rows.append([("t", f"{n['app']} · {when}  ", d.p["accent"] if fresh else d.p["dim"]),
                     ("t", short(text, 100), None if fresh else d.p["dim"])])
    d.lines(rows)


# ---------- playtime and break reminder ----------
DEFAULTS["break_minutes"] = 0
SETTINGS += [dict(kind="heading", label="Playtime"),
             dict(key="break_minutes", kind="spin", label="Break reminder after", min=0, max=600, step=15,
                  hint="Minutes of play in one go, then the playtime tile says it's time for a break. 0 = off.")]


def played_today(ex):
    """Seconds of active game today: the GW2 logger writes one row per second while the game runs."""
    if time.monotonic() - getattr(ex, "play_at", -99) < 30:
        return ex.play
    ex.play_at, n = time.monotonic(), 0
    import glob
    for p in glob.glob(f"/var/log/letterbox/gw2-{date.today():%Y%m%d}*.csv"):
        try:
            with open(p, "rb") as fh:
                n += max(0, sum(1 for _ in fh) - 1)
        except OSError:
            pass
    ex.play = n
    return n


@tile("playtime", "Playtime")
def draw_playtime(d):
    session = time.time() - d.ex.started
    today = max(played_today(d.ex), session)
    top = d.big([("t", hm(today), None, d.r.f_num), ("t", " h today", d.p["dim"])])
    rows = [[("t", f"this session {hm(session)} h", d.p["dim"])]]
    brk = d.cfg.get("break_minutes", 0)
    if brk and session > brk * 60:
        rows.append([("t", f"time for a break, you've been playing {hm(session)} h", d.ORANGE)])
    elif brk:
        rows.append([("t", f"break in {int((brk * 60 - session) // 60) + 1} min", d.p["dim"])])
    d.lines(rows, top=top)


# ---------- weather (Open-Meteo, no account needed) ----------
DEFAULTS["weather_city"] = ""
SETTINGS += [dict(kind="heading", label="Weather"),
             dict(key="weather_city", kind="entry", apply=True, label="Town", placeholder="e.g. Hamburg, then Enter",
                  hint="For the weather tile, from open-meteo.com.")]
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog", 51: "drizzle",
       53: "drizzle", 55: "drizzle", 56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
       65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
       77: "snow grains", 80: "showers", 81: "showers", 82: "heavy showers", 85: "snow showers",
       86: "snow showers", 95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail"}


def fetch_weather(ex):
    city = ex.cfg.get("weather_city", "").strip()
    geo = load_json("geo.json", {})
    if city not in geo:
        g = http_json("https://geocoding-api.open-meteo.com/v1/search?count=1&name=" + urllib.parse.quote(city))
        if not g.get("results"):
            raise ValueError(f"'{city}' not found")
        r = g["results"][0]
        geo[city] = [r["latitude"], r["longitude"], r["name"]]
        save_json("geo.json", geo)
    lat, lon, name = geo[city]
    w = http_json(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone=auto"
                  "&current=temperature_2m,weather_code,wind_speed_10m&hourly=precipitation_probability"
                  "&daily=temperature_2m_max,temperature_2m_min&forecast_days=1")
    return dict(w, place=name, city=city)


@tile("weather", "Weather", group="net")
def draw_weather(d):
    if not d.cfg.get("weather_city", "").strip():
        d.msg("set your town in the settings (Extras)")
        return
    pl = d.ex.poll("weather", fetch_weather, 900, key=d.cfg["weather_city"].strip())
    if d.waiting(pl):
        return
    v = pl.value
    cur, day = v["current"], v["daily"]
    top = d.big([("t", f"{cur['temperature_2m']:.0f}", None, d.r.f_num), ("t", " °C"),
                 ("t", f"   {WMO.get(cur['weather_code'], '')}", d.p["dim"])])
    rows = [[("t", f"{v['place']} · {day['temperature_2m_min'][0]:.0f} to {day['temperature_2m_max'][0]:.0f} °C · "
                   f"wind {cur['wind_speed_10m']:.0f} km/h", d.p["dim"])]]
    hrs, now_h = v.get("hourly", {}), datetime.now().hour
    rain = [(t[11:16], p) for t, p in zip(hrs.get("time", []), hrs.get("precipitation_probability", []))
            if int(t[11:13]) > now_h and p is not None and p >= 50]
    wet = cur["weather_code"] >= 51
    rows.append([("t", f"rain likely from {rain[0][0]} ({rain[0][1]} %)" if rain else
                  f"no {'more ' if wet else ''}rain expected today", d.p["accent"] if rain else d.p["dim"])])
    d.lines(rows, top=top)


# ---------- calendar (any iCal link: Google Calendar, Nextcloud, Outlook) ----------
DEFAULTS["calendar_ics"] = ""
SETTINGS += [dict(kind="heading", label="Calendar"),
             dict(key="calendar_ics", kind="secret", apply=True, label="iCal link",
                  hint="Google Calendar: settings of the calendar → 'Secret address in iCal format'. Nextcloud and "
                       "Outlook have such a link too. It's private, so it's hidden here like a password.")]


def _ics_time(val, params):
    val = val.strip()
    if re.fullmatch(r"\d{8}", val):                                     # all-day
        return datetime.strptime(val, "%Y%m%d").astimezone(), True
    t = datetime.strptime(val.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if val.endswith("Z"):
        return t.replace(tzinfo=timezone.utc).astimezone(), False
    tz = re.search(r"TZID=([^;:]+)", params)
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return t.replace(tzinfo=ZoneInfo(tz.group(1))).astimezone(), False
        except Exception:
            pass
    return t.astimezone(), False


def upcoming_events(text, now, n):
    """The next n events of an iCal file: [(start, all_day, summary)]. Repeats (RRULE) daily, weekly, monthly and
    yearly are followed (INTERVAL, COUNT, UNTIL); finer rules give the plain series."""
    text = re.sub(r"\r?\n[ \t]", "", text)                             # unfold continued lines
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        props = {}
        for line in block.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                name, _, params = k.partition(";")
                props.setdefault(name.upper(), (v, params))
        if "DTSTART" not in props or props.get("STATUS", ("",))[0].upper() == "CANCELLED":
            continue
        t, allday = _ics_time(*props["DTSTART"])
        summary = props.get("SUMMARY", ("(no title)", ""))[0].replace("\\,", ",").replace("\\n", " ").replace("\\;", ";")
        rule = dict(x.split("=", 1) for x in props.get("RRULE", ("", ""))[0].split(";") if "=" in x)
        freq, every = rule.get("FREQ"), int(rule.get("INTERVAL", "1") or 1)
        until = _ics_time(rule["UNTIL"], "")[0] if "UNTIL" in rule else None
        count, i = int(rule.get("COUNT", "0") or 0), 1
        while freq in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") and t < now - timedelta(hours=1) and i < 5000:
            if count and i >= count:
                break
            if freq in ("DAILY", "WEEKLY"):
                t += timedelta(days=every * (7 if freq == "WEEKLY" else 1))
            else:
                m = t.month - 1 + (every if freq == "MONTHLY" else 12 * every)
                try:
                    t = t.replace(year=t.year + m // 12, month=m % 12 + 1)
                except ValueError:                                     # 31st in a short month: skip it
                    pass
            i += 1
        if until and t > until:
            continue
        if t >= now - timedelta(hours=1) or (allday and t.date() == now.date()):
            out.append((t, allday, summary))
    return sorted(out)[:n]


def fetch_calendar(ex):
    url = ex.cfg.get("calendar_ics", "").strip().replace("webcal://", "https://")
    req = urllib.request.Request(url, headers={"User-Agent": "letterbox"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return upcoming_events(r.read().decode("utf-8", "replace"), datetime.now().astimezone(), 6)


@tile("calendar", "Calendar", group="net")
def draw_calendar(d):
    if not d.cfg.get("calendar_ics", "").strip():
        d.msg("add your calendar's iCal link in the settings (Extras)")
        return
    pl = d.ex.poll("calendar", fetch_calendar, 600, key=d.cfg.get("calendar_ics", "").strip())
    if d.waiting(pl):
        return
    if not pl.value:
        d.msg("nothing coming up")
        return
    now, rows = datetime.now().astimezone(), []
    for t, allday, summary in pl.value:
        if allday:
            when = "today" if t.date() == now.date() else "tomorrow" if t.date() == now.date() + timedelta(1) \
                else t.strftime("%a %d.%m.")
        elif t.date() == now.date():
            mins = (t - now).total_seconds() / 60
            when = f"{t:%H:%M}" + (f" (in {int(mins) + 1} min)" if 0 < mins < 120 else "")
        else:
            when = t.strftime("%a %H:%M")
        soon = not allday and 0 < (t - now).total_seconds() < 900
        rows.append([("t", f"{when}  ", d.ORANGE if soon else d.p["accent"]),
                     ("t", short(summary, 70), None if soon else d.p["num"])])
    d.lines(rows, size=max(12, d.ssz * 1.25))


# ---------- sports scores (ESPN's public scoreboard; unofficial, so it may change without notice) ----------
LEAGUES = [("basketball/nba", "NBA"), ("basketball/wnba", "WNBA"), ("football/nfl", "NFL"), ("hockey/nhl", "NHL"),
           ("baseball/mlb", "MLB"), ("soccer/eng.1", "Premier League"), ("soccer/ger.1", "Bundesliga"),
           ("soccer/esp.1", "LaLiga"), ("soccer/uefa.champions", "Champions League")]
DEFAULTS.update(sports_league="basketball/nba", sports_team="")
SETTINGS += [dict(kind="heading", label="Scores"),
             dict(key="sports_league", kind="choice", label="League", items=LEAGUES),
             dict(key="sports_team", kind="entry", apply=True, label="Your team", placeholder="abbreviation, e.g. BOS",
                  hint="Highlighted and listed first. Data from ESPN's public scoreboard.")]


def fetch_sports(ex):
    league = ex.cfg.get("sports_league", "basketball/nba")
    data = http_json(f"https://site.api.espn.com/apis/site/v2/sports/{league}/scoreboard")
    games = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        teams = sorted(comp["competitors"], key=lambda c: c.get("homeAway") != "away")          # away first
        games.append(dict(teams=[(t["team"].get("abbreviation", "?"), t.get("score", ""), t["team"].get("logo"))
                                 for t in teams], state=ev["status"]["type"].get("state"),
                          detail=ev["status"]["type"].get("shortDetail", ""), start=ev.get("date")))
    ex.pollers["sports"].every = 30 if any(g["state"] == "in" for g in games) else 600       # live: often
    return dict(league=league, games=games)


@tile("sports", "Scores", group="net")
def draw_sports(d):
    pl = d.ex.poll("sports", fetch_sports, 60, key=d.cfg.get("sports_league", "basketball/nba"))
    if d.waiting(pl):
        return
    games = pl.value["games"]
    if not games:
        d.msg("no games today")
        return
    fav = d.cfg.get("sports_team", "").strip().upper()
    games = sorted(games, key=lambda g: (not any(t[0] == fav for t in g["teams"]) if fav else 0,
                                         {"in": 0, "pre": 1, "post": 2}.get(g["state"], 3), g["start"] or ""))
    rows, nf = [], d.r.f_num
    for g in games:
        (a, sa, la), (b, sb, lb) = g["teams"][:2]
        col = d.GOLD if fav and fav in (a, b) else None
        if g["state"] == "pre":
            st = datetime.fromisoformat(g["start"].replace("Z", "+00:00")).astimezone()
            score, state = "vs", st.strftime("%H:%M" if st.date() == date.today() else "%a %H:%M")
        else:
            score, state = f"{sa} : {sb}", g["detail"]
        rows.append([("i", ICONS.get(la)), ("t", f" {a}  ", col, nf), ("t", score, col, nf), ("t", f"  {b} ", col, nf),
                     ("i", ICONS.get(lb)), ("t", f"   {state}", d.GREEN if g["state"] == "in" else d.p["dim"])])
    d.lines(rows, size=max(12, d.ssz * 1.25))


# ---------- window tile: a real window (League Pass in Chrome, a stream, a chat) in the tile's place ----------
DEFAULTS["window_tile"] = {"match": "", "command": ""}
SETTINGS += [dict(kind="heading", label="Window tile"),
             dict(key="window_tile.match", kind="entry", apply=True, label="Window",
                  placeholder="part of its title, e.g. NBA, then Enter",
                  hint="KWin puts the one window whose title contains this (at least 3 letters) exactly over the "
                       "'Window' tile, without frame and above the game. It applies when you press Enter. Steam, "
                       "the game and KDE's own windows are never taken. Remove the text to give the window back."),
             dict(key="window_tile.command", kind="entry", apply=True, label="Open with",
                  placeholder="google-chrome-stable --app=https://www.nba.com/watch",
                  hint="Clicking the empty tile runs this. For NBA League Pass use Chrome or Firefox, as streams "
                       "with copy protection don't play anywhere else. Video costs GPU power while you play."),]
WINDOW_SCRIPT = "letterbox-window-tile"
FIREFOX_PROFILE = os.path.expanduser("~/.local/share/letterbox/firefox-tile")
FIREFOX_PREFS = """// letterbox: a Firefox only for the window tile
user_pref("full-screen-api.ignore-widgets", true);   // a player's fullscreen fills the window, not the screen
user_pref("media.eme.enabled", true);                // copy protected streams (NBA League Pass, Netflix, ...)
user_pref("media.gmp-widevinecdm.enabled", true);
user_pref("media.gmp-widevinecdm.visible", true);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);   // for the hidden toolbars below
"""
# toolbars out of the way (more room for the video), back when the mouse touches the window's top edge
FIREFOX_CSS = """/* letterbox: the tile's Firefox shows only the page; the toolbars slide in at the top edge */
#navigator-toolbox { margin-top: -88px !important; opacity: 0; transition: margin-top .15s, opacity .15s; z-index: 10; }
#navigator-toolbox:hover, #navigator-toolbox:focus-within { margin-top: 0 !important; opacity: 1; }
"""


def setup_firefox_profile():
    """The tile's own Firefox profile (made or brought up to date). -> the profile folder."""
    os.makedirs(os.path.join(FIREFOX_PROFILE, "chrome"), exist_ok=True)
    with open(os.path.join(FIREFOX_PROFILE, "user.js"), "w") as fh:
        fh.write(FIREFOX_PREFS)
    with open(os.path.join(FIREFOX_PROFILE, "chrome", "userChrome.css"), "w") as fh:
        fh.write(FIREFOX_CSS)
    return FIREFOX_PROFILE


def firefox_command(old_command):
    """'Open with' for the tile's Firefox, keeping the page of the old command."""
    m = re.search(r"https?://\S+", old_command or "")
    url = m.group(0) if m else "https://www.nba.com/watch"
    return f"firefox --no-remote --profile {setup_firefox_profile()} --new-window {url}"


def firefox_settings(win, f):
    """Button: make the tile's own Firefox profile and use it as the 'Open with' command."""
    from gi.repository import Gtk
    box = Gtk.Box(spacing=8)
    btn = Gtk.Button(label="Use a Firefox made for the tile")
    note = Gtk.Label(css_classes=["dim-label"], xalign=0, wrap=True, hexpand=True)

    def make(_b):
        wt = dict(win.c.get("window_tile") or {})
        wt["command"] = firefox_command(wt.get("command", ""))
        win.c["window_tile"] = wt
        win.changed()
        note.set_text("Done. Restart the settings to see the new command. Log in once in that Firefox.")
    btn.connect("clicked", make)
    box.append(btn)
    box.append(note)
    return box


SETTINGS.append(dict(key="window_tile.firefox", kind="custom", label="", build=firefox_settings,
                     hint="Its own Firefox profile where a player's fullscreen fills the window instead of the "
                          "screen, so the video can fill the tile. Your normal Firefox stays untouched. The first "
                          "time the window opens it's big, to log in and pick a game. Meta+Shift+W puts it in the "
                          "tile and sets it free again."))


def window_click(ex, cfg):
    """No window yet: open it, free and big for setting up. Otherwise switch between free and in the tile
    (the tile's title row stays clickable while the window covers the rest)."""
    if getattr(ex, "window_held", False) or getattr(ex, "window_free", False):
        window_toggle(ex)
        return
    cmd = (cfg.get("window_tile") or {}).get("command", "").strip()
    if cmd:
        import shlex
        subprocess.Popen(shlex.split(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        ex.window_free = True                             # first set it up (log in, pick a game), then park it


def bar_area(cfg, sc):
    """Where the bar is on screen, in logical coordinates: (x, y, w, h); sc: the monitor (bar.screen())."""
    s = sc["scale"]
    lw, lh, game = sc["w"] / s, sc["h"] / s, cfg["height"] / s
    pos = cfg.get("position", "bottom")
    free = lh - game
    if pos == "top":
        return sc["x"], sc["y"] + game, lw, free
    return sc["x"], sc["y"], lw, free / 2 if pos == "center" else free


def free_area(cfg, sc, share=0.9):
    """A big rectangle on the monitor that the bar doesn't cover (the game's area), logical coordinates."""
    bx, by, bw, bh = bar_area(cfg, sc)
    lh = sc["h"] / sc["scale"]
    top, bottom = (by + bh, sc["y"] + lh) if by <= sc["y"] + 1 else (sc["y"], by)
    w, h = bw * share, (bottom - top) * share
    return {"x": bx + (bw - w) / 2, "y": top + (bottom - top - h) / 2, "width": w, "height": h}


def move_out_of_bar(cfg, sc, resource_class):
    """One-shot KWin script: the given app's windows leave the bar's area (moved, and shrunk if needed, into
    the rest of the monitor). Used for the settings while the bar is shown. sc: the monitor."""
    if not sc:
        return
    bx, by, bw, bh = bar_area(cfg, sc)
    area = free_area(cfg, sc, share=1.0)
    js = ("const B = {x: %s, y: %s, width: %s, height: %s}; const A = %s;\n"
          "workspace.windowList().forEach(w => { if (w.resourceClass !== %s) return;\n"
          "  const g = w.frameGeometry;\n"
          "  const overlap = g.x < B.x + B.width && g.x + g.width > B.x && g.y < B.y + B.height && g.y + g.height > B.y;\n"
          "  if (!overlap) return;\n"
          "  if (w.maximizable) w.setMaximize(false, false);\n"
          "  const h = Math.min(g.height, A.height - 20), wd = Math.min(g.width, A.width - 20);\n"
          "  w.frameGeometry = {x: Math.max(A.x + 10, Math.min(g.x, A.x + A.width - wd - 10)),\n"
          "                     y: A.y + Math.max(10, (A.height - h) / 2), width: wd, height: h}; });\n"
          % (bx, by, bw, bh, json.dumps(area), json.dumps(resource_class)))
    try:
        _run_script("letterbox-move-out", js)
        time.sleep(0.3)
        _kwin("unloadScript", "letterbox-move-out", sig="(s)")
    except GLib.Error:
        pass


def window_toggle(ex, _arg=None):
    """Meta+Shift+W or a click: free the window (big, normal) or put it back into the tile."""
    ex.window_free = not getattr(ex, "window_free", False)


@tile("window", "Window", group="net", click=window_click)
def draw_window(d):
    wt = d.cfg.get("window_tile") or {}
    if len(wt.get("match", "").strip()) < 3:
        d.msg("pick a window in the settings (Extras)")
        return
    head = d.fh * 0.16                                    # the window covers the heading too: more picture
    d.ex.window_rect = (d.x, d.y - head, d.w, d.h + head)  # placed by window_place() after the frame
    d.cr.set_source_rgba(1, 1, 1, 0.04)
    d.cr.rectangle(d.x, d.y, d.w, d.h)
    d.cr.fill()
    if getattr(d.ex, "window_free", False):
        d.msg(f"'{wt['match']}' is free to set up. Press Meta+Shift+W or click here to put it in")
    else:
        d.msg(f"'{wt['match']}' goes here" + (", click to open it" if wt.get("command") else ""))


def _kwin(method, *args, sig=None):
    return Gio.bus_get_sync(Gio.BusType.SESSION).call_sync(
        "org.kde.KWin", args[0] if method == "run" else "/Scripting",
        "org.kde.kwin.Script" if method == "run" else "org.kde.kwin.Scripting", method,
        GLib.Variant(sig, args) if sig else None, None, Gio.DBusCallFlags.NONE, 1000, None)


def _window_js(match, body):
    """KWin script code: hit(w) = the one kind of window the tile may take. Plain text in the title (or app id),
    at least 3 characters, never the bar, the game, Steam or KDE's own windows."""
    return ("const M = %s;\n"
            "const SKIP = /^(letterbox|steam|org\\.kde\\.|plasmashell|kwin)/;\n"
            "function hit(w) { return w.normalWindow && !SKIP.test(w.resourceClass) && M.length >= 3 &&\n"
            "  (w.caption.toLowerCase().includes(M) || w.resourceClass.toLowerCase().includes(M)); }\n"
            % json.dumps(match.strip().lower())) + body


def _run_script(name, js):
    path = os.path.join(GLib.get_user_runtime_dir(), name + ".js")
    with open(path, "w") as fh:
        fh.write(js)
    try:
        _kwin("unloadScript", name, sig="(s)")
    except GLib.Error:
        pass
    sid = _kwin("loadScript", path, name, sig="(ss)").unpack()[0]
    _kwin("run", f"/Scripting/Script{sid}")


def window_place(bar):
    """After each frame of the real bar: keep a KWin script loaded that holds ONE matching window over the
    tile (in screen coordinates), or let it go when the tile isn't shown or the setting changed."""
    ex = bar.data.extras
    rect, ex.window_rect = getattr(ex, "window_rect", None), None
    match = (bar.cfg.get("window_tile") or {}).get("match", "").strip()
    sc = bar.scr
    if not rect or len(match) < 3 or not sc:
        if getattr(ex, "window_loaded", None):
            window_unload(ex)
        return
    if getattr(ex, "window_free", False):                 # setting up: a big normal window next to the bar
        if getattr(ex, "window_loaded", None):
            window_unload(ex, free_area(bar.cfg, sc))
        return
    # bar pixels -> logical screen coordinates; the bar is at the top of the monitor, or under the game
    k = (sc["w"] / sc["scale"]) / max(1, bar.area.get_width())
    top = bar.cfg["height"] / sc["scale"] if bar.cfg.get("position") == "top" else 0
    want = (match,) + tuple(round(v, 1) for v in (sc["x"] + rect[0] * k, sc["y"] + top + rect[1] * k,
                                                    rect[2] * k, rect[3] * k))
    if want == getattr(ex, "window_loaded", None):
        return
    if getattr(ex, "window_loaded", None):
        window_unload(ex)                                 # gives the old window back first
    js = _window_js(match, (
        "const R = {x: %s, y: %s, width: %s, height: %s};\n"
        "let held = null;                                  // only ever one window\n"
        "function release(w) { w.noBorder = false; w.keepAbove = false; held = null; }\n"
        "function put(w) {\n"
        "  if (held && held !== w) return;\n"
        "  if (!hit(w)) { if (held === w) release(w); return; }\n"
        "  if (held !== w) callDBus('letterbox.bar', '/letterbox/bar', 'letterbox.Bar', 'Action',   // its size, for later\n"
        "    'window-was:' + JSON.stringify(w.frameGeometry));\n"
        "  held = w; if (w.fullScreen) w.fullScreen = false;\n"
        "  w.noBorder = true; w.keepAbove = true;\n"
        "  const g = w.frameGeometry;\n"
        "  if (Math.abs(g.x - R.x) + Math.abs(g.y - R.y) + Math.abs(g.width - R.width) + Math.abs(g.height - R.height) > 1)\n"
        "    w.frameGeometry = R;\n"
        "}\n"
        "function watch(w) { put(w); w.captionChanged.connect(() => put(w));\n"
        "  w.frameGeometryChanged.connect(() => { if (held === w) put(w); }); }\n"
        "workspace.windowRemoved.connect(w => { if (held === w) { held = null;\n"
        "  callDBus('letterbox.bar', '/letterbox/bar', 'letterbox.Bar', 'Action', 'window-gone'); } });\n"
        "workspace.windowAdded.connect(watch); workspace.windowList().forEach(watch);\n") % want[1:])
    try:
        _run_script(WINDOW_SCRIPT, js)
        ex.window_loaded = want
    except GLib.Error as err:
        print("letterbox: window tile:", err.message, flush=True)


def window_unload(ex, size=None):
    """Unload the tile's script and give the window back: frame on, not pinned on top any more, its old size
    (or `size`)."""
    old = getattr(ex, "window_loaded", None)
    try:
        _kwin("unloadScript", WINDOW_SCRIPT, sig="(s)")
    except GLib.Error:
        pass
    ex.window_loaded = None
    if old:
        was = size or getattr(ex, "window_was", None)       # its size before the tile took it
        size = f"w.frameGeometry = {json.dumps(was)};" if was else ""
        ex.window_was = None
        try:
            _run_script(WINDOW_SCRIPT + "-release", _window_js(old[0], (
                "workspace.windowList().forEach(w => { if (hit(w) && w.noBorder && w.keepAbove) {\n"
                "  w.noBorder = false; w.keepAbove = false; %s } });\n" % size)))
            time.sleep(0.3)
            _kwin("unloadScript", WINDOW_SCRIPT + "-release", sig="(s)")
        except GLib.Error:
            pass


def window_was(ex, arg):
    ex.window_held = True
    try:
        g = json.loads(arg)
        ex.window_was = {k: float(g[k]) for k in ("x", "y", "width", "height")}
    except (ValueError, KeyError, TypeError):
        pass


def window_gone(ex, _arg=None):
    ex.window_held, ex.window_free = False, False


ACTIONS.update({"window-was": window_was, "window-gone": window_gone, "window": window_toggle})
ON_EXIT.append(lambda bar: window_unload(bar.data.extras))
AFTER_DRAW.append(window_place)


def birthdays_soon(ex):
    """[(name, years, days)] of characters whose birthday is today (0) or tomorrow (1), for the clock."""
    if not ex.key:
        return []
    v = ex.poll("chars", fetch_chars, 3600).value
    today, out = date.today(), []
    for c in (v or {}).get("chars", []):
        made = datetime.fromisoformat(c["created"].replace("Z", "+00:00")).astimezone().date()
        for days in (0, 1):
            day = today + timedelta(days=days)
            if (made.month, made.day) == (day.month, day.day) and day.year > made.year:
                out.append((c["name"], day.year - made.year, days))
    return sorted(out, key=lambda b: b[2])                   # today first
