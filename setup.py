#!/usr/bin/env python3
# setup.py: the parts of install/uninstall that need more than shell.
#   setup.py doctor              what letterbox found on this machine and what's missing
#   setup.py launch add|remove   put "letterbox run" into GW2's Steam launch options (Steam must be closed)
#   setup.py mangohud            make MangoHud log GW2's frames for the FPS tiles
#   setup.py gfx                 find GW2's graphics settings outside Steam and remember the path
#   setup.py bridge              put the letterbox bridge addon into GW2's addons folder (needs Nexus)
import glob, json, os, re, shutil, subprocess, sys, time

HOME = os.path.expanduser("~")
DIR = os.path.dirname(os.path.abspath(__file__))
CONF = f"{HOME}/.config/letterbox/config.json"
APPID = "1284210"
RUN = f"{HOME}/.local/share/letterbox/letterbox run"
MANGO_DIR = f"{HOME}/.config/MangoHud"
LOG_DIR = f"{HOME}/.local/share/letterbox/mangohud"
STEAM_ROOTS = [f"{HOME}/.local/share/Steam", f"{HOME}/.steam/steam"]


def load_conf():
    try:
        return json.load(open(CONF))
    except (OSError, ValueError):
        return {}


def save_conf(c):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    json.dump(c, open(CONF, "w"), indent=1)


def steam_running():
    return subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0


# ---------- Steam launch options ----------
def vdf_tokens(text):
    """Tokens of a Valve KeyValues text file as (kind, value, start, end): kind is "str", "{" or "}"."""
    i, n, out = 0, len(text), []
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif text.startswith("//", i):
            i = text.find("\n", i) if "\n" in text[i:] else n
        elif c in "{}":
            out.append((c, c, i, i + 1))
            i += 1
        elif c == '"':
            j, val = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    val.append(text[j + 1])
                    j += 2
                else:
                    val.append(text[j])
                    j += 1
            out.append(("str", "".join(val), i, j + 1))
            i = j + 1
        else:                                       # unquoted token
            j = i
            while j < n and not text[j].isspace() and text[j] not in '{}"':
                j += 1
            out.append(("str", text[i:j], i, j))
            i = j
    return out


def vdf_find(tokens, path):
    """Index range (open brace, close brace) of the block at path (keys case-insensitive), or None."""
    lo, hi = 0, len(tokens)
    for key in path:
        i, depth, found = lo, 0, None
        while i < hi:
            kind = tokens[i][0]
            if kind == "{":
                depth += 1
            elif kind == "}":
                depth -= 1
            elif depth == 0 and tokens[i][1].lower() == key.lower() and i + 1 < hi and tokens[i + 1][0] == "{":
                found = i + 1
                break
            i += 1
        if found is None:
            return None
        depth, j = 0, found
        while j < hi:
            if tokens[j][0] == "{":
                depth += 1
            elif tokens[j][0] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        lo, hi = found + 1, j
    return lo - 1, hi


def vdf_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def with_letterbox(opts):
    """Launch options with "letterbox run" in front of the command, after leading VAR=value parts."""
    opts = opts.strip()
    if "letterbox run" in opts:
        return opts
    if "%command%" not in opts:                     # only game arguments
        return f"{RUN} %command% {opts}".strip()
    parts = opts.split(" ")
    k = 0
    while k < len(parts) and (parts[k] == "" or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[k])):
        k += 1
    return " ".join(p for p in parts[:k] + [RUN] + parts[k:] if p != "")


def without_letterbox(opts):
    opts = re.sub(r"\S*letterbox run\s+", "", opts).strip()
    return "" if opts == "%command%" else opts


