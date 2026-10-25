#!/usr/bin/env python3
# letterbox-gw2log.py: logs what GW2 is doing (map, combat, mount, character) once per second while it
# runs; the letterbox bar shows it live. Source: GW2's MumbleLink shared memory (the official interface
# for overlays). Under Proton it is a Wine file mapping inside the game process, so this runs as root
# (letterbox-gw2log.service) and reads it through /proc/<pid>/mem. Output: /var/log/letterbox/
# gw2-YYYYMMDD.csv (local time, kept 14 days, LETTERBOX_KEEP_DAYS=0 keeps them all). Map and specialization names come from the public GW2 API
# once per id and are cached in /var/lib/letterbox/gw2-names.json.
# Also logged per second: the CPU use of GW2's busiest threads (% of one core: GW2's main/render thread
# can be the bottleneck while the CPU as a whole looks idle), the GPU's busy % sampled at 10 Hz (average
# and peak) and the CPU package power (RAPL, readable only by root on current kernels).
import csv, glob, json, math, os, re, struct, sys, time, urllib.request, zlib
from datetime import datetime

LOGDIR = "/var/log/letterbox"
CACHE = "/var/lib/letterbox/gw2-names.json"
KEEP_DAYS = int(os.environ.get("LETTERBOX_KEEP_DAYS", "14"))
NAME = "Guild Wars 2".encode("utf-16-le")
FIELDS = ["time", "map_id", "map_name", "map_type", "in_combat", "mount", "map_open", "focus",
          "x", "y", "z", "tick", "thread1_pct", "thread1_name", "thread2_pct", "thread2_name", "proc_pct",
          "threads_over_50", "gpu_busy_avg", "gpu_busy_max",
          "char_name", "profession", "spec", "commander", "heading", "cpu_pkg_w"]
PROFESSIONS = {1: "Guardian", 2: "Warrior", 3: "Engineer", 4: "Ranger", 5: "Thief", 6: "Elementalist",
               7: "Mesmer", 8: "Necromancer", 9: "Revenant"}
CLK_TCK = os.sysconf("SC_CLK_TCK")
RAPL = next(iter(sorted(glob.glob("/sys/class/powercap/intel-rapl:[0-9]/energy_uj"))), None)


def gpu_busy_file():
    """gpu_busy_percent of the GPU the bar shows: "gpu" (PCI address) in the users' letterbox configs is
    not readable here in general, so: LETTERBOX_GPU from the service, else the first amdgpu that is not
    the boot display (a discrete card next to an iGPU), else any."""
    want = os.environ.get("LETTERBOX_GPU", "")
    cards = []
    for p in sorted(glob.glob("/sys/class/drm/card[0-9]*/device/gpu_busy_percent")):
        real = os.path.realpath(os.path.dirname(p))
        if want:
            if real.endswith(want):
                return p
            continue
        try:
            boot = open(os.path.join(os.path.dirname(p), "boot_vga")).read().strip() == "1"
        except OSError:
            boot = False
        cards.append((boot, p))
    return sorted(cards)[0][1] if cards else None


GPU_BUSY = gpu_busy_file()


class Rapl:
    """CPU package power in W from the RAPL energy counter."""

    def __init__(self):
        self.last = None

    def sample(self):
        try:
            e, t = int(open(RAPL).read()), time.monotonic()
        except (OSError, ValueError, TypeError):
            return ""
        out = ""
        if self.last and t > self.last[1] and e >= self.last[0]:
            out = round((e - self.last[0]) / 1e6 / (t - self.last[1]), 1)
        self.last = (e, t)
        return out


def cleanup():
    if KEEP_DAYS <= 0:
        return
    cut = time.time() - KEEP_DAYS * 86400
    for p in glob.glob(f"{LOGDIR}/gw2-*.csv"):
        try:
            if os.path.getmtime(p) < cut:
                os.remove(p)
        except OSError:
            pass
