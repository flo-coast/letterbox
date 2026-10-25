#!/usr/bin/env python3
# bar.py: the stats strip next to GW2 in letterbox mode (see the "letterbox" script). Native X11 GTK4
# window, app id "letterbox.bar"; the KWin script placement.js puts it in place. Everything is configured
# in ~/.config/letterbox/config.json (settings app settings.py, which also uses Data/Renderer below for
# its live preview).
#
# Data (Data.update once per second): sysfs (sensors.py: CPU/GPU clocks, power, temps, fans; GPU busy 5x
# per second, VRAM, memory, network), the GW2 logger (logger/letterbox-gw2log.py: map, combat, character,
# GW2's busiest thread), MangoHud's frame log (FPS, average, 1 % low) and the game connection's TCP
# round-trip time (ping, from "ss", no packets sent). Optional: the log of the t2pm probe on T2 MacBooks
# (Apple SMC power budget tiles).
# Modes (Meta+Shift+B, D-Bus Cycle): full -> compact -> clock -> off.
# Background "ambient": a blurred, dimmed colour strip sampled from the edge of the game image through
# KWin's ScreenShot2 API (area passed by the letterbox script in LETTERBOX_AMBIENT_AREA).
import glob, json, math, os, re, subprocess, sys, threading, time, urllib.error, urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

import numpy as np
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gw2events
import sensors

CONF = os.path.expanduser("~/.config/letterbox/config.json")
MARKERS = os.path.expanduser("~/.local/share/letterbox/markers.log")
GW2LOG = "/var/log/letterbox"                # logger/letterbox-gw2log.py: gw2-YYYYMMDD.csv
PROBE = "/var/log/t2pm/latest.csv"           # optional: t2pm probe on T2 MacBooks (Apple SMC budget)
HAS_PROBE = os.path.exists(PROBE)
MODES = ["full", "compact", "clock", "off"]

# colour groups; a theme gives each group and text role a colour, "colors" in the config overrides
THEMES = {
    "classic": dict(fps="#a8e04a", cpu="#5b9ef9", gpu="#f26b5b", smc="#f9c24d", fan="#66d19e", mem="#b48cf2", game="#4fd1d9",
                 net="#9aa5b1", number="#dcdee2", dim="#737980", clock="#737980", bg="#000000"),
    "GW2 gold": dict(fps="#f2e6a8", cpu="#d9b46a", gpu="#c98b4a", smc="#e8c77a", fan="#a8b37a", mem="#b99a6b", game="#e0c48a",
                     net="#a39a86", number="#f0e3c4", dim="#8c7f66", clock="#c9a85c", bg="#0b0906"),
    "monochrome": dict(fps="#ffffff", cpu="#c8c8c8", gpu="#c8c8c8", smc="#c8c8c8", fan="#c8c8c8", mem="#c8c8c8",
                       game="#c8c8c8", net="#c8c8c8", number="#ffffff", dim="#7a7a7a", clock="#9a9a9a",
                       bg="#000000"),
    "neon": dict(fps="#ccff00", cpu="#00e5ff", gpu="#ff2bd6", smc="#fff200", fan="#39ff14", mem="#b026ff", game="#00ffa3",
                 net="#8f9bff", number="#ffffff", dim="#8a8aa8", clock="#ff2bd6", bg="#05010a"),
}

DEFAULTS = {
    "height": None, "position": "bottom",  # game height in px; None = default_height() of the monitor
    "sensors": True,                       # False = plain black strip (legacy switch)
    "mode": "full",
    "tiles": ["fps", "ping", "cpu", "ctemp", "thread", "gpu", "busy", "gtemp", "events", "bottleneck"],
    "compact_tiles": ["fps", "ping", "bottleneck", "gtemp"],
    "output": "",                          # monitor (e.g. "DP-1"); empty = the primary one
    "gpu": "",                             # PCI address of the GPU to show (e.g. "0000:03:00.0"); empty = auto
    "mangohud_dir": "~/.local/share/letterbox/mangohud",   # MangoHud output_folder (FPS tiles)
    "api_key": "",                         # GW2 API key ("account" permission): finds your WvW team
    "game_dir": "",                        # GW2's folder (Gw2-64.exe), for the Nexus bridge; empty = Steam library
    "bridge_file": "",                     # bridge.json of the Nexus addon; empty = in game_dir
    "graph_seconds": 60,
    "theme": "classic", "colors": {},         # colors: overrides per group/role or per tile key
    "background": "black",                 # black | ambient
    "ambient_dim": 0.35, "ambient_rate": 4,
    "brightness": 1.0,
    "clock": {"style": "digital", "h24": True, "seconds": False, "date": False, "tyria": True,
              "resets": True, "size": 1.0, "show": True},
    "tile_opts": {},                       # per tile: {"number": bool, "graph": bool}
    "event_sections": ["World bosses", "Hard world bosses"],   # rows of the event timeline tile
    "event_auto_map": True,                # + the meta of the map you are on, if the data has one
    "event_ahead": 110,                    # minutes of future shown in the timeline (10 min of past)
    "event_row_max": 0,                    # max height of one timeline bar in px, 0 = share the tile
    "event_row_gap": 0.16,                 # vertical gap between timeline bars, share of a row
    "event_col_gap": 2,                    # px between events that follow each other directly
    "event_row_heights": {},               # per timeline row (section name): height factor, default 1
    "tile_gap": 1.0,                       # space between tiles, factor of the default padding
    "clock_events": ["World bosses"],      # "next: ..." line under the clock
    # start warning under the clock: source clock | timeline | all | custom (alert_events)
    "alert": {"on": True, "source": "clock", "lead": 5, "color": "#ff8a3d", "sound": False, "blink": True},
    "alert_events": ["World bosses", "Hard world bosses"],
    "font_text": "sans", "font_number": "monospace",   # font families (fontconfig names)
    "combat": "dim",                       # off | dim | warnings: what happens to the tiles in combat
    "warnings_only": False,
}


def load_config():
    try:
        c = json.load(open(CONF))
    except (OSError, ValueError):
        c = {}
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in c.items():
        if k in ("clock", "alert") and isinstance(v, dict):
            out[k].update(v)
        else:
            out[k] = v
    if not out.get("height"):
        sc = screen(out)
        out["height"] = default_height(sc["w"], sc["h"]) if sc else 1800
    return out