def launch(action):
    files = []
    for root in STEAM_ROOTS:
        files += glob.glob(f"{root}/userdata/*/config/localconfig.vdf")
    files = sorted({os.path.realpath(f) for f in files})
    if not files:
        print("no Steam user config found (Steam not installed, or the Flatpak version, which can't run")
        print(f"letterbox from your home folder). Set the launch option by hand:  {RUN} %command%")
        return 1
    if steam_running():
        print("Steam is running and would undo the change when it exits.")
        return 2
    done = 0
    for f in files:
        text = open(f, encoding="utf-8", errors="surrogateescape").read()
        tok = vdf_tokens(text)
        rng = vdf_find(tok, ["UserLocalConfigStore", "Software", "Valve", "Steam", "apps", APPID])
        if not rng:
            continue                                # GW2 never started with this Steam account
        a, b = rng
        cur = next(((i, tok[i + 1]) for i in range(a + 1, b - 1)
                    if tok[i][0] == "str" and tok[i][1] == "LaunchOptions" and tok[i + 1][0] == "str"), None)
        old = cur[1][1] if cur else ""
        new = with_letterbox(old) if action == "add" else without_letterbox(old)
        if new == old:
            print(f"launch options already {'set' if action == 'add' else 'clean'}: {old or '(empty)'}")
            done += 1
            continue
        if cur:
            _, (_, _, s, e) = cur
            text = text[:s] + vdf_quote(new) + text[e:]
        else:
            close = tok[b][2]
            line_start = text.rfind("\n", 0, close) + 1
            indent = text[line_start:close]
            text = text[:line_start] + f"{indent}\t\"LaunchOptions\"\t\t{vdf_quote(new)}\n" + text[line_start:]
        shutil.copy2(f, f + ".before-letterbox")
        with open(f, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(text)
        print(f"launch options: {old or '(empty)'}\n             -> {new or '(empty)'}")
        done += 1
    if not done:
        print("GW2 isn't in your Steam library config yet. Start it once through Steam, then run")
        print("  letterbox setup-launch")
        return 1
    return 0


# ---------- MangoHud ----------
def mangohud():
    """Logging for the FPS tiles. An existing GW2 config gets the log lines if it has none. Without one, a
    new GW2 config is made from the global config (MangoHud uses only one of them) or, without any
    config, one that only logs and shows nothing (the bar shows the FPS)."""
    if not shutil.which("mangohud"):
        print("MangoHud isn't installed: the FPS tiles stay empty (everything else works).")
        return 0
    c = load_conf()
    gw2, glob_ = f"{MANGO_DIR}/wine-Gw2-64.conf", f"{MANGO_DIR}/MangoHud.conf"
    for f in (gw2, glob_):
        if os.path.exists(f):
            m = re.findall(r"(?m)^\s*output_folder\s*=\s*(.+?)\s*$", open(f).read())
            if m and (f == gw2 or not os.path.exists(gw2)):
                c["mangohud_dir"] = m[-1]
                save_conf(c)
                print(f"FPS tiles: MangoHud already logs to {m[-1]} ({os.path.basename(f)})")
                return 0
    lines = f"\n# letterbox: frame log for the FPS tiles\noutput_folder={LOG_DIR}\nautostart_log=5\nlog_interval=0\n"
    os.makedirs(MANGO_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(gw2):
        shutil.copy2(gw2, gw2 + ".before-letterbox")
        open(gw2, "a").write(lines)
        print(f"FPS tiles: added logging to {gw2}")
    elif os.path.exists(glob_):
        open(gw2, "w").write(open(glob_).read() + lines)
        print(f"FPS tiles: made {gw2} from your MangoHud.conf, plus logging")
    else:
        open(gw2, "w").write("# letterbox: MangoHud only logs frames for the bar's FPS tiles, no overlay\n"
                             "no_display=1" + lines)
        print(f"FPS tiles: made {gw2} (logging only, no overlay)")
    c["mangohud_dir"] = LOG_DIR
    save_conf(c)
    return 0


# ---------- GW2 outside Steam ----------
def find_gfx():
    """GFXSettings.Gw2-64.exe.xml in the usual Wine prefixes of Lutris, Heroic, Bottles and plain Wine."""
    pats = [f"{HOME}/Games/*/drive_c/users/*/AppData/Roaming/Guild Wars 2/GFXSettings.Gw2-64.exe.xml",
            f"{HOME}/Games/*/*/drive_c/users/*/AppData/Roaming/Guild Wars 2/GFXSettings.Gw2-64.exe.xml",
            f"{HOME}/.wine*/drive_c/users/*/AppData/Roaming/Guild Wars 2/GFXSettings.Gw2-64.exe.xml",
            f"{HOME}/.local/share/bottles/bottles/*/drive_c/users/*/AppData/Roaming/Guild Wars 2/GFXSettings.Gw2-64.exe.xml",
            f"{HOME}/.var/app/*/data/*/drive_c/users/*/AppData/Roaming/Guild Wars 2/GFXSettings.Gw2-64.exe.xml"]
    hits = sorted({p for pat in pats for p in glob.glob(pat)}, key=os.path.getmtime, reverse=True)
    return hits[0] if hits else None


def gfx():
    if subprocess.run([f"{DIR}/letterbox", "gfx-path"], capture_output=True, text=True).stdout.strip():
        return 0
    p = find_gfx()
    if p:
        c = load_conf()
        c["gfx_settings"] = p
        save_conf(c)
        print(f"GW2 (non-Steam) graphics settings: {p}")
        return 0
    print("GW2's graphics settings weren't found. Start GW2 once, then run  letterbox doctor")
    return 1


# ---------- Nexus bridge addon ----------
BRIDGE_URL = "https://github.com/flo-coast/letterbox/releases/latest/download/letterbox_bridge.dll"


def game_dir():
    sys.path.insert(0, DIR)
    import bar
    cfg = bar.load_config()
    if cfg.get("game_dir"):
        return os.path.expanduser(cfg["game_dir"])
    for lib in bar.steam_libraries():
        d = f"{lib}/steamapps/common/Guild Wars 2"
        if os.path.exists(f"{d}/Gw2-64.exe"):
            return d
    p = cfg.get("bridge_file")
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.expanduser(p)))) if p else None