# MumbleContext.mapType (GW2 wiki)
MAP_TYPES = {0: "redirect", 1: "character create", 2: "pvp", 3: "gvg", 4: "instance", 5: "open world",
             6: "tournament", 7: "tutorial", 8: "user tournament", 9: "wvw", 10: "wvw", 11: "wvw",
             12: "wvw", 13: "wvw lobby", 14: "obsidian sanctum", 15: "edge of the mists", 16: "public mini",
             18: "wvw lobby"}
MOUNTS = {0: "", 1: "jackal", 2: "griffon", 3: "springer", 4: "skimmer", 5: "raptor", 6: "roller beetle",
          7: "warclaw", 8: "skyscale", 9: "skiff", 10: "siege turtle"}


def gw2_pids():
    """The game process itself (comm "Gw2-64.exe"); wrappers like steam.exe/reaper only mention it."""
    out = []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open(f"/proc/{p}/comm") as f:
                if f.read().strip().lower() == "gw2-64.exe":
                    out.append(int(p))
        except OSError:
            continue
    return out


class Threads:
    """CPU use per thread of one process, from /proc/<pid>/task/*/stat deltas."""

    def __init__(self, pid):
        self.pid, self.last, self.t = pid, {}, None

    def sample(self):
        now, cur = time.monotonic(), {}
        for st in glob.glob(f"/proc/{self.pid}/task/*/stat"):
            try:
                raw = open(st).read()
            except OSError:
                continue
            name = raw[raw.index("(") + 1:raw.rindex(")")]
            f = raw[raw.rindex(")") + 2:].split()
            cur[st] = (name, int(f[11]) + int(f[12]))          # utime + stime
        out = None
        if self.t is not None and now - self.t >= 0.5:      # a shorter interval gives nonsense percentages
            dt = now - self.t
            pct = sorted(((100 * (j - self.last[k][1]) / CLK_TCK / dt, n) for k, (n, j) in cur.items()
                          if k in self.last), reverse=True)
            if pct:
                pct += [(0.0, "")]
                out = dict(thread1_pct=round(pct[0][0], 1), thread1_name=pct[0][1],
                           thread2_pct=round(pct[1][0], 1), thread2_name=pct[1][1],
                           proc_pct=round(sum(p for p, _ in pct), 1),
                           threads_over_50=sum(p >= 50 for p, _ in pct))
        self.last, self.t = cur, now
        return out


def gpu_second():
    """Sleep ~1 s while sampling the Radeon's busy % at 10 Hz -> (avg, max)."""
    vals = []
    for _ in range(10):
        if GPU_BUSY:
            try:
                vals.append(int(open(GPU_BUSY).read()))
            except (OSError, ValueError):
                pass
        time.sleep(0.1)
    return (round(sum(vals) / len(vals), 1), max(vals)) if vals else ("", "")


def find_link(pid):
    """Address of the LinkedMem struct: a small mapping that starts with the MumbleLink layout and has
    'Guild Wars 2' at offset 44 (name field)."""
    try:
        maps = open(f"/proc/{pid}/maps").read().splitlines()
        mem = open(f"/proc/{pid}/mem", "rb", buffering=0)
    except OSError:
        return None, None
    for line in maps:
        rng, perms = line.split()[:2]
        a, b = (int(x, 16) for x in rng.split("-"))
        # Wine keeps named file mappings in memfds; skip everything else (GPU buffers, files)
        if "r" not in perms or "wine-mapping" not in line or not (4096 <= b - a <= 65536):
            continue
        try:
            mem.seek(a + 44)
            if mem.read(len(NAME)) == NAME:
                return mem, a
        except OSError:
            continue
    mem.close()
    return None, None


def map_name(mid, cache):
    if not mid:
        return ""
    key = str(mid)
    if key not in cache:
        try:
            with urllib.request.urlopen(f"https://api.guildwars2.com/v2/maps/{mid}", timeout=5) as r:
                cache[key] = json.load(r).get("name", "")
        except Exception:
            return ""                      # try again next time
        save(cache)
    return cache[key]


def save(cache):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


def spec_name(sid, cache):
    """Specialization name from the GW2 API, cached in the same file as the map names (key 'spec:<id>')."""
    if not sid:
        return ""
    key = f"spec:{sid}"
    if key not in cache:
        try:
            with urllib.request.urlopen(f"https://api.guildwars2.com/v2/specializations/{sid}", timeout=5) as r:
                cache[key] = json.load(r).get("name", "")
        except Exception:
            return ""
        save(cache)
    return cache[key]