def default_height(w, h):
    """2.13 : 1 (3840x1800 on 4K, 2560x1200 on 1440p), but at least 10 % less than the monitor."""
    return min(w * 15 // 32, h * 9 // 10) // 2 * 2


def outputs():
    """Enabled monitors, primary (KDE "priority 1") first: [dict(name, w, h (mode in device pixels),
    x, y (logical position), scale, primary)]."""
    try:
        outs = json.loads(subprocess.run(["kscreen-doctor", "-j"], capture_output=True, text=True,
                                         timeout=5).stdout)["outputs"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return []
    res = []
    for o in sorted((o for o in outs if o.get("enabled")), key=lambda o: o.get("priority") or 99):
        mode = next((m for m in o.get("modes", []) if m["id"] == o.get("currentModeId")), None)
        if not mode:
            continue
        w, h = mode["size"]["width"], mode["size"]["height"]
        if o.get("rotation") in (2, 8):                    # portrait
            w, h = h, w
        res.append(dict(name=o["name"], w=w, h=h, x=o["pos"]["x"], y=o["pos"]["y"], scale=o.get("scale") or 1,
                        primary=o.get("priority") == 1))
    return res


def screen(cfg=None):
    """The monitor GW2 goes on: the configured output, else the primary one. -> dict (see outputs) or None."""
    cfg = cfg if cfg is not None else load_config()
    outs = outputs()
    return next((o for o in outs if o["name"] == cfg.get("output")), outs[0] if outs else None)


def rgb(hexstr):
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def color(cfg, key):
    """Colour for a group/role/tile key: override in the config, then the theme."""
    th = THEMES.get(cfg.get("theme"), THEMES["classic"])
    v = cfg.get("colors", {}).get(key) or th.get(key)
    return rgb(v) if v else None


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tail_row(path, cache):
    """Last complete CSV row of a growing log as dict (header read once per file/inode)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if cache.get("ino") != st.st_ino or cache.get("path") != path:
        try:
            with open(path, errors="replace") as fh:
                cache.update(ino=st.st_ino, path=path, header=fh.readline().strip().split(","))
        except OSError:
            return None
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, st.st_size - 4096))
            lines = fh.read().decode(errors="replace").strip().splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    vals = lines[-1].split(",")
    return dict(zip(cache["header"], vals)) if len(vals) == len(cache["header"]) else None


class MangoTail:
    """Follows the newest MangoHud log of GW2 and keeps the frame times of the last 60 s. MangoHud writes
    in blocks, so the numbers lag a few seconds behind; fine for averages."""

    def __init__(self, folder):
        self.folder = os.path.expanduser(folder)
        self.path, self.pos, self.cols, self.buf = None, 0, None, b""
        self.frames = deque()          # (elapsed s, frametime ms)

    def update(self):
        logs = sorted(glob.glob(f"{self.folder}/Gw2-64_*.csv"), key=os.path.getmtime)
        if not logs or time.time() - os.path.getmtime(logs[-1]) > 30:
            self.frames.clear()
            return
        if logs[-1] != self.path:
            self.path, self.pos, self.cols, self.buf = logs[-1], 0, None, b""
            self.frames.clear()
        try:
            with open(self.path, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                if self.pos == 0:
                    head = fh.read(4096).split(b"\n")
                    if len(head) > 2 and head[2].startswith(b"fps,"):
                        self.cols = head[2].decode().split(",")
                        self.pos = max(len(b"\n".join(head[:3])) + 1, size - 300_000)  # joining late: tail only
                fh.seek(self.pos)
                data = self.buf + fh.read()
                self.pos = fh.tell()
        except OSError:
            return
        if not self.cols:
            return
        lines = data.split(b"\n")
        self.buf = lines.pop()                           # incomplete last line
        i_ft, i_el = self.cols.index("frametime"), self.cols.index("elapsed")
        for ln in lines:
            p = ln.split(b",")
            if len(p) != len(self.cols):
                continue
            try:
                self.frames.append((int(p[i_el]) / 1e9, float(p[i_ft])))
            except ValueError:
                continue
        while self.frames and self.frames[-1][0] - self.frames[0][0] > 60:
            self.frames.popleft()

    def stats(self):
        """-> (FPS of the newest second, 60 s average FPS, 1 % low) or Nones."""
        if len(self.frames) < 30:
            return None, None, None
        last = self.frames[-1][0]
        fts = [ft for _, ft in self.frames if ft < 2000]
        recent = [ft for el, ft in self.frames if el > last - 1 and ft < 2000]
        now = len(recent) * 1000 / sum(recent) if recent else None
        avg = len(fts) * 1000 / sum(fts)
        worst = sorted(fts, reverse=True)[:max(1, len(fts) // 100)]
        return now, avg, 1000 / (sum(worst) / len(worst))


def game_ping():
    """Ping to the GW2 server without sending packets: the kernel's smoothed TCP round-trip time of the
    game's most recently active connection (ss), in ms, or None when not connected."""
    try:
        out = subprocess.run(["ss", "-tinpH", "state", "established"], capture_output=True, text=True,
                             timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    best, best_last, game = None, 1e12, False
    for ln in out.splitlines():
        if not ln.startswith((" ", "\t")):                   # socket line; details follow indented
            game = '"Gw2-64.exe"' in ln and " 127." not in ln
            continue
        if game:
            m_rtt, m_last = re.search(r"\brtt:([\d.]+)", ln), re.search(r"\blastrcv:(\d+)", ln)
            last = int(m_last.group(1)) if m_last else 1e9
            if m_rtt and last < best_last:
                best, best_last = float(m_rtt.group(1)), last
            game = False
    return best


API = "https://api.guildwars2.com/v2"
WVW_MAPS = {38: "Center", 95: "GreenHome", 96: "BlueHome", 1099: "RedHome"}
WVW_MAP_NAMES = {"Center": "Eternal Battlegrounds", "RedHome": "Red Desert BL", "GreenHome": "Green Alpine BL",
                 "BlueHome": "Blue Alpine BL"}
TEAM_COLOR_IDS = {9: "blue", 55: "green", 376: "red"}       # MumbleLink identity team_color_id
WVW_COLORS = {"red": (0.92, 0.33, 0.3), "green": (0.38, 0.8, 0.42), "blue": (0.36, 0.6, 0.96)}


def api_get(path, key=None):
    url = f"{API}/{path}" + (("&" if "?" in path else "?") + f"access_token={key}" if key else "")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "letterbox"}), timeout=10) as r:
        return json.load(r)