def bridge():
    """Downloads the bridge DLL from the latest release (or uses one built locally in nexus-bridge/)."""
    import urllib.request
    g = game_dir()
    if not g or not os.path.isdir(f"{g}/addons/Nexus"):
        print("Nexus isn't installed in GW2, so the bridge addon is skipped (the squad tile needs it).")
        return 1
    local = f"{DIR}/nexus-bridge/letterbox_bridge.dll"
    if os.path.exists(local):
        data = open(local, "rb").read()
    else:
        with urllib.request.urlopen(BRIDGE_URL, timeout=30) as r:
            data = r.read()
    if data[:2] != b"MZ":
        print("the download isn't a Windows DLL, skipped")
        return 1
    dst = f"{g}/addons/letterbox_bridge.dll"
    with open(dst + ".part", "wb") as fh:
        fh.write(data)
    os.replace(dst + ".part", dst)
    print(f"bridge addon: {dst}")
    print("  In GW2 open Nexus (the icon at the top left), load 'letterbox bridge' and install")
    print("  'RealTime API' from the Nexus library. The squad tile shows up in the settings under Tiles.")
    return 0


# ---------- doctor ----------
def doctor():
    sys.path.insert(0, DIR)
    ok = True

    def line(good, what, detail):
        nonlocal ok
        ok &= bool(good) or good is None
        mark = "ok " if good else ("-- " if good is None else "!! ")
        print(f"  {mark} {what:<17} {detail}")

    print("letterbox doctor")
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
        import cairo, numpy  # noqa: F401
        line(True, "Python modules", "GTK 4, cairo, NumPy")
    except Exception as ex:
        line(False, "Python modules", f"missing ({ex}), run ./install.sh")
        return 1
    import bar
    desk = os.environ.get("XDG_CURRENT_DESKTOP", "")
    kde = "KDE" in desk and os.environ.get("XDG_SESSION_TYPE") == "wayland"
    line(kde, "Desktop", f"{desk or '?'} on {os.environ.get('XDG_SESSION_TYPE', '?')}" +
         ("" if kde else ", letterbox needs KDE Plasma 6 on Wayland"))
    cfg = bar.load_config()
    sc = bar.screen(cfg)
    line(bool(sc), "Monitor", f"{sc['name']} {sc['w']}x{sc['h']}, game {sc['w']}x{cfg['height']}" if sc else
         "can't read the monitor setup (kscreen-doctor)")
    s = bar.sensors.Sensors(cfg.get("gpu", ""))
    gname = bar.sensors.gpu_name(os.path.basename(os.path.realpath(s.gpu))) if s.gpu else None
    line(True if s.gpu else None, "GPU", gname or "no AMD GPU found: the GPU tiles are hidden")
    line(True if s.cpu_hw else None, "CPU sensors",
         f"{s.cpu_hw[0]}" + (", power via RAPL" if s.rapl else ", power via the GW2 logger")
         if s.cpu_hw else "no CPU temperature sensor found")
    line(True if s.fans else None, "Fans", f"{len(s.fans)} found" if s.fans else "none found")
    g = subprocess.run([f"{DIR}/letterbox", "gfx-path"], capture_output=True, text=True).stdout.strip()
    line(bool(g), "GW2 settings", g or "not found: start GW2 once (or set gfx_settings in the config)")
    log_on = subprocess.run(["systemctl", "is-active", "letterbox-gw2log"], capture_output=True,
                            text=True).stdout.strip() == "active"
    line(True if log_on else None, "GW2 logger", "running" if log_on else
         "not running: map, combat, character and thread tiles stay empty")
    md = os.path.expanduser(cfg.get("mangohud_dir") or "")
    line(True if shutil.which("mangohud") and md else None, "MangoHud",
         f"logs to {md}" if shutil.which("mangohud") else "not installed: FPS tiles stay empty")
    opts = launch_options()
    try:
        gm = "letterbox start" in open(f"{HOME}/.config/gamemode.ini").read()
    except OSError:
        gm = False
    if gm:
        line(True, "Game start", "gamemode runs letterbox start/stop (~/.config/gamemode.ini)")
    else:
        line(True if opts and "letterbox run" in opts else (None if opts is None else False), "Launch option",
             opts if opts else ("(no Steam GW2 found)" if opts is None else
                                "letterbox run is missing, fix with: letterbox setup-launch"))
    line(True if os.path.exists(f"{HOME}/.config/letterbox/enabled") else None, "Letterbox mode",
         "on" if os.path.exists(f"{HOME}/.config/letterbox/enabled") else "off, turn it on with: letterbox on")
    g = game_dir()
    if g and os.path.isdir(f"{g}/addons/Nexus"):
        has = os.path.exists(f"{g}/addons/letterbox_bridge.dll")
        bf = bar.bridge_path(cfg)
        live = bool(bf) and os.path.exists(bf) and time.time() - os.path.getmtime(bf) < 5
        line(True if has else None, "Nexus bridge", ("running" if live else "installed, writes while GW2 runs")
             if has else "not installed (squad tile), fix with: letterbox setup-bridge")
    line(True if bar.HAS_PROBE else None, "Apple SMC tiles", "t2pm probe found" if bar.HAS_PROBE else
         "no t2pm probe (only on T2 MacBooks)")
    return 0 if ok else 1


def launch_options():
    """GW2's current Steam launch options, "" if none, None if GW2 isn't in a Steam config."""
    for root in STEAM_ROOTS:
        for f in glob.glob(f"{root}/userdata/*/config/localconfig.vdf"):
            text = open(f, encoding="utf-8", errors="surrogateescape").read()
            tok = vdf_tokens(text)
            rng = vdf_find(tok, ["UserLocalConfigStore", "Software", "Valve", "Steam", "apps", APPID])
            if rng:
                a, b = rng
                return next((tok[i + 1][1] for i in range(a + 1, b - 1)
                             if tok[i][1] == "LaunchOptions" and tok[i + 1][0] == "str"), "")
    return None


if __name__ == "__main__":
    cmd = sys.argv[1:] or ["doctor"]
    if cmd[0] == "launch" and len(cmd) > 1 and cmd[1] in ("add", "remove"):
        sys.exit(launch(cmd[1]))
    sys.exit({"doctor": doctor, "mangohud": mangohud, "gfx": gfx, "bridge": bridge}.get(cmd[0], doctor)())