def parse(buf):
    tick = struct.unpack_from("<I", buf, 4)[0]
    x, y, z = struct.unpack_from("<3f", buf, 8)
    ctx = 1108
    map_id, map_type = struct.unpack_from("<II", buf, ctx + 28)
    ui_state = struct.unpack_from("<I", buf, ctx + 48)[0]
    mount = buf[ctx + 84]
    ident = {}
    try:                                    # identity JSON: character, profession, spec, map id, commander
        ident = json.loads(buf[592:1104].decode("utf-16-le").split("\0")[0] or "{}")
        map_id = ident.get("map_id", map_id)
    except ValueError:
        pass
    fx, _fy, fz = struct.unpack_from("<3f", buf, 568)       # camera front vector
    heading = round(math.degrees(math.atan2(fx, fz)) % 360) if (fx or fz) else ""
    return dict(tick=tick, x=round(x, 1), y=round(y, 1), z=round(z, 1), map_id=map_id,
                map_type=MAP_TYPES.get(map_type, str(map_type)),
                in_combat=int(bool(ui_state & 0x40)), map_open=int(bool(ui_state & 0x1)),
                focus=int(bool(ui_state & 0x8)), mount=MOUNTS.get(mount, str(mount)),
                char_name=ident.get("name", ""), profession=PROFESSIONS.get(ident.get("profession"), ""),
                spec_id=ident.get("spec", 0), commander=int(bool(ident.get("commander"))), heading=heading)


def main():
    try:
        cache = json.load(open(CACHE))
    except (OSError, ValueError):
        cache = {}
    mem = addr = None
    out = day = None
    last_tick = None
    threads = None
    gpu = ("", "")
    rapl = Rapl()
    os.makedirs(LOGDIR, exist_ok=True)
    while True:
        if mem is None:
            for pid in gw2_pids():
                mem, addr = find_link(pid)
                if mem:
                    print(f"letterbox-gw2log: MumbleLink found in pid {pid} at {addr:#x}", flush=True)
                    threads = Threads(pid)
                    threads.sample()
                    break
            if mem is None:
                if out:
                    out.close()
                    out = None
                time.sleep(5)
                continue
        try:
            mem.seek(addr)
            buf = mem.read(1364)
        except OSError:
            mem.close()
            mem = None
            print("letterbox-gw2log: GW2 gone", flush=True)
            continue
        if len(buf) < 1364 or buf[44:44 + len(NAME)] != NAME:
            mem.close()
            mem = None
            continue
        d = parse(buf)
        if d["tick"] == last_tick:          # game not updating (loading screen, minimized): skip
            gpu = gpu_second()
            threads.sample()
            continue
        last_tick = d["tick"]
        now = datetime.now()
        if out is None or now.strftime("%Y%m%d") != day:
            if out:
                out.close()
            day = now.strftime("%Y%m%d")
            cleanup()
            path = f"{LOGDIR}/gw2-{day}.csv"
            if os.path.exists(path):              # older file with other columns: one extra file per column set
                with open(path) as f:
                    if f.readline().strip() != ",".join(FIELDS):
                        path = f"{LOGDIR}/gw2-{day}-c{zlib.crc32(','.join(FIELDS).encode()) % 10000:04d}.csv"
            new = not os.path.exists(path)
            out = open(path, "a", newline="")
            w = csv.DictWriter(out, FIELDS)
            if new:
                w.writeheader()
        d["time"] = now.strftime("%H:%M:%S")
        d["map_name"] = map_name(d["map_id"], cache)
        d["spec"] = spec_name(d.pop("spec_id"), cache)
        d.update(threads.sample() or {})
        d["gpu_busy_avg"], d["gpu_busy_max"] = gpu
        d["cpu_pkg_w"] = rapl.sample()
        w.writerow(d)
        out.flush()
        gpu = gpu_second()                  # the next row gets the GPU numbers of this second


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("run as root (reads the game's memory through /proc/<pid>/mem)")
    main()
