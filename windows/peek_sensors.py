"""
peek_sensors.py - readings beyond CPU, memory, disk and network.

Two kinds, both reported in the same reading the phone already understands
(see PROTOCOL.md section 1):

  * Built in: GPU load, temperature, power and memory, NPU load, fans and CPU
    package power. Found by asking, in order, nvidia-smi, LibreHardwareMonitor's
    web server, and Windows' own GPU performance counters. Whichever answers
    first for a given reading wins; the rest are not used for it.

  * Custom: anything you can get a number out of. A command, a file, a local
    web page or a perfmon counter, declared in config.json under "sensors".

Standard library only, like the rest of the agent. Nothing here imports Win32,
so the parsing is testable anywhere; only the process launches are
Windows-specific, and those are injected.

A negative number means "this machine has no such sensor" to the phone, so a
sensor with nothing to say is left out of the reading rather than sent as -1.
"""

import csv
import io
import json
import math
import re
import shlex
import shutil
import statistics
import subprocess
import threading
import time
import urllib.request

MAX_EXTRAS = 24            # the phone reads at most this many per machine
MAX_CUSTOM = 16
KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Names the phone already knows. An extra sensor must never shadow one.
RESERVED = frozenset({
    "host", "age_s", "messages", "metrics",
    "cpu_percent", "ram_percent", "cpu_temp_c", "storage_percent",
    "storage_total_gb", "storage_free_gb", "uptime_seconds",
    "net_rx_kbps", "net_tx_kbps",
    "battery_percent", "battery_charging", "battery_ac", "battery_minutes",
})

# Longest suffix first, so "_mbps" is never mistaken for "_mb".
SUFFIX_UNITS = (
    ("_percent", "%"), ("_seconds", "s"), ("_mbps", "Mbps"), ("_kbps", "kbps"),
    ("_pct", "%"), ("_mhz", "MHz"), ("_ghz", "GHz"), ("_rpm", "rpm"),
    ("_mb", "MB"), ("_gb", "GB"), ("_ms", "ms"),
    ("_c", "\u00b0C"), ("_w", "W"), ("_v", "V"), ("_a", "A"),
)

# Shown on the phone under these names with no further help, so they go as
# plain keys. Everything else is sent in "metrics" with a label of its own.
PLAIN_KEYS = frozenset({"gpu_percent", "gpu_temp_c", "npu_percent"})

LABELS = {
    "cpu_percent": "CPU", "ram_percent": "Memory", "cpu_temp_c": "CPU temperature",
    "storage_percent": "Storage", "battery_percent": "Battery",
    "net_kbps": "Network", "net_rx_kbps": "Network download",
    "net_tx_kbps": "Network upload",
    "gpu_percent": "GPU", "gpu_temp_c": "GPU temperature", "npu_percent": "NPU",
    "gpu_mem_percent": "GPU memory", "vram_used_gb": "VRAM used",
    "gpu_power_w": "GPU power", "gpu_fan_percent": "GPU fan",
    "gpu_fan_rpm": "GPU fan", "fan_rpm": "Fan", "cpu_power_w": "CPU power",
}


def unit_for_key(key):
    for suffix, unit in SUFFIX_UNITS:
        if key.endswith(suffix):
            return unit
    return ""


def label_for_key(key):
    """gpu_percent -> "GPU"; custom_sensor_w -> "Custom Sensor"."""
    if key in LABELS:
        return LABELS[key]
    base = key
    for suffix, _unit in SUFFIX_UNITS:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return " ".join(w.capitalize() for w in base.split("_") if w) or key


# --------------------------------------------------------------------------
#  Running things
# --------------------------------------------------------------------------
def run_process(argv, timeout=5):
    """stdout of a command, or None if it could not run or did not finish.

    Catches what launching a process can actually throw and nothing else: a
    bare `except Exception` here once turned a missing import into "this
    machine has no sensor" for the life of the program.
    """
    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout


