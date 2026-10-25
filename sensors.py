# sensors.py: CPU, GPU and fan readings straight from sysfs, for the bar's tiles. Returns the same keys
# as the optional t2pm probe log (see bar.py), so the bar draws either source the same way.
#   CPU clock: cpufreq scaling_cur_freq of all cores (average)
#   CPU temp:  hwmon coretemp (Intel: hottest core + package) or k10temp/zenpower (AMD: Tctl/Tdie)
#   CPU power: RAPL package energy (powercap); recent kernels allow it only for root, then the GW2 logger
#              (runs as root) supplies it
#   GPU:       amdgpu hwmon of the chosen card: power, power cap, shader/memory clock, edge/junction temp
#   Fans:      hwmon fan inputs (laptop/board/Apple SMC first, then the GPU's)
import glob, os, re, subprocess, time


def gpu_device(pci=""):
    """sysfs device dir of the GPU to show: the configured PCI address (e.g. "0000:03:00.0"), else the
    first amdgpu card that is not the boot display (a discrete card next to an iGPU), else any amdgpu."""
    cards = []
    for p in sorted(glob.glob("/sys/class/drm/card[0-9]*/device/gpu_busy_percent")):
        dev = os.path.dirname(p)
        real = os.path.realpath(dev)
        if pci:
            if real.endswith(pci):
                return dev
            continue
        try:
            boot = open(f"{dev}/boot_vga").read().strip() == "1"
        except OSError:
            boot = False
        cards.append((boot, dev))
    cards.sort()
    return cards[0][1] if cards else None


def gpu_list():
    """All AMD GPUs the bar can show: [(PCI address, sysfs device dir, readable name)]."""
    out = []
    for p in sorted(glob.glob("/sys/class/drm/card[0-9]*/device/gpu_busy_percent")):
        dev = os.path.dirname(p)
        pci = os.path.basename(os.path.realpath(dev))
        out.append((pci, dev, gpu_name(pci)))
    return out


def gpu_name(pci):
    """'AMD Navi 14 [Radeon RX 5500/5500M / Pro 5300/5300M/5500M]' from lspci, else the PCI address."""
    try:
        out = subprocess.run(["lspci", "-s", pci], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return pci
    out = re.sub(r"^\S+\s+[^:]+:\s*", "", out).replace("Advanced Micro Devices, Inc. [AMD/ATI]", "AMD")
    return re.sub(r"\s*\(rev \w+\)$", "", out) or pci


def _read(path, scale=1.0):
    try:
        return int(open(path).read().strip()) * scale
    except (OSError, ValueError):
        return None


def _hwmons():
    out = []
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            name = open(f"{d}/name").read().strip()
        except OSError:
            name = ""
        out.append((name, d))
    return out


def _labelled(d, kind):
    """{label: input path} of e.g. temp* in one hwmon dir (unlabelled ones as temp1, temp2, ...)."""
    out = {}
    for p in sorted(glob.glob(f"{d}/{kind}*_input")):
        base = p[:-len("_input")]
        try:
            label = open(base + "_label").read().strip()
        except OSError:
            label = os.path.basename(base)
        out[label] = p
    return out


class Sensors:
    def __init__(self, gpu_pci=""):
        self.gpu = gpu_device(gpu_pci)
        gpu_real = os.path.realpath(self.gpu) if self.gpu else None
        self.cpu_hw = self.gpu_hw = None
        self.fans = []
        for name, d in _hwmons():
            if name in ("coretemp", "k10temp", "zenpower") and not self.cpu_hw:
                self.cpu_hw = (name, d)
            elif name == "amdgpu" and gpu_real and os.path.realpath(f"{d}/device") == gpu_real:
                self.gpu_hw = d
            if name != "amdgpu":     # some drivers (applesmc on T2 Macs) keep the fans on the device itself
                self.fans += sorted(glob.glob(f"{d}/fan*_input")) or sorted(glob.glob(f"{d}/device/fan*_input"))
        if self.gpu_hw:
            self.fans += sorted(glob.glob(f"{self.gpu_hw}/fan*_input"))
        self.freqs = sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"))
        self.rapl = next((p for p in sorted(glob.glob("/sys/class/powercap/intel-rapl:[0-9]/energy_uj"))
                          if os.access(p, os.R_OK)), None)
        self.rapl_last = None

    def read(self):
        v = {}
        mhz = [x for x in (_read(p, 1e-3) for p in self.freqs) if x]
        if mhz:
            v["mhz_avg"], v["mhz_max"] = sum(mhz) / len(mhz), max(mhz)
        if self.cpu_hw:
            name, d = self.cpu_hw
            temps = {k: _read(p, 1e-3) for k, p in _labelled(d, "temp").items()}
            if name == "coretemp":
                cores = [t for k, t in temps.items() if k.startswith("Core") and t is not None]
                v["core_max_c"] = max(cores) if cores else None
                v["pkg_c"] = next((t for k, t in temps.items() if k.startswith("Package")), v["core_max_c"])
            else:
                t = temps.get("Tdie") or temps.get("Tctl") or next(iter(temps.values()), None)
                v["core_max_c"] = v["pkg_c"] = t
        if self.rapl:
            e, now = _read(self.rapl), time.monotonic()
            if e is not None and self.rapl_last and now > self.rapl_last[1]:
                de = e - self.rapl_last[0]
                if de >= 0:                                  # the counter wraps around now and then
                    v["pkg_w"] = de / 1e6 / (now - self.rapl_last[1])
            self.rapl_last = (e, now) if e is not None else None
        if self.gpu_hw:
            d = self.gpu_hw
            v["gpu_w"] = _read(f"{d}/power1_average", 1e-6) or _read(f"{d}/power1_input", 1e-6)
            v["gpu_cap_w"] = _read(f"{d}/power1_cap", 1e-6)
            freqs = {k: _read(p, 1e-6) for k, p in _labelled(d, "freq").items()}
            v["gpu_sclk"], v["gpu_mclk"] = freqs.get("sclk"), freqs.get("mclk")
            temps = {k: _read(p, 1e-3) for k, p in _labelled(d, "temp").items()}
            v["gpu_edge_c"], v["gpu_junc_c"] = temps.get("edge"), temps.get("junction") or temps.get("edge")
        fans = [x for x in (_read(p) for p in self.fans) if x is not None]
        v["fan1"] = fans[0] if fans else None
        v["fan2"] = fans[1] if len(fans) > 1 else None
        return {k: x for k, x in v.items() if x is not None}
