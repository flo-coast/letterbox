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
    pl = d.ex.poll("tp", fetch_tp, 120)
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

            def set_target(e, n=n):
                v = parse_coins(e.get_text())
                if v is not None:
                    lst = win.c["tp_watch"]
                    lst[n]["target"] = v
                    win.c["tp_watch"] = lst
                    win.changed()
            tgt.connect("changed", set_target)
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