class WvW:
    """Your WvW matchup from the GW2 API, refreshed every 30 s in a thread while a WvW tile is shown.
    Your team comes from the API key's account (/v2/account wvw.team_id). MumbleLink's world_id is an
    internal value since the WvW restructuring (e.g. 0x80000004) and doesn't name a team."""
    CACHE = os.path.expanduser("~/.cache/letterbox/wvw-objectives.json")

    def __init__(self):
        self.key = ""
        self.team = None                      # team or world id found in the match
        self.match = None
        self.err = "loading…"
        self.names = {}
        self.last_touch = 0
        self.thread = None

    def touch(self, key):
        """Called while a WvW tile is drawn: keeps the updates running."""
        self.last_touch = time.monotonic()
        if key != self.key:
            self.key, self.team = key, None
        if not self.thread or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()

    def loop(self):
        while time.monotonic() - self.last_touch < 120:      # stops 2 min after the last WvW tile was drawn
            try:
                self.refresh()
            except Exception as ex:                          # network, API down, bad key: show it, try again
                self.err = f"GW2 API: {ex}"[:80]
            time.sleep(30)

    def refresh(self):
        if not self.names:
            try:
                self.names = json.load(open(self.CACHE))
            except (OSError, ValueError):
                objs = api_get("wvw/objectives?ids=all")
                self.names = {o["id"]: o["name"] for o in objs}
                os.makedirs(os.path.dirname(self.CACHE), exist_ok=True)
                json.dump(self.names, open(self.CACHE, "w"))
        ids = []
        if self.key:
            if self.team is None:
                # the WvW team only comes with a newer schema version; "world" is the old server, which
                # since the WvW restructuring can point to a different matchup than your team
                acc = api_get("account?v=2024-07-20T01:00:00Z", self.key)
                self.team = (acc.get("wvw") or {}).get("team_id") or acc.get("world")
            ids.append(self.team)
        for wid in ids:
            if not wid:
                continue
            try:
                m = api_get(f"wvw/matches?world={wid}")
            except urllib.error.HTTPError:
                continue
            m["_mine"] = next((c for c, ws in m.get("all_worlds", {}).items() if wid in ws), None)
            self.match, self.err = m, ""
            return
        self.err = ("add a GW2 API key in the settings (Behaviour & hotkeys)" if not self.key
                    else "no matchup found for your team")

    # ---------- for the tiles ----------
    def mine(self, color_id=None):
        m = self.match or {}
        return m.get("_mine") or TEAM_COLOR_IDS.get(color_id)

    def skirmish(self):
        """-> (number, of, seconds left) of the current skirmish (2 h each from the match start)."""
        m = self.match
        if not m or not m.get("skirmishes"):
            return None
        start = datetime.fromisoformat(m["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(m["end_time"].replace("Z", "+00:00"))
        n = m["skirmishes"][-1]["id"]
        left = (start + timedelta(hours=2 * n) - datetime.now(timezone.utc)).total_seconds()
        return n, round((end - start).total_seconds() / 7200), max(0, left)

    def ppt(self):
        out = {"red": 0, "green": 0, "blue": 0}
        for mp in (self.match or {}).get("maps", []):
            for o in mp["objectives"]:
                c = o.get("owner", "").lower()
                if c in out:
                    out[c] += o.get("points_tick", 0)
        return out


PROFESSIONS = {1: ("Guardian", (0.45, 0.76, 0.85)), 2: ("Warrior", (1.0, 0.82, 0.4)),
               3: ("Engineer", (0.82, 0.61, 0.35)), 4: ("Ranger", (0.55, 0.86, 0.51)),
               5: ("Thief", (0.75, 0.56, 0.58)), 6: ("Elementalist", (0.96, 0.54, 0.53)),
               7: ("Mesmer", (0.71, 0.47, 0.84)), 8: ("Necromancer", (0.32, 0.65, 0.44)),
               9: ("Revenant", (0.82, 0.43, 0.35))}


def steam_libraries():
    libs = [os.path.expanduser(p) for p in ("~/.local/share/Steam", "~/.steam/steam")]
    for vdf in (f"{lib}/steamapps/libraryfolders.vdf" for lib in list(libs)):
        try:
            libs += re.findall(r'"path"\s*"([^"]+)"', open(vdf).read())
        except OSError:
            pass
    return list(dict.fromkeys(libs))


def bridge_path(cfg):
    if cfg.get("bridge_file"):
        return os.path.expanduser(cfg["bridge_file"])
    if cfg.get("game_dir"):
        return os.path.join(os.path.expanduser(cfg["game_dir"]), "addons", "letterbox", "bridge.json")
    for lib in steam_libraries():
        p = f"{lib}/steamapps/common/Guild Wars 2/addons/letterbox/bridge.json"
        if os.path.isdir(os.path.dirname(os.path.dirname(p))):
            return p
    return None


def compass(deg):
    if deg is None:
        return "-"
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg + 22.5) % 360 // 45)]


def fmt_dur(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class Data:
    """Everything the tiles show, refreshed once per second (GPU busy sampled 5x per second)."""

    def __init__(self, graph_seconds=60, cfg=None):
        cfg = cfg or load_config()
        self.sensors = sensors.Sensors(cfg.get("gpu", ""))
        self.gpu_dev = self.sensors.gpu
        self.pings = deque(maxlen=60)
        self.wvw = WvW()
        self.bridge_file = bridge_path(cfg)
        self.bridge = None                    # latest bridge.json of the Nexus addon, or None
        self.hist = {}
        self.graph_seconds = graph_seconds
        self.probe = self.mumble = None
        self._pc, self._mc = {}, {}
        self.mango = MangoTail(cfg.get("mangohud_dir") or DEFAULTS["mangohud_dir"])
        self.gpu_samples = deque(maxlen=20)
        self.net_last = None
        self.vals, self.subs, self.text = {}, {}, {}
        self.combat_since, self.combat_total, self.in_combat = None, 0, False
        self.fps_avg = self.fps_low = None

    def sample_gpu(self):
        try:
            self.gpu_samples.append(int(open(f"{self.gpu_dev}/gpu_busy_percent").read()))
        except (OSError, ValueError, TypeError):
            pass
        return True

    def update(self):
        probe = tail_row(PROBE, self._pc) if HAS_PROBE and time.time() - os.path.getmtime(PROBE) < 5 else None
        self.probe = probe
        files = sorted(glob.glob(f"{GW2LOG}/gw2-*.csv"), key=os.path.getmtime)
        mp = files[-1] if files else None
        self.mumble = tail_row(mp, self._mc) if mp and time.time() - os.path.getmtime(mp) < 5 else None
        self.mango.update()
        g = self.mumble or {}
        p = dict(probe) if probe else self.sensors.read()      # the probe has the same keys plus the SMC ones
        if p.get("pkg_w") is None and g.get("cpu_pkg_w"):      # RAPL is root-only: the logger reads it
            p["pkg_w"] = g["cpu_pkg_w"]
        v, sub = {}, {}
        fmt = lambda x, s="{:.0f}": "-" if x is None else s.format(x)
        ceil = f(p.get("max_perf_pct"))
        v["cpu"] = f(p.get("mhz_avg"))
        sub["cpu"] = (f"ceiling {fmt(ceil * 48 if ceil else None)} · MSLC {fmt(f(p.get('smc_MSLC')))}" if probe
                      else f"fastest core {fmt(f(p.get('mhz_max')))}")
        v["pkg"], sub["pkg"] = f(p.get("pkg_w")), (f"PL1 {fmt(f(p.get('pl1_w')))} W" if probe else "RAPL package")
        v["ctemp"], sub["ctemp"] = f(p.get("core_max_c")), f"package {fmt(f(p.get('pkg_c')))} °C"
        v["thread"] = f(g.get("thread1_pct"))
        sub["thread"] = f"{g.get('thread1_name') or '-'} · 2nd {fmt(f(g.get('thread2_pct')))} %"
        v["gpu"] = f(p.get("gpu_w"))
        sub["gpu"] = f"cap {fmt(f(p.get('gpu_cap_w')))} W" + (f" · MSLG {fmt(f(p.get('smc_MSLG')))}" if probe else "")
        v["sclk"], sub["sclk"] = f(p.get("gpu_sclk")), f"mem {fmt(f(p.get('gpu_mclk')))} MHz"
        gs = list(self.gpu_samples)
        self.gpu_samples.clear()
        v["busy"], sub["busy"] = (sum(gs) / len(gs) if gs else None), f"peak {max(gs) if gs else '-'} %"
        v["gtemp"], sub["gtemp"] = f(p.get("gpu_junc_c")), f"hotspot · edge {fmt(f(p.get('gpu_edge_c')))}"
        try:
            used = int(open(f"{self.gpu_dev}/mem_info_vram_used").read()) / 2**30
            tot = int(open(f"{self.gpu_dev}/mem_info_vram_total").read()) / 2**30
            v["vram"], sub["vram"] = used, f"of {tot:.0f} GiB"
        except (OSError, ValueError, TypeError):
            v["vram"] = None
        e, u = f(p.get("smc_PZ0E")), f(p.get("smc_PZ0F"))
        v["budget"], sub["budget"] = u, f"of {fmt(e, '{:.1f}')} W · T {fmt(f(p.get('smc_PZ0T')))}"
        v["fan"], sub["fan"] = f(p.get("fan1")), f"{fmt(f(p.get('fan1')))} / {fmt(f(p.get('fan2')))} rpm"
        mi = {}
        try:
            for line in open("/proc/meminfo"):
                k, val = line.split(":", 1)
                mi[k] = int(val.split()[0]) / 1048576
        except OSError:
            pass
        v["mem"] = mi["MemTotal"] - mi["MemAvailable"] if "MemAvailable" in mi else None
        sub["mem"] = f"of {mi.get('MemTotal', 0):.0f} GiB · swap {mi.get('SwapTotal', 0) - mi.get('SwapFree', 0):.1f}"
        rx = tx = 0
        try:
            for line in open("/proc/net/dev").readlines()[2:]:
                name, rest = line.split(":", 1)
                if name.strip() != "lo":
                    a = rest.split()
                    rx, tx = rx + int(a[0]), tx + int(a[8])
        except (OSError, ValueError):
            pass
        now = time.monotonic()
        if self.net_last:
            dt = now - self.net_last[0]
            v["net"] = (rx - self.net_last[1]) / dt / 1024
            sub["net"] = f"up {(tx - self.net_last[2]) / dt / 1024:.0f} KiB/s"
        self.net_last = (now, rx, tx)
        fnow, self.fps_avg, self.fps_low = self.mango.stats()
        v["fps"], sub["fps"] = fnow, f"avg {fmt(self.fps_avg)} · 1% low {fmt(self.fps_low)}"
        v["fps_avg"] = self.fps_avg                     # graph line for "FPS with average"
        v["ping"] = game_ping()
        if v["ping"] is not None:
            self.pings.append(v["ping"])
        sub["ping"] = f"avg {sum(self.pings) / len(self.pings):.0f} ms (60 s)" if self.pings else "-"
        self.bridge = None
        try:
            if self.bridge_file and time.time() - os.path.getmtime(self.bridge_file) < 5:
                self.bridge = json.load(open(self.bridge_file))
        except (OSError, ValueError):
            pass
        self.in_combat = g.get("in_combat") == "1"
        if self.in_combat and self.combat_since is None:
            self.combat_since = now
        if not self.in_combat and self.combat_since is not None:
            self.combat_total += now - self.combat_since
            self.combat_since = None
        cur = now - self.combat_since if self.combat_since else 0
        self.text = {
            "map": (g.get("map_name") or "-", f"{g.get('map_type') or '-'} · facing {compass(f(g.get('heading')))}"),
            "combat": (fmt_dur(cur) if self.combat_since else "out of combat",
                       f"this session {fmt_dur(self.combat_total + cur)} in combat"),
            "char": (g.get("char_name") or "-",
                     " · ".join(x for x in (g.get("spec") or g.get("profession"), g.get("mount"),
                                            "commander" if g.get("commander") == "1" else "") if x)),
            "bottleneck": self.bottleneck(p, g, v),
            "smc": self.traffic(p),
        }
        for key, val in v.items():
            h = self.hist.setdefault(key, deque(maxlen=600))
            h.append(val)
        self.vals, self.subs = v, sub

    def bottleneck(self, p, g, v):
        """What limits the game right now: the logic of today's manual analysis, live."""
        mslc, mslg = f(p.get("smc_MSLC")) or 0, f(p.get("smc_MSLG")) or 0
        e, u = f(p.get("smc_PZ0E")), f(p.get("smc_PZ0F"))
        busy, th = v.get("busy"), f(g.get("thread1_pct"))
        if busy is not None and busy >= 95:
            return ("GPU-bound", f"GPU {busy:.0f} % busy" + (" · SMC cap" if mslg else ""))
        if th is not None and th >= 85:
            return ("CPU-bound", f"main thread {th:.0f} % · {v.get('cpu') or 0:.0f} MHz")
        if mslc or mslg:
            return ("SMC budget", f"{u or 0:.0f}/{e or 0:.0f} W · MSLC {mslc:.0f}/{mslg:.0f}")
        return ("headroom", f"GPU {busy or 0:.0f} % · thread {th or 0:.0f} %")

    def traffic(self, p):
        e, mslc, mslg = f(p.get("smc_PZ0E")), f(p.get("smc_MSLC")) or 0, f(p.get("smc_MSLG")) or 0
        if e is None:
            return ("-", "no probe data")
        if e < 75 and (mslc or mslg):
            return ("red", f"budget down to {e:.0f} W, throttling")
        if mslc or mslg:
            return ("yellow", f"throttling · MSLC {mslc:.0f} MSLG {mslg:.0f}")
        return ("green", f"budget {e:.0f} W, no throttling")

    def warnings(self):
        v, p, out = self.vals, self.probe or {}, []
        if (v.get("gtemp") or 0) >= 95:
            out.append(f"GPU hotspot {v['gtemp']:.0f} °C")
        e = f(p.get("smc_PZ0E"))
        if e is not None and e < 70:
            out.append(f"SMC budget down to {e:.0f} W")
        if (v.get("thread") or 0) >= 90:
            out.append(f"GW2 main thread {v['thread']:.0f} %")
        if (v.get("busy") or 0) >= 97:
            out.append(f"GPU {v['busy']:.0f} % busy")
        if (v.get("ping") or 0) >= 150:
            out.append(f"ping {v['ping']:.0f} ms")
        if self.fps_low is not None and self.fps_low < 20:
            out.append(f"1 % low {self.fps_low:.0f} FPS")
        return out


# key: (title, unit, graph max, colour group, kind)
TILES = {
    "fps": ("FPS", "", 70, "fps", "num"),
    "ping": ("Ping", "ms", 200, "game", "num"),
    "map": ("Map", "", 0, "game", "text"),
    "combat": ("Combat", "", 0, "game", "text"),
    "char": ("Character", "", 0, "game", "text"),
    "events": ("Event timer", "", 0, "game", "events"),
    "wvw": ("WvW matchup", "", 0, "game", "wvw"),
    "wvwmap": ("WvW map", "", 0, "game", "wvwmap"),
    "squad": ("Squad", "", 0, "game", "squad"),
    "cpu": ("CPU", "MHz", 5000, "cpu", "num"),
    "pkg": ("CPU package", "W", 60, "cpu", "num"),
    "ctemp": ("CPU temp", "°C", 105, "cpu", "num"),
    "thread": ("GW2 main thread", "%", 110, "cpu", "num"),
    "gpu": ("GPU power", "W", 55, "gpu", "num"),
    "sclk": ("GPU clock", "MHz", 1500, "gpu", "num"),
    "busy": ("GPU busy", "%", 100, "gpu", "num"),
    "gtemp": ("GPU temp", "°C", 105, "gpu", "num"),
    "vram": ("VRAM", "GiB", 8, "gpu", "num"),
    "budget": ("SMC budget used", "W", 90, "smc", "num"),
    "smc": ("SMC state", "", 0, "smc", "light"),
    "bottleneck": ("Bottleneck", "", 0, "smc", "text"),
    "fan": ("Fans", "rpm", 6000, "fan", "num"),
    "mem": ("Memory", "GiB", 32, "mem", "num"),
    "net": ("Network down", "KiB/s", 2000, "net", "num"),
}
if not HAS_PROBE:                    # Apple SMC tiles only with the t2pm probe (T2 MacBooks)
    del TILES["budget"], TILES["smc"]
if not sensors.gpu_device(load_config().get("gpu", "")):    # GPU tiles need an AMD GPU (amdgpu sysfs)
    for _k in ("gpu", "sclk", "busy", "gtemp", "vram"):
        del TILES[_k]
TILE_WEIGHT = {"events": 3}          # tiles share the width by weight; the timeline needs room
MIN_FONT = 12                        # px: event names shrink to fit their block down to this, then get "…"
MIN_LABEL_ROW = 13                   # px: thinner timeline rows (many series chosen) are drawn without text
EVENTS = gw2events.load()
LIGHT = {"green": (0.3, 0.8, 0.4), "yellow": (0.95, 0.78, 0.25), "red": (0.95, 0.3, 0.25), "-": (0.4, 0.4, 0.4)}
BOTTLENECK_COL = {"GPU-bound": (0.95, 0.42, 0.36), "CPU-bound": (0.36, 0.62, 0.98),
                  "SMC budget": (0.98, 0.76, 0.3), "headroom": (0.4, 0.82, 0.62)}


def rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def fit_text(cr, text, width, size, min_size):
    """Largest font size from size down to min_size at which text fits width; if it still doesn't fit,
    the text is shortened with an ellipsis. -> (text, size)"""
    cr.set_font_size(size)
    while cr.text_extents(text).x_advance > width and size > min_size:
        size = max(min_size, size * 0.92)
        cr.set_font_size(size)
    if cr.text_extents(text).x_advance > width:
        while len(text) > 1 and cr.text_extents(text + "…").x_advance > width:
            text = text[:-1]
        text = text.rstrip() + "…" if len(text) > 1 else ""
    return text, size


def layout(cfg, mode):
    """Blocks for a mode: [{"w": width weight, "rows": [[tile, ...], ...]}, ...]. Without a saved layout,
    every tile of the old flat list is its own block (event timer 3 wide)."""
    lay = cfg.get("compact_layout" if mode == "compact" else "layout")
    if not lay:
        keys = cfg["compact_tiles"] if mode == "compact" else cfg["tiles"]
        lay = [{"w": TILE_WEIGHT.get(k, 1), "rows": [[k]]} for k in keys]
    out = []
    for b in lay:
        rows = [[k for k in r if k in TILES] for r in b.get("rows", [])]
        rows = [r for r in rows if r]
        if rows:
            out.append({"w": max(0.25, float(b.get("w", 1))), "rows": rows})
    return out


def tyria(now_utc):
    """Central Tyria day/night, 2 h cycle starting with day at even UTC hours + 30 min:
    day 70 min, dusk 5, night 40, dawn 5 (GW2 wiki, Template:Day-night cycle). -> (phase, minutes left)."""
    m = ((now_utc.hour % 2) * 60 + now_utc.minute + now_utc.second / 60 - 30) % 120
    for name, length in (("day", 70), ("dusk", 5), ("night", 40), ("dawn", 5)):
        if m < length:
            return name, length - m
        m -= length
    return "day", 0


def resets(now_utc):
    """Minutes to the daily (00:00 UTC) and weekly (Monday 07:30 UTC) reset."""
    daily = 24 * 60 - (now_utc.hour * 60 + now_utc.minute + now_utc.second / 60)
    nxt = now_utc.replace(hour=7, minute=30, second=0, microsecond=0) + timedelta(days=(7 - now_utc.weekday()) % 7)
    if nxt <= now_utc:
        nxt += timedelta(days=7)
    return daily, (nxt - now_utc).total_seconds() / 60


class Ambient:
    """Blurred colour strip from the game edge: KWin ScreenShot2 CaptureArea, a robust colour per column
    (median over the strip's rows ignores small UI elements), eased towards the new colours."""
    COLS = 48

    def __init__(self, area):
        self.area = area                       # (x, y, w, h) logical
        self.target = self.cur = None
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.ok = bool(area)

    def grab(self):
        if not self.ok:
            return
        try:
            import capture
            w, h, stride, data, _fmt = capture.capture_area(self.conn, *self.area)
            img = np.frombuffer(data, np.uint8)[:h * stride].reshape(h, stride // 4, 4)[:, :w, :3]
            cols = np.median(img, axis=0)
            self.target = np.array([b.mean(axis=0) for b in np.array_split(cols, self.COLS)]) / 255
        except Exception as ex:                # capture refused or failed: stay with the plain background
            print("gw2-bar: ambient capture failed, plain background:", ex, flush=True)
            self.ok = False

    def step(self, dt, tau=0.5):
        if self.target is None:
            return None
        if self.cur is None:
            self.cur = self.target.copy()
        self.cur += (self.target - self.cur) * min(1, dt / tau)
        return self.cur


NO_ALERT = {"Day and night", "Cantha: Day and night"}       # cycles, not events


class Renderer:
    def __init__(self):
        self.flash_until = 0
        self.alerted = set()                  # events the sound was played for

    def alert(self, cfg, data):
        """-> (text, seconds to start) of the next event start to warn about, or None."""
        al = cfg.get("alert", {})
        if not al.get("on", True):
            return None
        src = al.get("source", "clock")
        names = {"clock": cfg.get("clock_events", []), "timeline": cfg.get("event_sections", []),
                 "custom": cfg.get("alert_events", [])}.get(src)
        secs = [x for x in EVENTS if (names is None or x["name"] in names) and x["name"] not in NO_ALERT]
        now = datetime.now(timezone.utc)
        lead = al.get("lead", 5) * 60
        best = None
        for a, _b, sname, seg in gw2events.upcoming(secs, now, al.get("lead", 5) + 5):
            dt = (a - now).total_seconds()
            if -120 <= dt <= lead and (best is None or dt < best[1]):
                best = (f"{seg['name']}" + (f" ({sname})" if sname not in ("World bosses", "Hard world bosses") else ""),
                        dt, (sname, a))
        if best and al.get("sound") and best[2] not in self.alerted and best[1] > 0:
            self.alerted.add(best[2])
            subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/bell.oga"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return best

    def draw(self, cr, w, h, cfg, data, mode, ambient_cols=None):
        self.f_text = cfg.get("font_text") or "sans"
        self.f_num = cfg.get("font_number") or "monospace"
        cr.set_source_rgb(*(color(cfg, "bg") or (0, 0, 0)))
        cr.paint()
        if cfg.get("background") == "ambient" and ambient_cols is not None and mode != "off":
            self.draw_ambient(cr, w, h, ambient_cols, cfg.get("ambient_dim", 0.35), cfg.get("position"))
        if mode != "off":
            cr.push_group()
            self.draw_content(cr, w, h, cfg, data, mode)
            cr.pop_group_to_source()
            cr.paint_with_alpha(cfg.get("brightness", 1.0))
        if time.time() < self.flash_until:                       # lag marker confirmation: red frame + banner
            red = (0.95, 0.25, 0.2)
            cr.set_source_rgba(*red, 0.9)
            cr.set_line_width(h * 0.04)
            cr.rectangle(0, 0, w, h)
            cr.stroke()
            bw, bh = min(w * 0.3, h * 2.6), h * 0.36
            bx, by = (w - bw) / 2, (h - bh) / 2
            rounded(cr, bx, by, bw, bh, bh * 0.2)
            cr.set_source_rgba(0.08, 0.02, 0.02, 0.92)
            cr.fill_preserve()
            cr.set_source_rgb(*red)
            cr.set_line_width(max(2, h * 0.01))
            cr.stroke()
            cr.arc(bx + bh * 0.5, by + bh / 2, bh * 0.18, 0, 2 * math.pi)       # red dot (drawn, not a glyph)
            cr.fill()
            self.text(cr, "marker set", bx + bh * 0.85, by + bh * 0.64, bw - bh, bh * 0.42, (1, 1, 1))

    def draw_ambient(self, cr, w, h, cols, dim, position):
        import cairo
        g = cairo.LinearGradient(0, 0, w, 0)
        n = len(cols)
        for i, c in enumerate(cols):
            g.add_color_stop_rgb(i / (n - 1), *(float(x) * dim for x in c))
        cr.set_source(g)
        cr.paint()
        # darker towards the screen edge, full colour next to the game
        game_below = position != "top"
        v = cairo.LinearGradient(0, 0, 0, h)
        v.add_color_stop_rgba(0, 0, 0, 0, 0.6 if game_below else 0.0)
        v.add_color_stop_rgba(1, 0, 0, 0, 0.0 if game_below else 0.6)
        cr.set_source(v)
        cr.paint()

    def draw_content(self, cr, w, h, cfg, data, mode):
        clk = cfg["clock"]
        pad = h * 0.08
        clock_w = 0
        if mode == "clock":
            self.draw_clock(cr, 0, 0, w, h, cfg, True)
            return
        if clk.get("show", True) and clk.get("style") != "off":
            clock_w = min(w * 0.26, h * (2.6 if clk.get("style") == "analog" else 2.0) * clk.get("size", 1))
            self.draw_clock(cr, w - clock_w, 0, clock_w, h, cfg, False)
        blocks = layout(cfg, mode) if cfg.get("sensors", True) else []
        warn = data.warnings() if data else []
        combat = bool(data and data.in_combat)
        if cfg.get("warnings_only") or (combat and cfg.get("combat") == "warnings"):
            self.draw_warnings(cr, pad, h / 2, w - clock_w - 2 * pad, h * 0.16, warn)
            return
        alpha = 0.25 if (combat and cfg.get("combat") == "dim") else 1.0
        # blocks left to right by width weight; a block's rows share its height, a row's tiles its width
        unit = (w - clock_w) / max(1, sum(b["w"] for b in blocks))
        self.uniform = {}                     # same headline/subtitle size for all tiles of one row height
        cr.select_font_face(self.f_text)
        for b in blocks:
            rh = h / len(b["rows"])
            p = pad * cfg.get("tile_gap", 1.0) / (1 + 0.5 * (len(b["rows"]) - 1))
            for row in b["rows"]:
                tw = unit * b["w"] / len(row) - 2 * p
                th = rh - 2 * p
                fh = min(th, tw * 2.0)
                for k in row:
                    if TILES[k][4] == "events":
                        continue
                    # shrink together, but not below ~85 % (titles) / 80 % (subtitles): longer ones get "…"
                    t = fit_text(cr, TILES[k][0], tw, fh * 0.1, max(MIN_FONT, fh * 0.085))[1]
                    sub = (data.text.get(k, ("", ""))[1] if TILES[k][4] in ("text", "light") else
                           data.subs.get(k, "")) if data else ""
                    sz = fit_text(cr, sub, tw, fh * 0.085, max(MIN_FONT, fh * 0.068))[1] if sub else fh * 0.085
                    key = round(th)
                    old = self.uniform.get(key, (t, sz))
                    self.uniform[key] = (min(old[0], t), min(old[1], sz))
        x = 0
        for b in blocks:
            bw = unit * b["w"]
            rh = h / len(b["rows"])
            for ri, row in enumerate(b["rows"]):
                tw = bw / len(row)
                p = pad * cfg.get("tile_gap", 1.0) / (1 + 0.5 * (len(b["rows"]) - 1))   # less in stacked rows
                for ti, k in enumerate(row):
                    tx, ty = x + ti * tw, ri * rh
                    cr.push_group()
                    cr.rectangle(tx + p, ty + p, tw - 2 * p, rh - 2 * p)
                    cr.clip()
                    self.tile(cr, tx + p, ty + p, tw - 2 * p, rh - 2 * p, k, cfg, data)
                    cr.pop_group_to_source()
                    cr.paint_with_alpha(alpha)
            x += bw
        if warn and alpha < 1:                                    # warnings stay visible in combat, on top
            self.draw_warnings(cr, pad, h / 2, w - clock_w - 2 * pad, h * 0.11, warn)

    def draw_warnings(self, cr, x, y, width, size, warn):
        """Warnings in a dark pill with a drawn warning triangle (fonts here have no ⚠ glyph)."""
        if not warn:
            return
        orange = (0.98, 0.45, 0.35)
        txt = "   ·   ".join(warn)
        cr.select_font_face(self.f_text)
        txt, size = fit_text(cr, txt, width - size * 2.2, size, MIN_FONT)
        cr.set_font_size(size)
        tw = cr.text_extents(txt).x_advance
        ph = size * 1.7
        rounded(cr, x, y - ph / 2, tw + size * 2.4, ph, ph * 0.3)
        cr.set_source_rgba(0.06, 0.03, 0.03, 0.88)
        cr.fill()
        tx, ts = x + size * 0.45, size * 0.95                       # triangle
        cr.move_to(tx, y + ts * 0.42)
        cr.line_to(tx + ts / 2, y - ts * 0.48)
        cr.line_to(tx + ts, y + ts * 0.42)
        cr.close_path()
        cr.set_source_rgb(*orange)
        cr.fill()
        cr.set_source_rgb(0.06, 0.03, 0.03)
        cr.rectangle(tx + ts * 0.46, y - ts * 0.18, ts * 0.08, ts * 0.34)
        cr.rectangle(tx + ts * 0.46, y + ts * 0.22, ts * 0.08, ts * 0.08)
        cr.fill()
        cr.set_source_rgb(*orange)
        cr.move_to(x + size * 1.8, y + size * 0.36)
        cr.show_text(txt)

    # ---------- text that always fits ----------
    def text(self, cr, txt, x, base, width, size, color, face=None, min_size=None, align="left"):
        """Draw txt so it fits width: shrink down to min_size (default: a readable minimum), then
        shorten with "…". -> x where the text ends."""
        cr.select_font_face(face or self.f_text)
        txt, size = fit_text(cr, txt, width, size, min_size if min_size is not None else min(size, MIN_FONT))
        cr.set_font_size(size)
        adv = cr.text_extents(txt).x_advance
        cr.set_source_rgb(*color)
        cr.move_to(x + (width - adv if align == "right" else (width - adv) / 2 if align == "center" else 0), base)
        cr.show_text(txt)
        return x + adv

    def number_line(self, cr, x, base, width, size, parts):
        """A row of (text, face, relative size, colour) drawn side by side, scaled down together (never
        shortened) so the whole value with its unit fits width."""
        def widths(sz):
            out = []
            for txt, face, rel, _c in parts:
                cr.select_font_face(face)
                cr.set_font_size(sz * rel)
                out.append(cr.text_extents(txt).x_advance)
            return out
        gap = size * 0.08
        total = sum(widths(size)) + gap * (len(parts) - 1)
        if total > width:
            size *= max(0.3, width / total)
            gap = size * 0.08
        cx = x
        for (txt, face, rel, c), wd in zip(parts, widths(size)):
            cr.select_font_face(face)
            cr.set_font_size(size * rel)
            cr.set_source_rgb(*c)
            cr.move_to(cx, base)
            cr.show_text(txt)
            cx += wd + gap

    def tile(self, cr, x, y, w, h, key, cfg, data):
        title, unit, gmax, group, kind = TILES[key]
        # font sizes follow the tile height, but never more than its width allows (tall, narrow tiles)
        fh = min(h, w * 2.0)                  # only extreme tall-and-narrow tiles get capped; lines fit themselves
        col = color(cfg, key) or color(cfg, group) or (0.8, 0.8, 0.8)
        num = color(cfg, "number") or (0.86, 0.87, 0.89)
        dim = color(cfg, "dim") or (0.45, 0.47, 0.5)
        tsz, ssz = getattr(self, "uniform", {}).get(round(h), (fh * 0.1, fh * 0.085))
        self.text(cr, title, x, y + fh * 0.1, w, tsz, col)
        if kind == "events":
            self.event_tile(cr, x, y + fh * 0.16, w, h - fh * 0.16, cfg, data, 1)
            return
        if data is None:
            return
        if kind == "squad":
            self.squad_tile(cr, x, y + fh * 0.16, w, h - fh * 0.16, fh, ssz, cfg, data)
            return
        if kind in ("wvw", "wvwmap"):
            data.wvw.touch(cfg.get("api_key", ""))
            draw = self.wvw_tile if kind == "wvw" else self.wvwmap_tile
            draw(cr, x, y + fh * 0.16, w, h - fh * 0.16, fh, ssz, cfg, data)
            return
        if kind in ("text", "light"):
            big, small = data.text.get(key) or ("-", "")
            bx = x
            if kind == "light":
                cr.set_source_rgb(*LIGHT.get(big, LIGHT["-"]))
                cr.arc(x + fh * 0.12, y + h * 0.42, fh * 0.1, 0, 2 * math.pi)
                cr.fill()
                bx = x + fh * 0.28
            bcol = BOTTLENECK_COL.get(big, num) if key == "bottleneck" else num
            self.text(cr, big, bx, y + h * 0.48, w - (bx - x), fh * 0.2, bcol)
            self.text(cr, small, x, y + h * 0.66, w, ssz, dim)
            return
        opts = cfg.get("tile_opts", {}).get(key, {})
        show_num, show_graph = opts.get("number", True), opts.get("graph", True)
        if not show_num and not show_graph:
            return
        cur = data.vals.get(key)
        val = "-" if cur is None else (f"{cur:.1f}" if unit in ("W", "GiB") else f"{cur:.0f}")
        parts = [(val, self.f_num, 1.0, num)]
        if key == "fps" and opts.get("avg"):        # "58 | 55 avg"
            av = data.fps_avg
            parts += [("|", self.f_num, 1.0, dim), ("-" if av is None else f"{av:.0f}", self.f_num, 1.0, col),
                      ("avg", self.f_text, 0.42, dim)]
        elif unit:
            parts.append((unit, self.f_num, 0.42, dim))
        sub = data.subs.get(key, "")
        if not show_graph:                    # number only: bigger, in the middle of the tile
            self.number_line(cr, x, y + h * 0.62, w, fh * 0.42, parts)
            self.text(cr, sub, x, y + h * 0.82, w, ssz, dim)
            return
        if not show_num:                      # graph only: graph fills the tile, value small at the top right
            self.graph(cr, x, y + h * 0.18, w, h * 0.82, key, gmax, col, dim, cfg, data)
            if cur is not None:
                tw = w * 0.45
                self.text(cr, f"{val} {unit}".strip(), x + w - tw, y + fh * 0.1, tw, fh * 0.09, num,
                          face=self.f_num, align="right")
            return
        self.number_line(cr, x, y + h * 0.38, w, fh * 0.26, parts)
        self.text(cr, sub, x, y + h * 0.5, w, ssz, dim)
        self.graph(cr, x, y + h * 0.58, w, h * 0.42, key, gmax, col, dim, cfg, data)

    def squad_tile(self, cr, x, y, w, h, fh, ssz, cfg, data):
        """Squad or party from the Nexus bridge: size, commander, your subgroup and the profession mix of
        everyone on your map instance as a colour bar."""
        dim = color(cfg, "dim") or (0.45, 0.47, 0.5)
        num = color(cfg, "number") or (0.86, 0.87, 0.89)
        b = data.bridge
        if not b:
            known = data.bridge_file and os.path.isdir(os.path.dirname(data.bridge_file))   # made by the addon
            self.text(cr, "waiting for GW2" if known else "needs the letterbox bridge addon in Nexus", x,
                      y + h * 0.4, w, ssz, dim)
            return
        if not b.get("rtapi"):
            self.text(cr, "install RealTime API from the Nexus library", x, y + h * 0.4, w, ssz, dim)
            return
        members = b.get("members", [])
        kind = {1: "Party", 2: "Squad", 3: "Squad"}.get(b.get("group_type"), "Solo")
        me = next((m for m in members if m.get("self")), {})
        here = [m for m in members if m.get("here") or m.get("self")]
        size = min(fh * 0.2, h * 0.28)
        big = f"{kind} {b.get('group_count') or len(members)}" if kind != "Solo" else "Solo"
        end = self.text(cr, big, x, y + size, w * 0.5, size, num)
        cmd = next((m for m in members if m.get("commander")), None)
        if cmd:
            self.text(cr, f"tag {cmd.get('name') or cmd.get('account')}", end + size * 0.4, y + size, x + w - end
                      - size * 0.4, ssz * 1.1, (0.4, 0.75, 1.0))
        sub = []
        if kind == "Squad" and me.get("sub"):
            sub.append(f"subgroup {me['sub']}")
        if members:
            sub.append(f"{len(here)} on your map")
        self.text(cr, " · ".join(sub) or "not in a group", x, y + size + ssz * 1.5, w, ssz, dim)
        if not here:
            return
        # profession mix, one segment per profession, as wide as its share
        counts = {}
        for m in here:
            counts[m.get("prof", 0)] = counts.get(m.get("prof", 0), 0) + 1
        by, bh, bx = y + size + ssz * 2.3, min(h * 0.24, fh * 0.2), x
        for prof in sorted(counts, key=lambda p: -counts[p]):
            name, col = PROFESSIONS.get(prof, ("?", (0.4, 0.4, 0.4)))
            sw = w * counts[prof] / len(here)
            cr.set_source_rgb(*col)
            cr.rectangle(bx, by, max(1, sw - 2), bh)
            cr.fill()
            label = f"{counts[prof]} {name[:3]}"
            if sw > ssz * 3.2:
                self.text(cr, label, bx + 4, by + bh * 0.68, sw - 8, min(bh * 0.6, ssz * 1.1), (0.06, 0.06, 0.06))
            bx += sw
        # your subgroup's professions
        if kind == "Squad" and me.get("sub"):
            mates = {}
            for m in members:
                if m.get("sub") == me["sub"]:
                    n = PROFESSIONS.get(m.get("prof", 0), ("?", 0))[0]
                    mates[n] = mates.get(n, 0) + 1
            txt = " · ".join(f"{k} {n[:3]}" for n, k in sorted(mates.items(), key=lambda z: -z[1]))
            self.text(cr, f"your subgroup ({sum(mates.values())}): {txt}", x, by + bh + ssz * 1.4, w, ssz, dim)

    def wvw_tile(self, cr, x, y, w, h, fh, ssz, cfg, data):
        """Teams by victory points: name, bar of the current skirmish's score, VP. Yours is marked."""
        wv, dim = data.wvw, color(cfg, "dim") or (0.45, 0.47, 0.5)
        num = color(cfg, "number") or (0.86, 0.87, 0.89)
        m = wv.match
        if not m:
            self.text(cr, wv.err, x, y + h * 0.4, w, ssz, dim)
            return
        mine = wv.mine(f((data.mumble or {}).get("team_color_id")))
        vp, sk = m["victory_points"], m["skirmishes"][-1]["scores"]
        order = sorted(vp, key=lambda c: -vp[c])
        top = max(1, max(sk.values()))
        rows_h = h - ssz * 2.0
        rh = rows_h / 3
        nw, vw = w * 0.3, w * 0.2
        size = min(rh * 0.5, fh * 0.13)
        for i, c in enumerate(order):
            ry, col = y + i * rh, WVW_COLORS[c]
            self.text(cr, f"{i + 1}. {c.title()}" + (" · you" if c == mine else ""), x, ry + rh * 0.5 + size * 0.36,
                      nw - 6, size, col)
            bx, bw = x + nw, (w - nw - vw) * sk[c] / top
            cr.set_source_rgba(*col, 0.95 if c == mine else 0.6)
            cr.rectangle(bx, ry + rh * 0.2, max(2, bw), rh * 0.6)
            cr.fill()
            if bw > size * 2.5:
                self.text(cr, str(sk[c]), bx + 4, ry + rh * 0.5 + size * 0.3, bw - 8, size * 0.8, (0.05, 0.05, 0.05),
                          face=self.f_num)
            self.text(cr, f"{vp[c]} VP", x + w - vw, ry + rh * 0.5 + size * 0.36, vw, size, num, face=self.f_num,
                      align="right")
        sm = wv.skirmish()
        ppt = wv.ppt()
        line = (f"skirmish {sm[0]}/{sm[1]} · {int(sm[2] // 3600)}:{int(sm[2] % 3600 // 60):02d} left · " if sm else "")
        line += "PPT " + " ".join(f"{c[0].upper()} {ppt[c]}" for c in order)
        self.text(cr, line, x, y + rows_h + ssz * 1.4, w, ssz, dim)

    def wvwmap_tile(self, cr, x, y, w, h, fh, ssz, cfg, data):
        """The WvW map you're on (else Eternal Battlegrounds): who holds what, and the latest captures."""
        wv, dim = data.wvw, color(cfg, "dim") or (0.45, 0.47, 0.5)
        num = color(cfg, "number") or (0.86, 0.87, 0.89)
        m = wv.match
        if not m:
            self.text(cr, wv.err, x, y + h * 0.4, w, ssz, dim)
            return
        g = data.mumble or {}
        try:
            here = WVW_MAPS.get(int(g.get("map_id") or 0))
        except ValueError:
            here = None
        mtype = here or "Center"
        mp = next((x_ for x_ in m["maps"] if x_["type"] == mtype), None)
        if not mp:
            return
        size = min(fh * 0.15, h * 0.16)
        self.text(cr, WVW_MAP_NAMES[mtype] + ("" if here else "  (you're not in WvW)"), x, y + size, w, size, num)
        # ownership: one segment per objective, as wide as the points it gives per tick
        objs = [o for o in mp["objectives"] if o.get("points_tick")]
        rank = {"red": 0, "green": 1, "blue": 2}
        objs.sort(key=lambda o: (rank.get(o["owner"].lower(), 3), -o["points_tick"]))
        total = sum(o["points_tick"] for o in objs) or 1
        by, bh, bx = y + size * 1.5, h * 0.14, x
        for o in objs:
            ow = w * o["points_tick"] / total
            cr.set_source_rgb(*WVW_COLORS.get(o["owner"].lower(), (0.4, 0.4, 0.4)))
            cr.rectangle(bx, by, max(1, ow - 1.5), bh)
            cr.fill()
            bx += ow
        counts = {}
        for o in mp["objectives"]:
            if o["type"] in ("Camp", "Tower", "Keep", "Castle"):
                counts.setdefault(o["owner"].lower(), {}).setdefault(o["type"], 0)
                counts[o["owner"].lower()][o["type"]] += 1
        cx = x
        for c in ("red", "green", "blue"):
            k = counts.get(c, {})
            txt = " ".join((f"{k[t]} castle" if t == "Castle" else f"{k[t]}{t[0]}")
                           for t in ("Castle", "Keep", "Tower", "Camp") if k.get(t))
            cx = self.text(cr, txt or "-", cx, by + bh + ssz * 1.3, w / 3 - 8, ssz, WVW_COLORS[c]) + 0
            cx = x + w / 3 * (("red", "green", "blue").index(c) + 1)
        # latest captures on this map, newest first
        now = datetime.now(timezone.utc)
        flips = []
        for o in mp["objectives"]:
            if o.get("last_flipped") and o["type"] in ("Camp", "Tower", "Keep", "Castle"):
                t = datetime.fromisoformat(o["last_flipped"].replace("Z", "+00:00"))
                if (now - t).total_seconds() < 1800:
                    flips.append((t, o))
        flips.sort(key=lambda z: z[0], reverse=True)
        ly, lh = by + bh + ssz * 2.0, max(MIN_FONT, ssz) * 1.45
        for t, o in flips:
            if ly + lh > y + h:
                break
            col = WVW_COLORS.get(o["owner"].lower(), dim)
            cr.set_source_rgb(*col)
            cr.arc(x + lh * 0.25, ly + lh * 0.55, lh * 0.18, 0, 2 * math.pi)
            cr.fill()
            mins = int((now - t).total_seconds() // 60)
            self.text(cr, f"{wv.names.get(o['id'], o['type'])} · {mins} min", x + lh * 0.6, ly + lh * 0.75,
                      w - lh * 0.6, max(MIN_FONT, ssz), num if mins < 5 else dim)
            ly += lh
        if not flips:
            self.text(cr, "no captures in the last 30 min", x, ly + lh * 0.75, w, ssz, dim)

    def graph(self, cr, x, gy, w, gh, key, gmax, col, dim, cfg, data):
        h = gh / 0.42
        n = data.graph_seconds
        hist = list(data.hist.get(key, []))[-n:]
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.rectangle(x, gy, w, gh)
        cr.fill()
        if key == "budget":
            e = f((data.probe or {}).get("smc_PZ0E"))
            if e:
                cr.set_source_rgb(*dim)
                cr.set_line_width(max(1, h * 0.006))
                ly = gy + gh - min(e / gmax, 1) * gh
                cr.move_to(x, ly)
                cr.line_to(x + w, ly)
                cr.stroke()
        series = [(hist, col, None)]
        if key == "fps" and cfg.get("tile_opts", {}).get("fps", {}).get("avg"):
            series.append((list(data.hist.get("fps_avg", []))[-n:], tuple(c * 0.65 for c in col), [6, 4]))
        for hist, scol, dash in series:
            pts = [(i, v) for i, v in enumerate(hist) if v is not None]
            if len(pts) < 2:
                continue
            step = w / max(1, n - 1)
            off = n - len(hist)
            cr.set_source_rgb(*scol)
            cr.set_line_width(max(1.5, h * 0.012))
            cr.set_dash(dash or [])
            for j, (i, v) in enumerate(pts):
                (cr.move_to if j == 0 else cr.line_to)(x + (i + off) * step,
                                                       gy + gh - min(max(v / gmax, 0), 1) * gh)
            cr.stroke()
            cr.set_dash([])

    def event_sections(self, cfg, data, names):
        secs = [sct for sct in EVENTS if sct["name"] in names]
        if cfg.get("event_auto_map", True) and data and data.mumble:
            m = (data.mumble.get("map_name") or "").strip()
            secs += [sct for sct in EVENTS if sct["name"] == m and sct not in secs]
        return secs

    def event_tile(self, cr, x, y, w, h, cfg, data, k):
        """Timeline like the wiki's event timer: one row per section, 10 min back to event_ahead min ahead."""
        secs = self.event_sections(cfg, data, cfg.get("event_sections", []))
        dim = color(cfg, "dim") or (0.45, 0.47, 0.5)
        if not secs:
            cr.set_source_rgb(*dim)
            cr.set_font_size(h * 0.1)
            cr.move_to(x, y + h * 0.3)
            cr.show_text("no event rows chosen (settings → Events)")
            return
        now = datetime.now(timezone.utc)
        ahead = cfg.get("event_ahead", 110)
        t0, t1 = now - timedelta(minutes=10), now + timedelta(minutes=ahead)
        span = (t1 - t0).total_seconds()
        cap = cfg.get("event_row_max", 0) or 1e9  # px; few rows stay slim instead of filling the tile
        gap = min(0.8, max(0.0, cfg.get("event_row_gap", 0.16)))
        cg = max(0, cfg.get("event_col_gap", 2)) / 2
        wts = [max(0.1, float(cfg.get("event_row_heights", {}).get(s["name"], 1))) for s in secs]
        thin = min(cap, (h * 0.86) / sum(wts)) * max(wts) * (1 - gap) < MIN_LABEL_ROW * 0.84
        names_w = 0 if thin else min(w * 0.2, h * 1.3)   # name column on the left (gone with thin rows)
        lx, x, w = x, x + names_w, w - names_w
        X = lambda t: x + (t - t0).total_seconds() / span * w
        label_h = h * 0.14
        unit = min(cap, (h - label_h) / sum(wts))  # height of a factor-1 row; squeezed if there are many
        cr.select_font_face(self.f_text)
        ry = y + label_h
        for sct, wt in zip(secs, wts):
            rh = unit * wt
            labels = rh * (1 - gap) >= MIN_LABEL_ROW * 0.84     # thinner rows: bars without text
            if labels and names_w:
                cr.save()
                cr.rectangle(lx, ry, names_w - 6, rh)
                cr.clip()
                cr.set_source_rgb(*dim)
                label, size = fit_text(cr, sct["name"], names_w - 10, min(rh * 0.45, h * 0.1), MIN_FONT)
                cr.set_font_size(size)
                cr.move_to(lx, ry + rh / 2 + size * 0.36)
                cr.show_text(label)
                cr.restore()
            for a, b, seg in gw2events.window(sct, t0, t1):
                bx0, bx1 = max(x, X(a)), min(x + w, X(b))
                r, g, bb = (c / 255 for c in seg.get("bg", [128, 128, 128]))
                active = a <= now < b
                cr.set_source_rgba(r * 0.55, g * 0.55, bb * 0.55, 1 if active else 0.75)
                cr.rectangle(bx0 + cg, ry + rh * gap / 2, max(0, bx1 - bx0 - 2 * cg), rh * (1 - gap))
                cr.fill()
                if not labels:
                    continue
                cr.save()
                cr.rectangle(bx0 + cg, ry, max(0, bx1 - bx0 - 2 * cg - 2), rh)
                cr.clip()
                cr.set_source_rgb(1, 1, 1)
                label, size = fit_text(cr, seg["name"], bx1 - bx0 - 10, min(rh * (1 - gap) * 0.6, h * 0.12),
                                       MIN_FONT)
                cr.set_font_size(size)
                cr.move_to(bx0 + 5, ry + rh / 2 + size * 0.36)            # vertically centred
                cr.show_text(label)
                cr.restore()
            ry += rh
        # time ticks (closer for short windows, wider for long ones) and the "now" marker
        step = 15 if ahead <= 60 else 30 if ahead <= 180 else 60
        bottom = ry
        cr.set_font_size(label_h * 0.75)
        tick = (t0 + timedelta(minutes=step - t0.minute % step)).replace(second=0, microsecond=0)
        while tick < t1:
            tx = X(tick)
            lbl = tick.astimezone().strftime("%H:%M")
            if tx + 3 + cr.text_extents(lbl).x_advance <= x + w:   # no half labels at the right edge
                cr.set_source_rgba(*dim, 0.8)
                cr.move_to(tx + 3, y + label_h * 0.8)
                cr.show_text(lbl)
            cr.set_source_rgba(1, 1, 1, 0.15)
            cr.set_line_width(1)
            cr.move_to(tx, y + label_h)
            cr.line_to(tx, bottom)
            cr.stroke()
            tick += timedelta(minutes=step)
        cr.set_source_rgb(0.95, 0.3, 0.25)
        cr.set_line_width(max(2, h * 0.012))
        cr.move_to(X(now), y)
        cr.line_to(X(now), bottom)
        cr.stroke()

    def draw_clock(self, cr, x, y, w, h, cfg, big):
        """Digital / analog / minimal clock plus the info lines (start warning, Tyria time, next event,
        resets). Every line is fitted to the column width; the analog face sits left of its lines."""
        c = cfg["clock"]
        col = color(cfg, "clock") or (0.45, 0.47, 0.5)
        dim = color(cfg, "dim") or (0.45, 0.47, 0.5)
        now, utc = datetime.now(), datetime.now(timezone.utc)
        style = c.get("style", "digital")
        s = c.get("size", 1.0)
        lines = []
        if c.get("tyria", True):
            ph, left = tyria(utc)
            nxt = {"day": "dusk", "dusk": "night", "night": "dawn", "dawn": "day"}[ph]
            lines.append(f"Tyria: {ph} · {nxt} in {int(left) + 1} min")
        if c.get("events", True):
            up = [e for e in gw2events.upcoming(self.event_sections(cfg, None, cfg.get("clock_events", [])), utc, 120)
                  if e[0] > utc]
            if up:
                a, _b, _sec, seg = up[0]
                lines.append(f"next: {seg['name']} {a.astimezone():%H:%M} (in {int((a - utc).total_seconds() // 60) + 1} min)")
        if c.get("resets", True):
            d, wk = resets(utc)
            lines.append(f"daily reset {int(d // 60)}:{int(d % 60):02d} · weekly {int(wk // 1440)}d {int(wk // 60 % 24)}h")
        if style == "minimal":
            lines = []
        al = self.alert(cfg, None)
        alert = None
        if al:
            when = "now" if al[1] <= 0 else f"in {int(al[1] // 60)}:{int(al[1] % 60):02d}"
            blink = cfg.get("alert", {}).get("blink", True)
            pulse = 0.55 + 0.45 * abs(math.sin(time.time() * 3)) if blink and 0 < al[1] <= 60 else 1.0
            alert = (f"» {al[0]} {when}", rgb(cfg.get("alert", {}).get("color", "#ff8a3d")), pulse)
        k = 1.35 if big else 1.0                  # clock-only mode: everything larger
        line_size = h * 0.068 * k
        n_lines = len(lines) + (1 if alert else 0)

        def info(x0, width, top, align):
            ty = top
            if alert:
                txt, ac, pulse = alert
                cr.push_group()
                self.text(cr, txt, x0, ty + line_size * 1.15, width, line_size * 1.25, ac, align=align)
                cr.pop_group_to_source()
                cr.paint_with_alpha(pulse)
                ty += line_size * 1.6
            for ln in lines:
                self.text(cr, ln, x0, ty + line_size, width, line_size, dim, align=align)
                ty += line_size * 1.4

        info_h = (line_size * 1.6 if alert else 0) + len(lines) * line_size * 1.4
        if style == "analog":
            r = min(h * 0.36, (w * (0.22 if n_lines else 0.45))) * min(s, 1.4)
            cx, cy = (x + w * 0.04 + r) if n_lines else (x + w / 2), y + h / 2
            cr.set_source_rgb(*col)
            cr.set_line_width(max(1.5, r * 0.04))
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.stroke()
            for i in range(12):
                a = i / 12 * 2 * math.pi
                cr.move_to(cx + math.sin(a) * r * 0.84, cy - math.cos(a) * r * 0.84)
                cr.line_to(cx + math.sin(a) * r, cy - math.cos(a) * r)
            cr.stroke()
            hands = [((now.hour % 12 + now.minute / 60) / 12, 0.5, 0.075),
                     ((now.minute + now.second / 60) / 60, 0.78, 0.05)]
            if c.get("seconds"):
                hands.append((now.second / 60, 0.9, 0.02))
            for frac, ln, wd in hands:
                a = frac * 2 * math.pi
                cr.set_line_width(max(1, r * wd))
                cr.set_line_cap(1)                  # round
                cr.move_to(cx, cy)
                cr.line_to(cx + math.sin(a) * r * ln, cy - math.cos(a) * r * ln)
                cr.stroke()
            if n_lines:
                ix = cx + r + w * 0.05
                info(ix, x + w - ix, y + (h - info_h) / 2, "left")
            return
        fmtc = ("%H:%M" if c.get("h24", True) else "%I:%M") + (":%S" if c.get("seconds") else "")
        txt = now.strftime(fmtc) + ("" if c.get("h24", True) else now.strftime(" %p"))
        tsize = h * (0.13 if style == "minimal" else 0.26) * s * (1.6 if big else 1)
        date = now.strftime("%a %d.%m.") if c.get("date") else None
        block = tsize * 0.85 + (line_size * 1.5 if date else 0) + (line_size * 0.6 + info_h if n_lines else 0)
        top = y + max(h * 0.06, (h - block) / 2)
        cr.select_font_face(self.f_num)
        self.text(cr, txt, x, top + tsize * 0.85, w, tsize, col, face=self.f_num, align="center")
        ty = top + tsize * 0.85
        if date:
            self.text(cr, date, x, ty + line_size * 1.4, w, line_size * 1.1, dim, align="center")
            ty += line_size * 1.5
        if n_lines:
            info(x + w * 0.02, w * 0.96, ty + line_size * 0.6, "center")


class Bar(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="letterbox bar")
        self.set_decorated(False)
        self.cfg = load_config()
        self.mode = self.cfg.get("mode", "full")
        self.data = Data(self.cfg.get("graph_seconds", 60), self.cfg)
        self.renderer = Renderer()
        area_env = os.environ.get("LETTERBOX_AMBIENT_AREA")
        self.ambient = Ambient(tuple(int(float(x)) for x in area_env.split()) if area_env else None)
        self.amb_cols = None
        self.last_frame = time.monotonic()
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(lambda a, cr, w, h: self.renderer.draw(cr, w, h, self.cfg, self.data, self.mode,
                                                                        self.amb_cols))
        self.set_child(self.area)
        css = Gtk.CssProvider()
        css.load_from_string("window { background: black; }")
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        GLib.timeout_add(200, self.data.sample_gpu)
        GLib.timeout_add_seconds(1, self.tick)
        GLib.timeout_add(int(1000 / max(1, self.cfg.get("ambient_rate", 4))), self.grab)
        GLib.timeout_add(250, self.animate)
        self.tick()

    def tick(self):
        self.data.update()
        return True

    def grab(self):
        if self.cfg.get("background") == "ambient" and self.mode != "off":
            self.ambient.grab()
        return True

    def animate(self):
        now = time.monotonic()
        self.amb_cols = self.ambient.step(now - self.last_frame)
        self.last_frame = now
        self.area.queue_draw()
        return True

    def cycle(self):
        i = MODES.index(self.mode) if self.mode in MODES else 0
        self.mode = MODES[(i + 1) % len(MODES)]
        self.area.queue_draw()

    def reload(self):
        self.cfg = load_config()
        self.data.graph_seconds = self.cfg.get("graph_seconds", 60)
        self.area.queue_draw()


# D-Bus letterbox.Bar on /letterbox/bar: Cycle/Toggle (mode), Mark (lag marker: time to MARKERS, sound,
# red flash), Flash (only the flash), Reload (config changed)
IFACE = Gio.DBusNodeInfo.new_for_xml(
    "<node><interface name='letterbox.Bar'><method name='Toggle'/><method name='Cycle'/><method name='Mark'/>"
    "<method name='Flash'/><method name='Reload'/></interface></node>").interfaces[0]


def mark():
    os.makedirs(os.path.dirname(MARKERS), exist_ok=True)
    with open(MARKERS, "a") as fh:
        fh.write(datetime.now().isoformat(timespec="milliseconds") + "\n")
    subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/camera-shutter.oga"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    if sys.argv[1:] == ["--screen"]:                       # for the letterbox script
        sc = screen()
        if not sc:
            return 1
        print(sc["name"], sc["w"], sc["h"], sc["x"], sc["y"], sc["scale"])
        return 0
    app = Gtk.Application(application_id="letterbox.bar")

    def activate(a):
        bar = Bar(a)
        bar.present()

        def call(_conn, _sender, _path, _iface, method, _params, inv):
            if method in ("Cycle", "Toggle"):
                bar.cycle()
            elif method in ("Mark", "Flash"):
                if method == "Mark":
                    mark()
                bar.renderer.flash_until = time.time() + 0.8
            elif method == "Reload":
                bar.reload()
            inv.return_value(None)
        a.get_dbus_connection().register_object("/letterbox/bar", IFACE, call, None, None)
    app.connect("activate", activate)
    GLib.set_prgname("letterbox-bar")
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