def run_powershell(command, timeout=10):
    return run_process(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        timeout)


def http_get(url, timeout=3, limit=262144):
    req = urllib.request.Request(url, headers={"User-Agent": "PeekESP-agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(limit).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
#  Number extraction (shared by custom sensors)
# --------------------------------------------------------------------------
_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")


def to_float(text):
    """Parse one number out of a string, or None. "45,5" is 45.5."""
    if text is None:
        return None
    m = _NUM.search(str(text))
    if not m:
        return None
    s = m.group(0)
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def extract_number(text, regex=None):
    """The first number in text, or the first group of regex if one is given."""
    if text is None:
        return None
    if regex:
        try:
            m = re.search(regex, text, re.S)
        except re.error:
            return None
        if not m:
            return None
        return to_float(m.group(1) if m.groups() else m.group(0))
    return to_float(text)


def json_lookup(text, path):
    """"a.b.0.c" into parsed JSON: dict keys and list indexes."""
    try:
        node = json.loads(text)
    except ValueError:
        return None
    for part in str(path).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.lstrip("-").isdigit() \
                and -len(node) <= int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    if isinstance(node, bool):
        return None
    return to_float(node)


# --------------------------------------------------------------------------
#  Built-in sources
# --------------------------------------------------------------------------
NVIDIA_QUERY = ("utilization.gpu,temperature.gpu,power.draw,"
                "memory.used,memory.total,fan.speed")
_NVIDIA_PATHS = (
    r"C:\Windows\System32\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
)


def _opt(s):
    """A float, or None for "[N/A]" and "[Not Supported]"."""
    try:
        v = float(str(s).strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def parse_nvidia(text):
    """nvidia-smi csv (noheader, nounits) -> readings. Several GPUs are folded:
    the busiest and hottest are what a dashboard wants, power and memory add."""
    rows = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 6:
            rows.append([_opt(p) for p in parts[:6]])
    if not rows:
        return {}

    def col(i):
        return [r[i] for r in rows if r[i] is not None]

    out = {}
    util, temp, power, used, total, fan = (col(i) for i in range(6))
    if util:
        out["gpu_percent"] = max(util)
    if temp:
        out["gpu_temp_c"] = max(temp)
    if power:
        out["gpu_power_w"] = sum(power)
    if used:
        out["vram_used_gb"] = sum(used) / 1024.0           # MiB
        if total and sum(total) > 0:
            out["gpu_mem_percent"] = 100.0 * sum(used) / sum(total)
    if fan:
        out["gpu_fan_percent"] = max(fan)
    return out


def collect_nvidia(run=run_process, which=shutil.which):
    exe = which("nvidia-smi")
    if not exe:
        import os
        exe = next((p for p in _NVIDIA_PATHS if os.path.exists(p)), None)
    if not exe:
        return {}
    return parse_nvidia(run(
        [exe, f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"], 4))


# LibreHardwareMonitor ships its web server switched off, on 8085 by default.
LHM_PORTS = (8085, 6596, 6597)


def fetch_lhm(ports=LHM_PORTS, timeout=1.5):
    for port in ports:
        text = http_get(f"http://127.0.0.1:{port}/data.json", timeout, 4_000_000)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                continue
    return None


_VALUE = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)\s*(.*)$")


def _classify(sensor_id, typ, trail, unit):
    """(family, kind) for one LibreHardwareMonitor leaf.

    SensorId ("/gpu-nvidia/0/load/0") is exact, and recent versions include it.
    Without it we fall back to the hardware node's name and the unit, which
    is guesswork and is only reached on older builds.
    """
    fam = kind = ""
    if sensor_id.startswith("/"):
        seg = sensor_id.strip("/").split("/")
        fam = seg[0].lower()
        if len(seg) >= 2:
            kind = seg[-2].lower()
    if not kind and typ:
        kind = typ.lower()
    if not kind:
        kind = {"\u00b0c": "temperature", "%": "load", "rpm": "fan",
                "w": "power"}.get(unit.strip().lower(), "")
    for k in ("temperature", "fan", "power", "load", "smalldata"):
        if kind.startswith(k):
            kind = k
            break
    if not fam:
        hw = (trail[2] if len(trail) > 2 else "").lower()
        if any(w in hw for w in ("nvidia", "geforce", "radeon", "rtx", "gtx", " arc")):
            fam = "gpu"
        elif any(w in hw for w in ("cpu", "ryzen", "core i", "xeon")):
            fam = "cpu"
    if fam.startswith("gpu"):
        fam = "gpu"
    elif "cpu" in fam:
        fam = "cpu"
    return fam, kind


def parse_lhm(root):
    """LibreHardwareMonitor data.json -> readings."""
    rows = []

    def walk(node, trail):
        if not isinstance(node, dict):
            return
        text = str(node.get("Text", ""))
        here = trail + [text]
        kids = node.get("Children") or []
        if kids:
            for kid in kids:
                walk(kid, here)
            return
        m = _VALUE.match(str(node.get("Value", "")))
        if not m:
            return
        val = float(m.group(1).replace(",", "."))
        unit = m.group(2).strip()
        fam, kind = _classify(str(node.get("SensorId", "") or ""),
                              str(node.get("Type", "") or ""), here, unit)
        rows.append((fam, kind, text.lower(), val, unit))

    walk(root, [])

    def pick(fam, kind, prefer=(), how=max):
        sel = [(n, v) for f, k, n, v, _u in rows if f == fam and k == kind]
        if not sel:
            return None
        for want in prefer:
            hit = [v for n, v in sel if n == want]
            if hit:
                return max(hit)
        return how(v for _n, v in sel)

    out = {}
    v = pick("gpu", "load", ("gpu core", "d3d 3d"))
    if v is not None:
        out["gpu_percent"] = v
    hit = [x for f, k, n, x, _u in rows if (f, k, n) == ("gpu", "load", "gpu memory")]
    if hit:
        out["gpu_mem_percent"] = max(hit)
    v = pick("gpu", "temperature", ("gpu core",))
    if v is not None:
        out["gpu_temp_c"] = v
    v = pick("gpu", "power", ("gpu package", "gpu power", "gpu total"))
    if v is not None:
        out["gpu_power_w"] = v
    for f, k, n, x, u in rows:
        if (f, k, n) == ("gpu", "smalldata", "gpu memory used"):
            out["vram_used_gb"] = x if u.upper() == "GB" else x / 1024.0
    v = pick("cpu", "power", ("cpu package",))
    if v is not None:
        out["cpu_power_w"] = v
    gpu_fans = [x for f, k, _n, x, _u in rows if f == "gpu" and k == "fan" and x > 0]
    other_fans = [x for f, k, _n, x, _u in rows if f != "gpu" and k == "fan" and x > 0]
    if gpu_fans:
        out["gpu_fan_rpm"] = max(gpu_fans)
    if other_fans:
        out["fan_rpm"] = max(other_fans)
    return out


# Windows' GPU performance counters. Queried through CIM rather than
# Get-Counter because class and property names are not localised, whereas
# counter paths are - "\GPU Engine(*)" does not exist on a German install.
ENGINE_PS = (
    "Get-CimInstance -ClassName Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine "
    "-ErrorAction Stop | ForEach-Object { '{0}|{1}' -f $_.Name, $_.UtilizationPercentage }")

_ENGINE = re.compile(
    r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+_eng_\d+_engtype_(.+)$", re.I)
_NPU_ENGINES = {"compute", "neural", "npu"}


class EngineParser:
    """Turns GPU Engine counter rows into GPU and NPU load.

    Windows has no NPU counter of its own: Task Manager reads the same "GPU
    Engine" counters and tells the adapters apart through DXCore, which is not
    reachable from here. So an adapter is treated as an NPU when every engine
    it has shown is compute or neural and none is 3D, video or copy. An idle
    NPU has no instances at all - counters exist per process - so once one has
    been recognised it is remembered, and reads 0 when it goes quiet.
    """

    def __init__(self):
        self.npu_luids = set()

    def parse(self, lines):
        per = {}                      # luid -> engtype -> summed utilisation
        for line in lines:
            name, _, val = line.partition("|")
            m = _ENGINE.search(name.strip())
            v = _opt(val)
            if not m or v is None:
                continue
            luid, eng = m.group(1).lower(), m.group(2).strip().lower()
            d = per.setdefault(luid, {})
            d[eng] = d.get(eng, 0.0) + v

        for luid, engines in per.items():
            if engines and set(engines) <= _NPU_ENGINES:
                self.npu_luids.add(luid)

        gpu = [max(e.values()) for l, e in per.items()
               if l not in self.npu_luids and e]
        npu = [max(per[l].values()) if per.get(l) else 0.0
               for l in self.npu_luids]
        out = {}
        if gpu:
            out["gpu_percent"] = max(0.0, min(100.0, max(gpu)))
        if npu:
            out["npu_percent"] = max(0.0, min(100.0, max(npu)))
        return out


def collect_engines(parser, run=run_powershell):
    text = run(ENGINE_PS, 12)
    if not text:
        return {}
    return parser.parse(text.splitlines())


# --------------------------------------------------------------------------
#  Custom sensors
# --------------------------------------------------------------------------
SOURCES = ("command", "file", "http", "counter")


def clean_custom(raw):
    """Validate the "sensors" list from config.json -> (clean list, warnings).

    A hand-edited file will have mistakes in it. A bad entry is dropped with a
    reason, not allowed to stop the agent or the sensors beside it.
    """
    out, warns, seen = [], [], set()
    if not isinstance(raw, list):
        return out, warns
    for i, r in enumerate(raw[:MAX_CUSTOM * 2]):
        tag = f"sensor #{i + 1}"
        if not isinstance(r, dict):
            warns.append(f"{tag}: not an object")
            continue
        key = str(r.get("key", "")).strip().lower()
        tag = f"sensor '{key or i + 1}'"
        if not KEY_RE.match(key):
            warns.append(f"{tag}: key must be a-z, 0-9 and _ (32 max)")
            continue
        if key in RESERVED:
            warns.append(f"{tag}: '{key}' is already a built-in reading")
            continue
        if key in seen:
            warns.append(f"{tag}: duplicate key")
            continue
        src = str(r.get("source", "")).strip().lower()
        if src not in SOURCES:
            warns.append(f"{tag}: source must be one of {', '.join(SOURCES)}")
            continue
        s = {"key": key, "source": src}

        if src == "command":
            cmd = r.get("command")
            if isinstance(cmd, str):
                try:
                    cmd = [a.strip('"') for a in shlex.split(cmd, posix=False)]
                except ValueError:
                    cmd = None
            if not (isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd)):
                warns.append(f"{tag}: command is empty or malformed")
                continue
            s["command"] = cmd
        elif src == "file":
            if not str(r.get("path", "")).strip():
                warns.append(f"{tag}: path is empty")
                continue
            s["path"] = str(r["path"]).strip()
        elif src == "http":
            url = str(r.get("url", "")).strip()
            if not url.lower().startswith(("http://", "https://")):
                warns.append(f"{tag}: url must start with http:// or https://")
                continue
            s["url"] = url
        else:
            if not str(r.get("counter", "")).strip():
                warns.append(f"{tag}: counter is empty")
                continue
            s["counter"] = str(r["counter"]).strip()

        s["label"] = str(r.get("label", "") or "").strip()[:40]
        s["unit"] = str(r.get("unit", "") or "").strip()[:8]
        for f in ("regex", "json_path"):
            if str(r.get(f, "") or "").strip():
                s[f] = str(r[f]).strip()[:200]
        try:
            s["interval"] = max(2.0, min(3600.0, float(r.get("interval", 10))))
        except (TypeError, ValueError):
            s["interval"] = 10.0
        try:
            s["scale"] = float(r.get("scale", 1.0))
        except (TypeError, ValueError):
            s["scale"] = 1.0
        mx = r.get("max")
        try:
            s["max"] = float(mx) if mx not in (None, "") else None
        except (TypeError, ValueError):
            s["max"] = None
        if s["max"] is not None and not math.isfinite(s["max"]):
            s["max"] = None
        seen.add(key)
        out.append(s)
        if len(out) >= MAX_CUSTOM:
            break
    return out, warns


def read_custom(spec, run=run_process, fetch=http_get):
    """Read one custom sensor once -> (value, error). Exactly one is None."""
    src = spec["source"]
    text = None
    if src == "file":
        try:
            with open(spec["path"], "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(65536)
        except OSError as e:
            return None, f"cannot read file: {e.strerror or e}"
    elif src == "http":
        text = fetch(spec["url"])
        if text is None:
            return None, "no answer from the URL"
    elif src == "command":
        text = run(spec["command"], 6)
        if text is None:
            return None, "command did not run or took over 6 s"
    else:
        text = run(["typeperf", spec["counter"], "-sc", "2"], 8)
        if text is None:
            return None, "typeperf did not run"
        rows = [r for r in csv.reader(io.StringIO(text))
                if len(r) >= 2 and re.match(r"\d", r[0])]
        text = rows[-1][-1] if rows else None
        if text is None:
            return None, "counter not found (counter names are localised)"

    if spec.get("json_path"):
        v = json_lookup(text, spec["json_path"])
    else:
        v = extract_number(text, spec.get("regex"))
    if v is None:
        return None, "no number found in the output"
    v *= spec.get("scale", 1.0)
    return (v, None) if math.isfinite(v) else (None, "not a finite number")


# --------------------------------------------------------------------------
#  The collector
# --------------------------------------------------------------------------
PRIORITY = {"custom": 0, "nvidia": 1, "lhm": 2, "engines": 3}


class ExtraSensors:
    """Samples in its own thread so a reading never waits on a process launch.

    A source that keeps returning nothing is asked less and less often, up to
    every five minutes, so a machine with no NVIDIA card is not launching
    nvidia-smi forever.
    """

    def __init__(self, enabled=True, interval=10.0, custom=None,
                 run=run_process, ps=run_powershell, fetch=http_get,
                 lhm=fetch_lhm, which=shutil.which, clock=time.monotonic):
        self.enabled = enabled
        self.interval = max(2.0, float(interval))
        self.custom = list(custom or [])
        self._run, self._ps, self._fetch, self._lhm = run, ps, fetch, lhm
        self._which = which
        self._clock = clock
        self._engines = EngineParser()
        self._vals = {}               # key -> {source: (value, expires)}
        self._errors = {}             # custom key -> last error
        self._due = {}                # source name -> next time
        self._miss = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        # A fresh event per start: stop() then start() must never leave the old
        # thread watching a flag the new one has just cleared.
        self.stop()
        stop = self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(stop,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self, stop):
        while not stop.is_set():
            self.collect_once()
            stop.wait(1.0)

    # -- sampling ----------------------------------------------------------
    def _store(self, source, readings, now, every):
        exp = now + 3 * every + 5
        with self._lock:
            for k, v in readings.items():
                if v is not None and math.isfinite(v):
                    self._vals.setdefault(k, {})[source] = (v, exp)

    def _due_now(self, name, now, force):
        return force or now >= self._due.get(name, 0.0)

    def _after(self, name, now, got, base):
        miss = 0 if got else self._miss.get(name, 0) + 1
        self._miss[name] = miss
        self._due[name] = now + min(300.0, base * (1 + miss) if miss else base)

    def _gpu_from_elsewhere(self, now):
        with self._lock:
            srcs = self._vals.get("gpu_percent", {})
            return any(s != "engines" and exp > now for s, (_v, exp) in srcs.items())

    def collect_once(self, force=False):
        """Run whatever is due. `force` runs everything now (for --once)."""
        now = self._clock()
        if self.enabled:
            base = self.interval
            if self._due_now("nvidia", now, force):
                r = collect_nvidia(self._run, self._which)
                self._store("nvidia", r, now, base)
                self._after("nvidia", now, r, base)
            if self._due_now("lhm", now, force):
                tree = self._lhm()
                r = parse_lhm(tree) if tree else {}
                self._store("lhm", r, now, base)
                self._after("lhm", now, r, base)
            if self._due_now("engines", now, force):
                # The slowest source, and the only one that can see an NPU, so
                # it runs less often when something cheaper already has a GPU.
                every = max(base, 30.0) if self._gpu_from_elsewhere(now) else base
                r = collect_engines(self._engines, self._ps)
                self._store("engines", r, now, every)
                self._after("engines", now, r, every)

        for spec in self.custom:
            name = "c:" + spec["key"]
            if not self._due_now(name, now, force):
                continue
            v, err = read_custom(spec, self._run, self._fetch)
            with self._lock:
                if err:
                    self._errors[spec["key"]] = err
                else:
                    self._errors.pop(spec["key"], None)
            if v is not None:
                self._store("custom", {spec["key"]: v}, now, spec["interval"])
            self._due[name] = now + spec["interval"]

    # -- output ------------------------------------------------------------
    def _fresh(self):
        now = self._clock()
        out = {}
        with self._lock:
            for k, srcs in self._vals.items():
                live = [(PRIORITY.get(s, 9), s, v) for s, (v, exp) in srcs.items()
                        if exp > now]
                if live:
                    _p, s, v = min(live)
                    out[k] = (v, s)
        return out

    def payload(self):
        """(plain keys, metrics object) ready to merge into a reading."""
        fresh = self._fresh()
        specs = {s["key"]: s for s in self.custom}
        order = sorted(fresh, key=lambda k: (k in specs, k))   # built-ins first
        plain, metrics = {}, {}
        for k in order:
            v = fresh[k][0]
            if v < 0 or k in RESERVED or not KEY_RE.match(k):
                continue
            if len(plain) + len(metrics) >= MAX_EXTRAS:
                break
            v = round(v, 1)
            spec = specs.get(k)
            if k in PLAIN_KEYS and not spec:
                plain[k] = v
                continue
            unit = (spec or {}).get("unit") or unit_for_key(k)
            m = {"value": v, "label": (spec or {}).get("label") or label_for_key(k)}
            if unit:
                m["unit"] = unit
            mx = (spec or {}).get("max")
            if mx is None and unit == "%":
                mx = 100
            if mx is not None:
                m["max"] = mx
            metrics[k] = m
        return plain, metrics

    def describe(self):
        """Human-readable lines for the settings window."""
        fresh = self._fresh()
        lines = []
        for k in sorted(fresh):
            v, src = fresh[k]
            unit = unit_for_key(k)
            lines.append(f"{label_for_key(k):<20} {v:g} {unit}".rstrip()
                         + f"   ({src})")
        for k, err in sorted(self._errors.items()):
            lines.append(f"{k:<20} -  {err}")
        return lines


# --------------------------------------------------------------------------
def robust_scale(values):
    """(median, scale) where scale is the MAD made comparable to a standard
    deviation. One spike cannot stretch its own yardstick, which a mean and
    standard deviation would let it do."""
    med = statistics.median(values)
    mad = statistics.median(abs(x - med) for x in values)
    return med, 1.4826 * mad