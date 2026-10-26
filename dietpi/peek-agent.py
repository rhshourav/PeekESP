#!/usr/bin/env python3
"""
peek-agent.py - the telemetry endpoint PeekESP polls.

Serves the JSON that PeekESP.ino expects on http://0.0.0.0:6595/telemetry.
Pure standard library, reads /proc directly - no psutil, no pip, nothing to
install on a DietPi.

    { "host": "dietpi", "cpu_percent": 12.5, "ram_percent": 43.2,
      "storage_percent": 61.0, "storage_total_gb": 117.9,
      "storage_free_gb": 46.0, "cpu_temp_c": 48.3, "battery_percent": 78,
      "battery_charging": true, "battery_ac": true, "battery_minutes": 134,
      "uptime_seconds": 271830, "net_rx_kbps": 128.4, "net_tx_kbps": 12.9 }

Install it:  curl -fsSL https://raw.githubusercontent.com/rhshourav/PeekESP/\\
             main/dietpi/install.sh | sudo sh
Pair it:     python3 peek-agent.py --pair-code K7M2-P4QX-9R --no-serve
Serve it:    python3 peek-agent.py          # JSON on :6595, for a LAN device

Beyond that standard reading it can add GPU, NPU, fan and disk-temperature
sensors and your own, run alert rules on this machine, and carry messages from
other programs to the phone. All optional; see "Extra sensors" below.

See dietpi/README.md for the rest.
"""

import argparse
import binascii
import copy
import datetime
import glob
import hashlib
import hmac
import json
import math
import os
import re
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = "0.0.0.0"          # must NOT be 127.0.0.1, or the tunnel cannot reach it
PORT = 6595
PATH = "/telemetry"
SAMPLE_SECONDS = 2.0      # how often the background sampler takes a reading

# Mounted over the network. The bytes are real, but they are not this machine's
# storage, and an unreachable server makes statvfs() hang rather than fail.
NET_FSTYPES = frozenset((
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "sshfs", "ncpfs", "afs", "9p",
))

# A thermal zone is whatever the board vendor decided to expose: on a Pi it is
# the SoC, on a laptop it can be the battery bay or the wifi card. Prefer the
# zones that are actually a CPU before falling back to "the first one that
# reads above zero".
CPU_ZONE_HINTS = ("x86_pkg_temp", "coretemp", "cpu-thermal", "cpu_thermal",
                  "soc_thermal", "k10temp", "acpitz")

# Cloudflare's edge bans the default "Python-urllib/3.x" user agent outright
# with error 1010, before the Worker ever runs - so a push would fail with an
# opaque 403 that no amount of checking tokens would explain. Identify
# ourselves properly instead.
USER_AGENT = "PeekESP-agent/1.0 (+https://github.com/rhshourav/PeekESP)"

# The same relay the firmware ships pointing at, so a pairing code alone is
# enough on both sides and there is no URL to copy anywhere.
RELAY_BASE = "https://peek-relay.peekesp.workers.dev"

# Kept in step with the VERSION file by tools/bump_version.py, which CI runs
# with --check. Before this the Linux agent carried no version at all, so
# `peekesp version` could only print a path and a date, and there was nothing
# to compare a release against.
AGENT_VERSION = "2.0.1"

RELEASES_API = "https://api.github.com/repos/rhshourav/PeekESP/releases/latest"


# --------------------------------------------------------------------------
#  Commands for the display.
#
#  The display polls and never listens, so a command is left at the relay and
#  collected on its next poll: queued, not done. The vocabulary is the relay's
#  own closed set (COMMANDS in cloudflare/src/index.js). The relay would reject
#  anything else too, but refusing it here names the valid verbs instead of
#  printing an HTTP 400.
# --------------------------------------------------------------------------
COMMANDS = ("reboot", "standby", "wake", "refresh", "identify")

# The verbs that take a number, and the highest one worth sending.
#
# The relay accepts 0-15 for both. The firmware bounds-checks and silently
# ignores anything past what it has, so "bright 9" would be accepted and do
# nothing - the least useful way for a command to fail. The display has four
# backlight levels, so bright stops at 3. The page count depends on how many
# machines the display is showing, which only the display knows, so page is
# held to the relay's bound and nothing tighter.
ARG_COMMANDS = {"page": 15, "bright": 3}


def normalise_command(verb, arg=None):
    """'Reboot' -> 'reboot'; ('bright', 2) or 'bright:2' -> 'bright:2'.

    Raises ValueError with a message meant for a person."""
    text = str(verb or "").strip().lower()
    if arg is None and ":" in text:
        text, _, arg = text.partition(":")
    if text in COMMANDS:
        if arg not in (None, ""):
            raise ValueError("'%s' takes no number" % text)
        return text
    if text in ARG_COMMANDS:
        if arg is None or str(arg).strip() == "":
            raise ValueError("'%s' needs a number: %s N" % (text, text))
        try:
            n = int(str(arg).strip())
        except ValueError:
            raise ValueError("'%s' needs a whole number, not '%s'" % (text, arg))
        if not 0 <= n <= ARG_COMMANDS[text]:
            raise ValueError("%s must be 0-%d" % (text, ARG_COMMANDS[text]))
        return "%s:%d" % (text, n)
    raise ValueError("unknown command '%s'. Known: %s, page N, bright N"
                     % (text, ", ".join(COMMANDS)))


def send_command(code, verb, relay_base=RELAY_BASE, timeout=10):
    """Leave a command for the display. Returns (ok, message).

    Authorised with the push token this machine already derives from the
    pairing code, so it grants nothing this machine did not already have:
    anything that can push its telemetry can also ask its display to reboot.
    """
    import urllib.error
    import urllib.request

    try:
        text = normalise_command(verb)
        d = pair_derive(code)
    except ValueError as e:
        return False, str(e)

    base = (relay_base or "").rstrip("/")
    # Loopback may be plain HTTP - it is how this is tested, and how a relay
    # run with `wrangler dev` is reached. Anywhere else would carry the push
    # token in the clear, so it is refused before a connection is opened.
    if not (base.startswith("https://")
            or re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", base)):
        return False, "the relay must be an https:// URL, not %s" % (base or "nothing")

    # The bare verb as text, not JSON. The Worker hands request.text()
    # straight to its parser, so {"cmd": "reboot"} would be read as that
    # literal string and refused as an unknown command.
    req = urllib.request.Request(
        "%s/command/%s" % (base, d["stream"]),
        data=text.encode("ascii"),
        method="POST",
        headers={
            "Content-Type": "text/plain",
            "Authorization": "Bearer " + d["push"],
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "the relay refused the credential derived from this pairing code"
        if e.code == 400:
            return False, "the relay rejected '%s'" % text
        return False, "the relay answered HTTP %d %s" % (e.code, e.reason)
    except (urllib.error.URLError, OSError) as e:
        return False, "cannot reach %s: %s" % (base, getattr(e, "reason", e))
    return True, ("%s queued - the display collects it on its next poll, within "
                  "about 5 s, or a minute if it is in standby" % text)


# --------------------------------------------------------------------------
#  Is there a newer release.
# --------------------------------------------------------------------------
def _version_tuple(text):
    """'v1.10.2' -> (1, 10, 2).

    Numbers, not strings: "1.10.0" is newer than "1.9.0" and sorts before it
    alphabetically, which is the classic way an update check goes quiet at
    exactly the point it starts to matter."""
    out = []
    for part in str(text or "").strip().lstrip("vV").split(".")[:4]:
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_newer(latest, current):
    a, b = _version_tuple(latest), _version_tuple(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def latest_release(url=RELEASES_API, timeout=8):
    """The newest published version, or "" if it could not be found out.

    Never raises. A failed check has to read as "could not check" - a network
    error mistaken for news is how an update prompt teaches people to ignore
    it.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("tag_name") or "").strip().lstrip("vV")[:20]


def update_status(latest, current=AGENT_VERSION):
    """(exit status, sentence): 0 up to date, 10 newer available, 1 unknown."""
    if not latest:
        return 1, "installed %s; could not reach GitHub to check" % current
    if is_newer(latest, current):
        return 10, "installed %s; %s is available" % (current, latest)
    return 0, "installed %s; up to date (latest is %s)" % (current, latest)


# --------------------------------------------------------------------------
#  Pairing: the device shows a code, you type it here, and both ends derive
#  the same stream and tokens locally. Nothing but the code ever moves between
#  them, and the relay never sees it - which is why this needs no secrets
#  configured on the Worker at all.
#
#      stream = SHA-256("peek-stream:" + CODE)  first 16 hex
#      push   = SHA-256("peek-push:"   + CODE)  first 48 hex
#
#  Kept byte-identical to windows/peek_pair.py, cloudflare/src/index.js and the
#  firmware's C++. A drift in any one of them would have this machine pushing
#  to a stream the device never reads, with every request looking perfectly
#  valid from both ends.
# --------------------------------------------------------------------------
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIR_CODE_LEN = 10


def pair_normalise(code):
    """Dashes and case are decoration: "k7m2-p4qx-9r" is "K7M2P4QX9R"."""
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def pair_derive(code):
    c = pair_normalise(code)
    if len(c) != PAIR_CODE_LEN or any(ch not in PAIR_ALPHABET for ch in c):
        raise ValueError(
            "pairing code must be %d characters from %s "
            "(dashes and case are ignored)" % (PAIR_CODE_LEN, PAIR_ALPHABET))

    def h(prefix, n):
        return hashlib.sha256((prefix + c).encode("ascii")).hexdigest()[:n]

    return {"code": c, "stream": h("peek-stream:", 16), "push": h("peek-push:", 48)}


# --------------------------------------------------------------------------
#  Rate-based readings need two samples, so a background thread keeps a
#  rolling value rather than making every HTTP request block for a delta.
# --------------------------------------------------------------------------
_state = {"cpu_percent": 0.0, "net_rx_kbps": 0.0, "net_tx_kbps": 0.0}
_lock = threading.Lock()
# Set once the sampler has produced its first real delta. Without it, the
# first snapshot races the sampler and reports cpu/net as a flat 0.
_primed = threading.Event()


def _cpu_totals():
    with open("/proc/stat", "r") as fh:
        parts = [float(x) for x in fh.readline().split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)   # idle + iowait
    return idle, sum(parts)


def _net_totals():
    rx = tx = 0
    with open("/proc/net/dev", "r") as fh:
        for line in fh.readlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            # Skip loopback and the tunnels themselves, otherwise the ESP32's
            # own polling traffic shows up in the numbers it is displaying.
            if name == "lo" or name.startswith(("wg", "tailscale", "docker", "veth")):
                continue
            fields = rest.split()
            rx += int(fields[0])
            tx += int(fields[8])
    return rx, tx


def _sampler():
    prev_cpu = _cpu_totals()
    prev_net = _net_totals()
    prev_t = time.monotonic()

    while True:
        time.sleep(SAMPLE_SECONDS)
        now = time.monotonic()
        elapsed = max(now - prev_t, 1e-6)

        cur_cpu = _cpu_totals()
        d_idle = cur_cpu[0] - prev_cpu[0]
        d_total = cur_cpu[1] - prev_cpu[1]
        cpu = 100.0 * (1.0 - d_idle / d_total) if d_total > 0 else 0.0

        cur_net = _net_totals()
        rx_kbps = (cur_net[0] - prev_net[0]) / 1024.0 / elapsed
        tx_kbps = (cur_net[1] - prev_net[1]) / 1024.0 / elapsed

        with _lock:
            _state["cpu_percent"] = max(0.0, min(100.0, cpu))
            _state["net_rx_kbps"] = max(0.0, rx_kbps)
            _state["net_tx_kbps"] = max(0.0, tx_kbps)

        _primed.set()
        prev_cpu, prev_net, prev_t = cur_cpu, cur_net, now


# --------------------------------------------------------------------------
#  Point-in-time readings
# --------------------------------------------------------------------------
def _ram_percent():
    info = {}
    with open("/proc/meminfo", "r") as fh:
        for line in fh:
            key, _, value = line.partition(":")
            info[key] = float(value.split()[0])
    total = info.get("MemTotal", 0.0)
    if total <= 0:
        return 0.0
    # MemAvailable is the honest number: it accounts for reclaimable cache,
    # which is why `free -m` and a naive MemFree calculation disagree so badly.
    available = info.get("MemAvailable", info.get("MemFree", 0.0))
    return 100.0 * (total - available) / total


def _storage():
    """(used_percent, total_bytes, free_bytes) across every real filesystem.

    Reporting only `/` understates a machine whose data lives on a second disk
    - a DietPi with a USB drive would show a nearly full 4 GB card and never
    mention the 2 TB attached to it.

    The numbers follow `df`: capacity is what a normal user can occupy, so the
    5 % reserved for root is excluded from both the total and the free figure.
    That keeps the percentage, the total and the free space agreeing with each
    other, and with the tool anyone would check them against.
    """
    total = free = 0
    seen = set()
    try:
        with open("/proc/mounts", "r") as fh:
            mounts = fh.readlines()
    except OSError:
        return 0.0, 0, 0

    for line in mounts:
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mount, fstype = fields[0], fields[1], fields[2]
        # A real filesystem is backed by a block device. Everything else is
        # RAM (tmpfs), a kernel view (proc, sysfs), or another filesystem seen
        # a second time (overlay) - counting those would invent capacity that
        # does not exist.
        if not device.startswith("/dev/") or fstype in NET_FSTYPES:
            continue
        # A bind mount and its source share a device. Counting both would
        # double the disk.
        if device in seen:
            continue
        seen.add(device)
        # /proc/mounts octal-escapes the characters that would otherwise break
        # its own field separation - a mount point named "My Drive" arrives as
        # "My\040Drive" and would not exist under that name. unicode_escape
        # decodes exactly the octal form the kernel writes.
        try:
            mount = mount.encode("latin-1", "backslashreplace").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        try:
            st = os.statvfs(mount)
        except OSError:
            continue                     # unmounted underneath us, or no perms
        used = st.f_blocks - st.f_bfree
        avail = st.f_bavail
        total += (used + avail) * st.f_frsize
        free += avail * st.f_frsize

    if not total:
        return 0.0, 0, 0
    return 100.0 * (total - free) / total, total, free


def _read_zone(path):
    try:
        with open(path, "r") as fh:
            milli = float(fh.read().strip())
    except (OSError, ValueError):
        return None
    # Some zones sit at 0 until something enables them, and a few report a
    # sentinel far outside anything a chip survives.
    return milli / 1000.0 if 0 < milli < 150000 else None


def _cpu_temp_c():
    fallback = None

    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        value = _read_zone(zone + "/temp")
        if value is None:
            continue
        if fallback is None:
            fallback = value
        try:
            with open(zone + "/type", "r") as fh:
                kind = fh.read().strip().lower()
        except OSError:
            continue
        if any(hint in kind for hint in CPU_ZONE_HINTS):
            return value

    if fallback is not None:
        return fallback
    return -1.0                          # the sketch renders "--" for this


def _sysfs(path):
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _battery():
    """(percent, charging, on_ac, minutes_left) for this machine.

    A Pi has neither a battery nor a mains supply that announces itself, so
    this reports -1 and the display simply leaves it out. On a laptop it is all
    in /sys/class/power_supply.
    """
    on_ac = any(_sysfs(p) == "1"
                for pat in ("AC*/online", "ADP*/online", "ACAD/online")
                for p in glob.glob("/sys/class/power_supply/" + pat))

    bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
    if not bats:
        return -1, False, on_ac, -1

    b = bats[0]
    cap = _sysfs(b + "/capacity")
    if not cap.isdigit():
        return -1, False, on_ac, -1

    charging = _sysfs(b + "/status").lower() == "charging"

    # Energy pairs with power (µWh over µW) and charge pairs with current
    # (µAh over µA). Crossing them - energy over current - is dimensionally
    # nonsense and produces a confident, wrong number of hours.
    minutes = -1
    for amount, rate in (("energy_now", "power_now"), ("charge_now", "current_now")):
        a, r = _sysfs(b + "/" + amount), _sysfs(b + "/" + rate)
        if a.isdigit() and r.isdigit() and int(r) > 0:
            minutes = int(int(a) * 60 // int(r))
            break

    return min(100, int(cap)), charging, on_ac, minutes


def _uptime_seconds():
    with open("/proc/uptime", "r") as fh:
        return int(float(fh.readline().split()[0]))


# ==========================================================================
#  Extra sensors: GPU, NPU, fans, disk temperature, and anything you can get a
#  number out of.
#
#  Reported in the same reading the phone already understands (PROTOCOL.md,
#  section 1): the three it names itself as plain keys, everything else in a
#  "metrics" object with a label of its own. A negative number means "this
#  machine has no such sensor", so a sensor with nothing to say is left out of
#  the reading rather than sent as -1.
# ==========================================================================
MAX_EXTRAS = 24            # the phone reads at most this many per machine
MAX_CUSTOM = 16
KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Names the phone already knows. An extra sensor must never shadow one.
RESERVED = frozenset((
    "host", "age_s", "messages", "metrics",
    "cpu_percent", "ram_percent", "cpu_temp_c", "storage_percent",
    "storage_total_gb", "storage_free_gb", "uptime_seconds",
    "net_rx_kbps", "net_tx_kbps",
    "battery_percent", "battery_charging", "battery_ac", "battery_minutes",
))

# Longest suffix first, so "_mbps" is never mistaken for "_mb".
SUFFIX_UNITS = (
    ("_percent", "%"), ("_seconds", "s"), ("_mbps", "Mbps"), ("_kbps", "kbps"),
    ("_pct", "%"), ("_mhz", "MHz"), ("_ghz", "GHz"), ("_rpm", "rpm"),
    ("_mb", "MB"), ("_gb", "GB"), ("_ms", "ms"),
    ("_c", "\u00b0C"), ("_w", "W"), ("_v", "V"), ("_a", "A"),
)

# Shown on the phone under these names with no further help, so they go as
# plain keys. Everything else is sent in "metrics" with a label.
PLAIN_KEYS = frozenset(("gpu_percent", "gpu_temp_c", "npu_percent"))

LABELS = {
    "cpu_percent": "CPU", "ram_percent": "Memory", "cpu_temp_c": "CPU temperature",
    "storage_percent": "Storage", "battery_percent": "Battery",
    "net_kbps": "Network", "net_rx_kbps": "Network download",
    "net_tx_kbps": "Network upload",
    "gpu_percent": "GPU", "gpu_temp_c": "GPU temperature", "npu_percent": "NPU",
    "gpu_mem_percent": "GPU memory", "vram_used_gb": "VRAM used",
    "gpu_power_w": "GPU power", "gpu_fan_percent": "GPU fan",
    "gpu_fan_rpm": "GPU fan", "fan_rpm": "Fan", "disk_temp_c": "Disk temperature",
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
            base = base[:-len(suffix)]
            break
    return " ".join(w.capitalize() for w in base.split("_") if w) or key


def run_process(argv, timeout=5):
    """stdout of a command as text, or None if it could not run or finish.

    Catches what launching a process can actually throw and nothing else: a
    bare `except Exception` here would turn a typo into "this machine has no
    sensor" for the life of the program. Output is decoded by hand because the
    service runs with no locale, where text=True means ASCII and the first
    accented byte from a tool would raise.
    """
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout, env=clean_env())
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.decode("utf-8", "replace")


def http_get(url, timeout=3, limit=262144):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(limit).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


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
        elif (isinstance(node, list) and part.lstrip("-").isdigit()
              and -len(node) <= int(part) < len(node)):
            node = node[int(part)]
        else:
            return None
    if isinstance(node, bool):
        return None
    return to_float(node)


def robust_scale(values):
    """(median, scale) where scale is the MAD made comparable to a standard
    deviation. One spike cannot stretch its own yardstick, which a mean and
    standard deviation would let it do."""
    med = statistics.median(values)
    mad = statistics.median(abs(x - med) for x in values)
    return med, 1.4826 * mad


def _num_file(path, scale=1.0):
    """A number from a sysfs file, or None."""
    v = to_float(_sysfs(path))
    return None if v is None else v * scale


# --------------------------------------------------------------------------
#  Built-in sources. Each returns {key: float}, empty when it has nothing.
#  `root` is "/sys" and exists only so the tests can point at a fake tree.
# --------------------------------------------------------------------------
NVIDIA_QUERY = ("utilization.gpu,temperature.gpu,power.draw,"
                "memory.used,memory.total,fan.speed")


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
        return {}
    return parse_nvidia(run(
        [exe, "--query-gpu=" + NVIDIA_QUERY, "--format=csv,noheader,nounits"], 4))


def collect_amd(root="/sys"):
    """AMD GPUs through the amdgpu driver's own files: no tool to install.
    Vendor 0x1002 is AMD; Intel and NVIDIA cards are skipped here (NVIDIA is
    nvidia-smi's, and Intel's i915 publishes no load figure without perf)."""
    util, temp, power, used, total, fan = [], [], [], [], [], []
    for dev in sorted(glob.glob(root + "/class/drm/card[0-9]*/device")):
        if _sysfs(dev + "/vendor").lower() != "0x1002":
            continue
        for lst, name, scale in ((util, "gpu_busy_percent", 1.0),
                                 (used, "mem_info_vram_used", 1.0),
                                 (total, "mem_info_vram_total", 1.0)):
            v = _num_file(dev + "/" + name, scale)
            if v is not None:
                lst.append(v)
        for hw in sorted(glob.glob(dev + "/hwmon/hwmon*")):
            t = _num_file(hw + "/temp1_input", 0.001)
            if t is not None and 0 < t < 150:
                temp.append(t)
            p = _num_file(hw + "/power1_average", 1e-6)
            if p is None:
                p = _num_file(hw + "/power1_input", 1e-6)
            if p is not None and p >= 0:
                power.append(p)
            f = _num_file(hw + "/fan1_input")
            if f is not None and f > 0:
                fan.append(f)
    out = {}
    if util:
        out["gpu_percent"] = max(0.0, min(100.0, max(util)))
    if temp:
        out["gpu_temp_c"] = max(temp)
    if power:
        out["gpu_power_w"] = sum(power)
    if used:
        out["vram_used_gb"] = sum(used) / (1024.0 ** 3)
        if total and sum(total) > 0:
            out["gpu_mem_percent"] = 100.0 * sum(used) / sum(total)
    if fan:
        out["gpu_fan_rpm"] = max(fan)
    return out


def collect_hwmon(root="/sys"):
    """Case fans and disk temperatures, from the kernel's hwmon interface."""
    fans, disks = [], []
    for hw in sorted(glob.glob(root + "/class/hwmon/hwmon*")):
        name = _sysfs(hw + "/name")
        if name == "amdgpu":
            continue                    # a GPU fan is the GPU's, not the case's
        for f in glob.glob(hw + "/fan[0-9]*_input"):
            v = _num_file(f)
            if v is not None and v > 0:
                fans.append(v)
        if name in ("nvme", "drivetemp"):
            t = _num_file(hw + "/temp1_input", 0.001)
            if t is not None and 0 < t < 150:
                disks.append(t)
    out = {}
    if fans:
        out["fan_rpm"] = max(fans)
    if disks:
        out["disk_temp_c"] = max(disks)
    return out


class NpuReader:
    """Intel NPU load, from the intel_vpu driver (kernel 6.11 and later).

    The driver publishes npu_busy_time_us: microseconds spent executing jobs,
    counted only while jobs are submitted. Utilisation is therefore the change
    in that counter over the change in wall time, so the first read only sets a
    baseline. The driver asks to be read about once a second at most; this is
    read every ten.

    Other vendors' NPUs publish nothing comparable that an unprivileged
    service can read (Rockchip's is in debugfs, root only), and are not guessed
    at: they can be added as a custom sensor.
    """

    def __init__(self, root="/sys", clock=time.monotonic):
        self.root, self._clock = root, clock
        self._prev = {}
        self.present = False

    def read(self):
        paths = (glob.glob(self.root + "/class/accel/accel*/device/npu_busy_time_us")
                 + glob.glob(self.root + "/bus/pci/drivers/intel_vpu/*/npu_busy_time_us"))
        seen, best = set(), None
        self.present = False
        for p in sorted(paths):
            real = os.path.realpath(p)
            if real in seen:
                continue
            seen.add(real)
            busy = _num_file(p)
            if busy is None:
                continue
            self.present = True
            now = self._clock()
            prev = self._prev.get(real)
            self._prev[real] = (busy, now)
            if prev and now > prev[1] and busy >= prev[0]:
                pct = 100.0 * (busy - prev[0]) / 1e6 / (now - prev[1])
                best = max(best if best is not None else 0.0, min(100.0, pct))
        return {"npu_percent": best} if best is not None else {}


# --------------------------------------------------------------------------
#  Custom sensors
# --------------------------------------------------------------------------
SOURCES = ("command", "file", "http")


def clean_custom(raw):
    """Validate the "sensors" list from alerts.json -> (clean list, warnings).

    A hand-edited file will have mistakes in it. A bad entry is dropped with a
    reason, not allowed to stop the agent or the sensors beside it.
    """
    out, warns, seen = [], [], set()
    if not isinstance(raw, list):
        return out, warns
    for i, r in enumerate(raw[:MAX_CUSTOM * 2]):
        tag = "sensor #%d" % (i + 1)
        if not isinstance(r, dict):
            warns.append("%s: not an object" % tag)
            continue
        key = str(r.get("key", "")).strip().lower()
        tag = "sensor '%s'" % (key or i + 1)
        if not KEY_RE.match(key):
            warns.append("%s: key must be a-z, 0-9 and _ (32 max)" % tag)
            continue
        if key in RESERVED:
            warns.append("%s: '%s' is already a built-in reading" % (tag, key))
            continue
        if key in seen:
            warns.append("%s: duplicate key" % tag)
            continue
        src = str(r.get("source", "")).strip().lower()
        if src not in SOURCES:
            warns.append("%s: source must be one of %s" % (tag, ", ".join(SOURCES)))
            continue
        s = {"key": key, "source": src}

        if src == "command":
            cmd = r.get("command")
            if isinstance(cmd, str):
                try:
                    cmd = shlex.split(cmd)
                except ValueError:
                    cmd = None
            if not (isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd)):
                warns.append("%s: command is empty or malformed" % tag)
                continue
            s["command"] = cmd
        elif src == "file":
            if not str(r.get("path", "")).strip():
                warns.append("%s: path is empty" % tag)
                continue
            s["path"] = str(r["path"]).strip()
        else:
            url = str(r.get("url", "")).strip()
            if not url.lower().startswith(("http://", "https://")):
                warns.append("%s: url must start with http:// or https://" % tag)
                continue
            s["url"] = url

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
    if src == "file":
        try:
            with open(spec["path"], "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(65536)
        except OSError as e:
            return None, "cannot read file: %s" % (e.strerror or e)
    elif src == "http":
        text = fetch(spec["url"])
        if text is None:
            return None, "no answer from the URL"
    else:
        text = run(spec["command"], 6)
        if text is None:
            return None, "command did not run or took over 6 s"
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
PRIORITY = {"custom": 0, "nvidia": 1, "amd": 2, "hwmon": 3, "npu": 3}


class ExtraSensors:
    """Samples in its own thread so a reading never waits on a process launch.

    A source that keeps returning nothing is asked less and less often, up to
    every five minutes, so a machine with no NVIDIA card is not launching
    nvidia-smi forever.
    """

    def __init__(self, enabled=True, interval=10.0, custom=None, run=run_process,
                 fetch=http_get, which=shutil.which, root="/sys", clock=time.monotonic):
        self.enabled = enabled
        self.interval = max(2.0, float(interval))
        self.custom = list(custom or [])
        self._run, self._fetch, self._which, self._root = run, fetch, which, root
        self._clock = clock
        self._npu = NpuReader(root, clock)
        self._vals = {}               # key -> {source: (value, expires)}
        self._errors = {}             # custom key -> last error
        self._due = {}
        self._miss = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def configure(self, enabled, interval, custom):
        """New settings from a reloaded alerts.json. A sensor that was removed
        stops being reported at once rather than lingering until it expires."""
        with self._lock:
            self.enabled = enabled
            self.interval = max(2.0, float(interval))
            keep = {s["key"] for s in custom}
            for k in [k for k, srcs in self._vals.items() if "custom" in srcs and k not in keep]:
                self._vals[k].pop("custom", None)
            for k in [k for k in self._errors if k not in keep]:
                del self._errors[k]
            self.custom = list(custom)

    def start(self):
        self._stop.set()                              # a fresh event per start,
        stop = self._stop = threading.Event()         # so a restart never races
        threading.Thread(target=self._loop, args=(stop,), daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self, stop):
        while not stop.is_set():
            self.collect_once()
            stop.wait(1.0)

    def _store(self, source, readings, now, every):
        exp = now + 3 * every + 5
        with self._lock:
            for k, v in readings.items():
                if v is not None and math.isfinite(v):
                    self._vals.setdefault(k, {})[source] = (v, exp)

    def _after(self, name, now, got, base):
        miss = 0 if got else self._miss.get(name, 0) + 1
        self._miss[name] = miss
        self._due[name] = now + (min(300.0, base * (1 + miss)) if miss else base)

    def collect_once(self, force=False):
        """Run whatever is due. `force` runs everything now (for --once)."""
        now = self._clock()
        if self.enabled:
            base = self.interval
            sources = (
                ("nvidia", lambda: collect_nvidia(self._run, self._which)),
                ("amd", lambda: collect_amd(self._root)),
                ("hwmon", lambda: collect_hwmon(self._root)),
                ("npu", self._npu.read),
            )
            for name, fn in sources:
                if not (force or now >= self._due.get(name, 0.0)):
                    continue
                r = fn()
                self._store(name, r, now, base)
                # An NPU's first read only sets a baseline and returns nothing;
                # that is the device being there, not a source with no answer.
                present = bool(r) or (name == "npu" and self._npu.present)
                self._after(name, now, present, base)

        for spec in list(self.custom):
            name = "c:" + spec["key"]
            if not (force or now >= self._due.get(name, 0.0)):
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
        """Human-readable lines: what is being read, from where, and what failed."""
        fresh = self._fresh()
        specs = dict((s["key"], s) for s in self.custom)
        lines = []
        for k in sorted(fresh):
            v, src = fresh[k]
            spec = specs.get(k, {})
            lines.append(("%-20s %g %s" % (spec.get("label") or label_for_key(k), v,
                                           spec.get("unit") or unit_for_key(k))).rstrip()
                         + "   (%s)" % src)
        for k, err in sorted(self._errors.items()):
            lines.append("%-20s -  %s" % (k, err))
        return lines


# ==========================================================================
#  Messages for the phone.
#
#  The phone reads a "messages" list inside each reading (PROTOCOL.md section
#  2). This is the machine's side of that: an outbox that every reading carries
#  a window of, and three ways for a program on this machine to put something
#  in it.
#
#      1. a loopback HTTP endpoint   POST http://127.0.0.1:6598/notify  (needs the key)
#      2. a watched folder           drop a .json file in /var/lib/peekesp/outbox
#      3. the command line           peekesp notify --severity red --title ...
#
#  A reading is "the latest state", not a queue: a phone that looks every
#  fifteen minutes with the app closed would miss a message sent once. So a
#  message stays in every reading for PEEK_MESSAGE_TTL_HOURS (six by default),
#  and its id stops it becoming a second alert each time it is re-sent.
# ==========================================================================
SEVERITIES = ("info", "warning", "critical")
_RANK = {"info": 0, "warning": 1, "critical": 2}

# Exactly the words PROTOCOL.md promises the phone understands.
_CRITICAL = frozenset(("critical", "red", "high", "alert", "emergency", "fatal", "error"))
_WARNING = frozenset(("warning", "yellow", "amber", "orange", "warn", "medium"))

TITLE_MAX, BODY_MAX, SOURCE_MAX = 80, 400, 40
MAX_PER_READING = 10           # the phone reads at most this many
MAX_STORED = 100
RATE_PER_MINUTE = 20           # across every outside channel, like the phone
DEFAULT_TTL_HOURS = 6
MAX_BODY_BYTES = 16384
NOTIFY_PORT = 6598

_ID_OK = re.compile(r"^[A-Za-z0-9._:@-]{1,44}$")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def is_known_severity(v):
    s = str(v or "").strip().lower()
    return s in _CRITICAL or s in _WARNING or s == "info"


def norm_severity(v):
    s = str(v or "").strip().lower()
    if s in _CRITICAL:
        return "critical"
    if s in _WARNING:
        return "warning"
    return "info"


def sev_rank(severity):
    return _RANK.get(severity, 0)


def _clip(v, n, one_line=False):
    s = _CTRL.sub("", str(v if v is not None else ""))
    if one_line:
        s = " ".join(s.split())
    return s.strip()[:n]


def _ts(v, now):
    try:
        t = float(v)
    except (TypeError, ValueError):
        return int(now)
    if t > 1e11:                    # milliseconds
        t /= 1000.0
    if t <= 0 or t > now + 86400:
        return int(now)
    return int(t)


def build_message(raw, now, default_source="Local app"):
    """One message from a loose dict, or None if it says nothing."""
    if not isinstance(raw, dict):
        return None
    body = _clip(raw.get("body", raw.get("message", "")), BODY_MAX)
    title = _clip(raw.get("title", ""), TITLE_MAX, one_line=True)
    if not title and body:
        title = _clip(body, TITLE_MAX, one_line=True)
    if not title and not body:
        return None
    rid = str(raw.get("id", "") or "").strip()
    return {
        "id": rid if _ID_OK.match(rid) else (
            hashlib.sha1(rid.encode("utf-8", "replace")).hexdigest()[:20] if rid else ""),
        "severity": norm_severity(raw.get("severity")),
        "title": title,
        "body": body,
        "source": _clip(raw.get("source", ""), SOURCE_MAX, one_line=True) or default_source,
        "ts": _ts(raw.get("ts"), now),
    }


def parse_payload(obj):
    """A dict, a list of dicts, or {"messages": [...]} -> list of dicts."""
    if isinstance(obj, dict) and isinstance(obj.get("messages"), list):
        obj = obj["messages"]
    if isinstance(obj, dict):
        obj = [obj]
    if not isinstance(obj, list):
        return []
    return [m for m in obj[:MAX_PER_READING] if isinstance(m, dict)]


class Outbox:
    """Recent messages, kept in memory and mirrored to a file so an agent
    restart does not drop an alert that is still inside its window. With no
    path (nowhere writable) it is memory only, and says so elsewhere."""

    def __init__(self, path=None, ttl_hours=DEFAULT_TTL_HOURS, host="", clock=time.time):
        self.path = path
        self.ttl = max(1.0, float(ttl_hours)) * 3600.0
        self.host = host
        self._clock = clock
        self._lock = threading.RLock()
        self._msgs = []              # oldest first; each also carries "at"
        self._listeners = []
        self._stamps = []
        self.save_error = ""
        self.listener_error = ""
        self._load()

    def configure(self, ttl_hours=None):
        with self._lock:
            if ttl_hours is not None:
                self.ttl = max(1.0, float(ttl_hours)) * 3600.0

    def on_new(self, callback):
        """callback(message) runs once for each message first seen, never for a
        re-send and never for one restored from disk."""
        with self._lock:
            self._listeners.append(callback)

    def add(self, raw, source=None, internal=False):
        """-> (status, message). status is new, duplicate, rate or empty.

        `internal` skips the per-minute limit: it is there to stop another
        program flooding the phone, not to stop this agent's own rules.
        """
        now = self._clock()
        m = build_message(raw, now, source or "Local app")
        if m is None:
            return "empty", None
        with self._lock:
            self._prune(now)
            base = m["id"] or uuid.uuid4().hex[:12]
            m["id"] = ("%s:%s" % (self.host, base))[:64] if self.host else base[:64]
            for e in self._msgs:
                if e["id"] == m["id"]:
                    return "duplicate", self._public(e)
            if not internal:
                self._stamps = [t for t in self._stamps if now - t < 60.0]
                if len(self._stamps) >= RATE_PER_MINUTE:
                    return "rate", None
                self._stamps.append(now)
            m["at"] = now
            self._msgs.append(m)
            del self._msgs[:-MAX_STORED]
            self._save()
            listeners = list(self._listeners)
            out = self._public(m)
        for cb in listeners:
            try:
                cb(dict(out))
            except Exception as e:        # a broken listener must not lose the message
                self.listener_error = repr(e)
        return "new", out

    @staticmethod
    def _public(m):
        d = {"id": m["id"], "severity": m["severity"], "title": m["title"],
             "source": m["source"], "ts": m["ts"]}
        if m.get("body"):
            d["body"] = m["body"]
        return d

    def active(self, limit=MAX_PER_READING):
        """What goes in a reading: most severe first, then newest."""
        with self._lock:
            self._prune(self._clock())
            ordered = sorted(self._msgs, key=lambda m: (-sev_rank(m["severity"]), -m["ts"]))
            return [self._public(m) for m in ordered[:limit]]

    def recent(self, n=20):
        with self._lock:
            return [self._public(m) for m in reversed(self._msgs[-n:])]

    def _prune(self, now):
        keep = [m for m in self._msgs if now - m["at"] <= self.ttl]
        if len(keep) != len(self._msgs):
            self._msgs = keep
            self._save()

    def _save(self):
        if not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"messages": self._msgs}, fh)
            os.replace(tmp, self.path)
            self.save_error = ""
        except OSError as e:
            self.save_error = str(e)

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        now = self._clock()
        for m in data.get("messages", []) if isinstance(data, dict) else []:
            if (isinstance(m, dict) and m.get("id") and m.get("severity") in SEVERITIES
                    and isinstance(m.get("at"), (int, float)) and now - m["at"] <= self.ttl):
                self._msgs.append({
                    "id": str(m["id"])[:64], "severity": m["severity"],
                    "title": _clip(m.get("title"), TITLE_MAX, True),
                    "body": _clip(m.get("body"), BODY_MAX),
                    "source": _clip(m.get("source"), SOURCE_MAX, True) or "Local app",
                    "ts": int(m.get("ts") or m["at"]), "at": float(m["at"])})


# --------------------------------------------------------------------------
#  Where things live
# --------------------------------------------------------------------------
def state_dir():
    """A writable directory for the outbox, history and key, or None.

    Under systemd this is /var/lib/peekesp, created by StateDirectory= in the
    unit. A unit without it (an older install, or a package that ships its own)
    has a read-only /var, and the agent then runs with an outbox in memory and
    no local endpoint rather than failing - see Services.start().
    """
    cands = []
    if os.environ.get("PEEK_STATE_DIR"):
        cands.append(os.environ["PEEK_STATE_DIR"])
    if os.environ.get("STATE_DIRECTORY"):
        cands.append(os.environ["STATE_DIRECTORY"].split(":")[0])
    cands.append("/var/lib/peekesp")
    home = os.environ.get("HOME", "")
    if home and home != "/":
        cands.append(os.path.join(home, ".local", "state", "peekesp"))
    for d in cands:
        try:
            os.makedirs(d, mode=0o750, exist_ok=True)
        except OSError:
            continue
        if os.access(d, os.W_OK):
            return d
    return None


def outbox_dir(state):
    return os.path.join(state, "outbox")


def ensure_outbox_dir(state):
    """The drop folder, group-writable and setgid: a member of the peekesp group
    can leave a file for the service without being root, and the file arrives
    owned by that group so the service can read it."""
    d = outbox_dir(state)
    try:
        os.makedirs(d, mode=0o2770, exist_ok=True)
        os.chmod(d, 0o2770)
    except OSError:
        pass
    return d


def load_or_create_key(state):
    """The local endpoint's key, kept in the state directory so it survives a
    restart: a key that changed on every start would break every program that
    had been told it. Mode 0640, group peekesp - root and group members can
    read it, nobody else."""
    path = os.path.join(state, "notify.key")
    try:
        with open(path, "r") as fh:
            key = fh.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = binascii.hexlify(os.urandom(24)).decode("ascii")
    old = os.umask(0o027)
    try:
        tmp = path + ".new"
        with open(tmp, "w") as fh:
            fh.write(key + "\n")
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    finally:
        os.umask(old)
    return key


def drop_file(folder, raw):
    """Write one message where the watcher will find it. Written under a
    temporary name and renamed, so the watcher never reads half a file."""
    os.makedirs(folder, exist_ok=True)
    name = "%d-%s" % (int(time.time() * 1000), uuid.uuid4().hex[:8])
    tmp = os.path.join(folder, name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
    with os.fdopen(fd, "w") as fh:
        json.dump(raw, fh)
    os.chmod(tmp, 0o660)                           # whatever the umask said
    if os.geteuid() == 0:
        # Root's file would otherwise be root's alone, and the service could not
        # read it. The directory is setgid, so a non-root writer's files already
        # arrive in the right group.
        try:
            import pwd
            u = pwd.getpwnam(SERVICE_USER)
            os.chown(tmp, u.pw_uid, u.pw_gid)
        except (KeyError, OSError):
            pass
    final = os.path.join(folder, name + ".json")
    os.replace(tmp, final)
    return final


SERVICE_USER = "peekesp"


class OutboxWatcher:
    """Polls a folder every couple of seconds; stdlib has no file-change API."""

    def __init__(self, outbox, folder, every=2.0):
        self.outbox, self.folder, self.every = outbox, folder, every
        self._stop = threading.Event()

    def start(self):
        self._stop.set()
        stop = self._stop = threading.Event()
        threading.Thread(target=self._loop, args=(stop,), daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self, stop):
        while not stop.wait(self.every):
            self.scan()

    def _reject(self, path):
        try:
            d = os.path.join(self.folder, "rejected")
            os.makedirs(d, exist_ok=True)
            os.replace(path, os.path.join(d, os.path.basename(path)))
        except OSError:
            pass

    def scan(self):
        try:
            files = sorted(f for f in os.listdir(self.folder) if f.endswith(".json"))[:20]
        except OSError:
            return
        for name in files:
            path = os.path.join(self.folder, name)
            try:
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                if os.path.getsize(path) > MAX_BODY_BYTES:
                    self._reject(path)
                    continue
                with open(path, "r", encoding="utf-8") as fh:
                    obj = json.load(fh)
            except ValueError:
                # Either a bad file or one a naive tool is still writing in
                # place. Give it a few seconds before calling it bad.
                try:
                    if time.time() - os.path.getmtime(path) > 5:
                        self._reject(path)
                except OSError:
                    pass
                continue
            except OSError:
                continue

            msgs = parse_payload(obj)
            limited = False
            for i, raw in enumerate(msgs):
                raw = dict(raw)
                if not raw.get("id"):
                    # Stable across a retry of this same file, so a file that
                    # is only half-processed cannot ring twice.
                    raw["id"] = "f" + hashlib.sha1(
                        ("%s/%d" % (name, i)).encode()).hexdigest()[:16]
                status, _m = self.outbox.add(raw, source="Local app")
                if status == "rate":
                    limited = True
                    break
            if limited:
                return                   # leave it; the next scan tries again
            if not msgs:
                self._reject(path)
                continue
            try:
                os.unlink(path)
            except OSError:
                pass


def _make_handler(outbox, key, port):
    allowed_hosts = set(("127.0.0.1:%d" % port, "localhost:%d" % port,
                         "127.0.0.1", "localhost"))
    key_b = key.encode("utf-8")

    class NotifyHandler(BaseHTTPRequestHandler):
        timeout = 5                                  # a stalled client cannot hold a thread

        def _reply(self, code, obj=None):
            body = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # A web page can make a browser POST to localhost. It cannot add an
            # Authorization header without a preflight we never answer, and a
            # DNS-rebinding page arrives with the wrong Host. Both are refused
            # before anything is read.
            if self.headers.get("Origin"):
                return self._reply(403)
            if (self.headers.get("Host") or "").lower() not in allowed_hosts:
                return self._reply(403)
            if self.path.split("?")[0] != "/notify":
                return self._reply(404)

            auth = self.headers.get("Authorization", "")
            supplied = auth[7:] if auth.lower().startswith("bearer ") \
                else self.headers.get("X-Peek-Key", "")
            if not hmac.compare_digest(supplied.strip().encode("utf-8", "replace"), key_b):
                return self._reply(401)

            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            if n <= 0:
                return self._reply(400, {"error": "empty body"})
            if n > MAX_BODY_BYTES:
                return self._reply(413, {"error": "too large"})
            try:
                msgs = parse_payload(json.loads(self.rfile.read(n).decode("utf-8", "replace")))
            except ValueError:
                return self._reply(400, {"error": "body is not JSON"})
            if not msgs:
                return self._reply(400, {"error": "no message found"})

            ids, rated = [], False
            for raw in msgs:
                status, m = outbox.add(raw, source="External app")
                if status == "rate":
                    rated = True
                    break
                if m:
                    ids.append(m["id"])
            if rated and not ids:
                return self._reply(429, {"error": "too many messages"})
            if not ids:
                return self._reply(400, {"error": "no message found"})
            self._reply(200, {"ok": True, "accepted": len(ids), "ids": ids})

        def log_message(self, *args):
            pass

    return NotifyHandler


class NotifyServer:
    """Loopback only. Binding 127.0.0.1 rather than 0.0.0.0 is the point: the
    key stops other programs on this machine, the bind stops everything else."""

    def __init__(self, outbox, key, port):
        self.outbox, self.key, self.port = outbox, key, port
        self._httpd = None
        self.error = ""

    def start(self):
        try:
            self._httpd = ThreadingHTTPServer(
                ("127.0.0.1", self.port), _make_handler(self.outbox, self.key, self.port))
        except OSError as e:
            self.error = "port %d: %s" % (self.port, e.strerror or e)
            return False
        threading.Thread(target=self._httpd.serve_forever,
                         kwargs={"poll_interval": 0.4}, daemon=True).start()
        self.error = ""
        return True

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except OSError:
                pass
            self._httpd = None


# ==========================================================================
#  Rules that watch this machine and leave a message when one trips.
#
#  The phone has rules of its own and judges them on whatever it last read.
#  These run here, on every sample, whether or not the phone is looking - so a
#  spike at 3 a.m. is caught while the phone is asleep and is waiting in the
#  next reading. Use one or the other for a given condition: both firing means
#  two notifications.
#
#  What this cannot do is notice that THIS machine went offline: a machine
#  that has stopped cannot report it. That one is the phone's - it sees the
#  relay go quiet. The nearest thing available here is the relay link, which
#  the journal and the hook (below) report when it drops and when it returns.
# ==========================================================================
OPS = ("above", "below", "unusual")
MAX_RULES = 50
CLEAR_HOLD = 30.0             # seconds a reading must stay normal before "recovered"
MIN_HISTORY = 360             # one-minute buckets: six hours
HIST_KEEP = 1440              # and a day
UNUSUAL_K = 5.0               # how many robust deviations counts as unusual
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DEFAULT_QUIET = {
    "enabled": False, "start": "22:00", "end": "07:00",
    "days": [0, 1, 2, 3, 4, 5, 6],             # Monday = 0; the day the window begins
    "allow_critical": True,
}

_DEFAULT_RULES = (
    ("cpu_high", "cpu_percent", "above", 90, 300, "warning", {}),
    ("ram_high", "ram_percent", "above", 90, 300, "warning", {}),
    ("cpu_hot", "cpu_temp_c", "above", 85, 60, "critical", {}),
    ("storage_full", "storage_percent", "above", 92, 0, "warning", {}),
    ("battery_low", "battery_percent", "below", 20, 0, "warning", {"unplugged": True}),
    ("battery_critical", "battery_percent", "below", 8, 0, "critical", {"unplugged": True}),
    ("net_unusual", "net_kbps", "unusual", 0, 300, "warning", {"min": 2000.0}),
)


def default_rules():
    out = []
    for rid, metric, op, limit, hold, sev, extra in _DEFAULT_RULES:
        r = {"id": rid, "enabled": True, "metric": metric, "op": op, "limit": float(limit),
             "min": 0.0, "for": hold, "severity": sev, "unplugged": False,
             "recovery": True, "title": "", "body": ""}
        r.update(extra)
        out.append(r)
    return out


def _hhmm(s):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(s or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return "%02d:%02d" % (h, mi) if h < 24 and mi < 60 else None


def clean_quiet(raw):
    q = copy.deepcopy(DEFAULT_QUIET)
    warns = []
    if not isinstance(raw, dict):
        return q, warns
    q["enabled"] = bool(raw.get("enabled", False))
    for k in ("start", "end"):
        if k in raw:
            v = _hhmm(raw[k])
            if v:
                q[k] = v
            else:
                warns.append("quiet hours: %s must look like 22:00" % k)
    days = raw.get("days", q["days"])
    if isinstance(days, list):
        q["days"] = sorted(set(int(x) for x in days
                               if isinstance(x, (int, float)) and 0 <= int(x) <= 6))
    q["allow_critical"] = bool(raw.get("allow_critical", True))
    return q, warns


def clean_rule(raw, i, taken):
    """-> (rule or None, warning or None)."""
    if not isinstance(raw, dict):
        return None, "rule #%d: not an object" % (i + 1)
    metric = str(raw.get("metric", "")).strip().lower()
    if not KEY_RE.match(metric):
        return None, "rule #%d: metric must be a-z, 0-9 and _" % (i + 1)
    op = str(raw.get("op", "")).strip().lower()
    if op not in OPS:
        return None, "rule '%s': op must be above, below or unusual" % metric
    try:
        limit = float(raw.get("limit", 0.0))
        if op != "unusual" and (not math.isfinite(limit) or "limit" not in raw):
            raise ValueError
    except (TypeError, ValueError):
        return None, "rule '%s': a numeric limit is needed" % metric
    try:
        floor = float(raw.get("min", 0.0))
    except (TypeError, ValueError):
        floor = 0.0
    try:
        hold = int(max(0, min(86400, float(raw.get("for", 0)))))
    except (TypeError, ValueError):
        hold = 0
    rid = re.sub(r"[^a-z0-9_-]", "", str(raw.get("id", "")).lower())[:32] or "rule%d" % (i + 1)
    base, n = rid, 2
    while rid in taken:
        rid = "%s-%d" % (base, n)
        n += 1
    taken.add(rid)
    return {
        "id": rid, "enabled": bool(raw.get("enabled", True)), "metric": metric,
        "op": op, "limit": limit if math.isfinite(limit) else 0.0,
        "min": floor if math.isfinite(floor) else 0.0, "for": hold,
        "severity": norm_severity(raw.get("severity", "warning")),
        "unplugged": bool(raw.get("unplugged", False)),
        "recovery": bool(raw.get("recovery", True)),
        "title": str(raw.get("title", "") or "")[:120],
        "body": str(raw.get("body", "") or "")[:400],
    }, None


def clean_config(raw):
    """The whole of alerts.json -> (clean dict, warnings).

    Missing keys mean the defaults. A rules list that is present but empty means
    the person deleted them all, and stays empty.
    """
    cfg = {"rules": default_rules(), "quiet": copy.deepcopy(DEFAULT_QUIET), "hook": [],
           "hook_min_severity": "warning", "sensors": []}
    warns = []
    if not isinstance(raw, dict):
        return cfg, ["alerts.json must be a JSON object"]
    cfg["quiet"], w = clean_quiet(raw.get("quiet"))
    warns += w
    if isinstance(raw.get("rules"), list):
        taken, rules = set(), []
        for i, r in enumerate(raw["rules"][:MAX_RULES]):
            rule, warn = clean_rule(r, i, taken)
            if rule:
                rules.append(rule)
            else:
                warns.append(warn)
        cfg["rules"] = rules
    hook = raw.get("hook")
    if isinstance(hook, str):
        try:
            hook = shlex.split(hook)
        except ValueError:
            hook = None
            warns.append("hook: could not read the command line")
    if isinstance(hook, list) and hook and all(isinstance(a, str) for a in hook):
        cfg["hook"] = hook
    elif hook not in (None, [], ""):
        warns.append("hook must be a command: [\"/path/to/program\", \"arg\"]")
    if "hook_min_severity" in raw:
        if is_known_severity(raw["hook_min_severity"]):
            cfg["hook_min_severity"] = norm_severity(raw["hook_min_severity"])
        else:
            warns.append("hook_min_severity must be info, warning or critical")
    cfg["sensors"], w = clean_custom(raw.get("sensors"))
    return cfg, warns + w


# --------------------------------------------------------------------------
#  Quiet hours
# --------------------------------------------------------------------------
def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet(q, dt):
    """Is dt inside the quiet window? `days` are the days the window begins, so
    a Friday 22:00-07:00 window still covers Saturday at 03:00."""
    if not q.get("enabled"):
        return False
    s, e = _minutes(q["start"]), _minutes(q["end"])
    now_m = dt.hour * 60 + dt.minute
    wd, days = dt.weekday(), q.get("days", [])
    if s == e:
        return False
    if s < e:
        return wd in days and s <= now_m < e
    if now_m >= s:
        return wd in days
    if now_m < e:
        return (wd - 1) % 7 in days
    return False


def hook_policy(severity, min_severity, quiet, dt):
    """Should the hook run for a message of this severity right now?

    Quiet hours silence the hook, not the phone: that has quiet hours of its
    own, and a message dropped here would never reach its Alerts tab at all.
    """
    if sev_rank(severity) < sev_rank(min_severity):
        return False
    if in_quiet(quiet, dt) and not (severity == "critical" and quiet.get("allow_critical", True)):
        return False
    return True


# --------------------------------------------------------------------------
#  Turning a reading into numbers a rule can use
# --------------------------------------------------------------------------
def flatten(snap):
    """A reading -> (values, meta). meta maps a key to (label, unit) for the
    ones that said so themselves; the rest are named from their keys."""
    flat, meta = {}, {}
    for k, v in snap.items():
        if isinstance(v, bool):
            flat[k] = 1.0 if v else 0.0
        elif isinstance(v, (int, float)) and math.isfinite(v):
            flat[k] = float(v)
    m = snap.get("metrics")
    if isinstance(m, dict):
        for k, v in m.items():
            if isinstance(v, dict):
                val = v.get("value")
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    flat[k] = float(val)
                    meta[k] = (str(v.get("label") or label_for_key(k)),
                               str(v.get("unit") or unit_for_key(k)))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                flat[k] = float(v)
    rx, tx = flat.get("net_rx_kbps", -1.0), flat.get("net_tx_kbps", -1.0)
    if rx >= 0 and tx >= 0:
        flat["net_kbps"] = rx + tx
    return flat, meta


def _num(v):
    s = ("%.1f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def fmt_value(v, unit):
    if not unit:
        return _num(v)
    return _num(v) + (unit if unit in ("%", "\u00b0C", "\u00b0F") else " " + unit)


def fmt_duration(sec):
    sec = int(sec)
    if sec < 60:
        return "%d s" % sec
    if sec < 5400:
        return "%d min" % round(sec / 60.0)
    h, m = divmod(int(round(sec / 60.0)), 60)
    return "%d h" % h + (" %d min" % m if m else "")


def describe_rule(r):
    """One line for the rules list: "CPU above 90% for 5 min - warning"."""
    label, unit = label_for_key(r["metric"]), unit_for_key(r["metric"])
    if r["op"] == "above":
        what = "%s above %s" % (label, fmt_value(r["limit"], unit))
    elif r["op"] == "below":
        what = "%s below %s" % (label, fmt_value(r["limit"], unit))
    else:
        what = "%s unusually high" % label
        if r["min"]:
            what += " (over %s)" % fmt_value(r["min"], unit)
    if r["for"]:
        what += " for " + fmt_duration(r["for"])
    what += " - " + r["severity"]
    if r["unplugged"]:
        what += ", on battery only"
    return what


def fill(template, ctx):
    """{name} substitution. Done by hand rather than str.format so a template
    cannot reach into attributes, and an unknown {name} is left as written."""
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), m.group(0))), template)


class AlertEngine:
    def __init__(self, values_fn, outbox, host, history_path=None, clock=time.time,
                 interval=5.0):
        self.values_fn, self.outbox, self.host = values_fn, outbox, host
        self.history_path = history_path
        self._clock, self.interval = clock, interval
        self.state = {}               # rule id -> episode bookkeeping; survives configure()
        self.hist = {}                # metric -> [(minute, mean)]
        self._acc = {}                # metric -> [minute, sum, n]
        self.last_error = ""
        self.rules = []
        self._tracked = set()
        self._stop = threading.Event()
        self._last_save = 0.0
        self._load_history()

    def configure(self, rules, enabled=True):
        """Swap in new rules without forgetting which are mid-incident, so
        editing the file during an alert does not make the phone ring again."""
        self.rules = [r for r in rules if r["enabled"]] if enabled else []
        self._tracked = set(r["metric"] for r in self.rules if r["op"] == "unusual")
        live = set(r["id"] for r in self.rules)
        self.state = dict((k, v) for k, v in self.state.items() if k in live or v.get("active"))

    def start(self):
        self._stop.set()
        stop = self._stop = threading.Event()
        threading.Thread(target=self._loop, args=(stop,), daemon=True).start()

    def stop(self):
        self._stop.set()
        self._save_history()

    def _loop(self, stop):
        while not stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:         # keep watching; say so, do not die quietly
                msg = "%s: %s" % (type(e).__name__, e)
                if msg != self.last_error:
                    print("alert rules: " + msg, flush=True)
                self.last_error = msg

    def _record(self, flat, now):
        minute = int(now // 60)
        for k in self._tracked:
            v = flat.get(k)
            if v is None or v < 0:
                continue
            acc = self._acc.get(k)
            if acc and acc[0] != minute:
                h = self.hist.setdefault(k, [])
                h.append((acc[0], acc[1] / acc[2]))
                del h[:-HIST_KEEP]
                acc = None
            if acc is None:
                acc = self._acc[k] = [minute, 0.0, 0]
            acc[1] += v
            acc[2] += 1
        if now - self._last_save > 300:
            self._last_save = now
            self._save_history()

    def _save_history(self):
        if not self.history_path or not self.hist:
            return
        try:
            tmp = self.history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(dict((k, [[a, round(b, 2)] for a, b in v])
                               for k, v in self.hist.items()), fh)
            os.replace(tmp, self.history_path)
        except OSError:
            pass

    def _load_history(self):
        if not self.history_path:
            return
        try:
            with open(self.history_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        floor = int(self._clock() // 60) - HIST_KEEP
        for k, rows in (data.items() if isinstance(data, dict) else []):
            if isinstance(rows, list) and KEY_RE.match(str(k)):
                self.hist[k] = [(int(a), float(b)) for a, b in rows
                                if isinstance(a, (int, float)) and isinstance(b, (int, float))
                                and a >= floor][-HIST_KEEP:]

    def _condition(self, rule, v, flat):
        """(is the rule's condition true, typical value for the message)."""
        if v is None or v < 0:                    # no such sensor
            return False, None
        if rule["unplugged"] and (flat.get("battery_ac", 0.0) >= 0.5
                                  or flat.get("battery_charging", 0.0) >= 0.5):
            return False, None
        op = rule["op"]
        if op == "above":
            return v > rule["limit"], None
        if op == "below":
            return v < rule["limit"], None
        vals = [x for _m, x in self.hist.get(rule["metric"], [])]
        if len(vals) < MIN_HISTORY or v < rule["min"]:
            return False, None
        med, scale = robust_scale(vals)
        scale = max(scale, 0.1 * max(abs(med), 1.0))
        return v > med + UNUSUAL_K * scale, med

    def tick(self, now=None):
        now = self._clock() if now is None else now
        flat, meta = self.values_fn()
        self._record(flat, now)
        for rule in self.rules:
            v = flat.get(rule["metric"])
            cond, typical = self._condition(rule, v, flat)
            st = self.state.setdefault(
                rule["id"], {"since": None, "active": False, "episode": 0, "clear": None})
            if cond:
                st["clear"] = None
                if st["since"] is None:
                    st["since"] = now
                if not st["active"] and now - st["since"] >= rule["for"]:
                    st["active"], st["episode"] = True, int(st["since"])
                    self._send(rule, v, typical, meta, now - st["since"], st["episode"], False)
            elif st["active"]:
                if st["clear"] is None:
                    st["clear"] = now
                if now - st["clear"] >= CLEAR_HOLD:
                    st["active"], st["since"], st["clear"] = False, None, None
                    if rule["recovery"] and v is not None and v >= 0:
                        self._send(rule, v, None, meta, 0, st["episode"], True)
            else:
                st["since"] = None

    def _send(self, rule, v, typical, meta, held, episode, recovered):
        label, unit = meta.get(rule["metric"],
                               (label_for_key(rule["metric"]), unit_for_key(rule["metric"])))
        ctx = {"machine": self.host, "label": label, "metric": rule["metric"],
               "value": fmt_value(v, unit), "limit": fmt_value(rule["limit"], unit),
               "duration": fmt_duration(max(held, rule["for"])),
               "typical": fmt_value(typical, unit) if typical is not None else ""}
        if recovered:
            title = "%s back to normal on %s" % (label, self.host)
            body = "%s is %s." % (label, ctx["value"])
            sev, mid = "info", "pc-%s-%d-ok" % (rule["id"], episode)
        else:
            sev, mid = rule["severity"], "pc-%s-%d" % (rule["id"], episode)
            if rule["op"] == "above":
                title = "%s high on %s" % (label, self.host)
                body = "%s is %s, above %s for %s." % (
                    label, ctx["value"], ctx["limit"], ctx["duration"])
            elif rule["op"] == "below":
                title = "%s low on %s" % (label, self.host)
                body = "%s is %s, below %s." % (label, ctx["value"], ctx["limit"])
            else:
                title = "Unusual %s on %s" % (label, self.host)
                body = "%s is %s, far above its usual %s here, for %s." % (
                    label, ctx["value"], ctx["typical"], ctx["duration"])
            if rule["title"]:
                title = fill(rule["title"], ctx)
            if rule["body"]:
                body = fill(rule["body"], ctx)
        self.outbox.add({"id": mid, "severity": sev, "title": title, "body": body,
                         "source": "PeekESP rules"}, internal=True)


# ==========================================================================
#  The hook: this machine's own way of telling you
#
#  A phone gets a notification and an alarm. A headless box has neither, so
#  the equivalent is a command you choose, run for every new message: a buzzer
#  on a GPIO pin, `wall`, a webhook, an ntfy or Telegram call. It receives the
#  message in PEEK_SEVERITY, PEEK_TITLE, PEEK_BODY, PEEK_SOURCE, PEEK_ID,
#  PEEK_HOST and PEEK_TS - in the environment rather than the command line, so
#  nothing in a message can be read as an argument or as shell.
# ==========================================================================
SECRET_ENV = ("PEEK_PAIR_CODE", "PEEK_PUSH_TOKEN")


def clean_env():
    """The service's environment minus the credential. PEEK_PAIR_CODE is in it
    (systemd loads agent.conf), and a hook or a sensor command would otherwise
    inherit the one value that lets anything push to this display."""
    return dict((k, v) for k, v in os.environ.items() if k not in SECRET_ENV)


class Hook:
    def __init__(self, host=""):
        self.host = host
        self.argv, self.min, self.quiet = [], "warning", copy.deepcopy(DEFAULT_QUIET)
        self.last_error = ""
        self._sem = threading.BoundedSemaphore(4)

    def configure(self, argv, min_severity, quiet):
        self.argv, self.min, self.quiet = list(argv), min_severity, quiet

    def fire(self, msg, dt=None):
        """Run the hook for a message if the policy allows it. -> True if run."""
        if not self.argv or not hook_policy(msg["severity"], self.min, self.quiet,
                                            dt or datetime.datetime.now()):
            return False
        if not self._sem.acquire(False):
            return False                     # four already running: do not pile up
        threading.Thread(target=self._run, args=(msg,), daemon=True).start()
        return True

    def _run(self, msg):
        env = clean_env()
        env.update({"PEEK_SEVERITY": msg["severity"], "PEEK_TITLE": msg["title"],
                    "PEEK_BODY": msg.get("body", ""), "PEEK_SOURCE": msg["source"],
                    "PEEK_ID": msg["id"], "PEEK_HOST": self.host,
                    "PEEK_TS": str(msg["ts"])})
        try:
            r = subprocess.run(self.argv, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=15)
            err = "" if r.returncode == 0 else "exited %d: %s" % (
                r.returncode, r.stderr.decode("utf-8", "replace").strip()[:200])
        except subprocess.TimeoutExpired:
            err = "took over 15 s and was stopped"
        except (OSError, subprocess.SubprocessError) as e:
            err = str(e)
        finally:
            self._sem.release()
        if err and err != self.last_error:
            print("hook: " + err, flush=True)
        self.last_error = err


# ==========================================================================
#  Everything the agent runs besides the sampler and the push
# ==========================================================================
ALERTS_FILE = os.environ.get("PEEK_ALERTS_FILE") or "/etc/peekesp/alerts.json"


def load_config_file(path=ALERTS_FILE):
    """-> (clean config, warnings, state) where state is "ok", "missing" or
    "error". An unreadable or malformed file is "error", and the caller keeps
    what it had: a typo mid-edit must not silently turn the rules off."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        cfg, _w = clean_config({})
        return cfg, [], "missing"
    except PermissionError:
        cfg, _w = clean_config({})
        return cfg, ["cannot read %s. To look at it, use sudo; if the SERVICE cannot "
                     "read it, run: sudo peekesp repair" % path], "error"
    except (OSError, ValueError) as e:
        cfg, _w = clean_config({})
        return cfg, ["%s is not valid JSON: %s" % (path, e)], "error"
    cfg, warns = clean_config(raw)
    return cfg, warns, "ok"


class Services:
    def __init__(self, host, alerts_file=ALERTS_FILE, snapshot_fn=None, state=None):
        """state: None finds the state directory (creating it); False means
        none at all, which is what a one-shot `--once` wants - printing a reading
        must not create directories as a side effect."""
        self.host = host
        self.alerts_file = alerts_file
        self.snapshot_fn = snapshot_fn or (lambda: {})
        self.state = state_dir() if state is None else (state or None)
        self.alerts_on = _env_flag_default("PEEK_ALERTS", True)
        self.sensors_on = _env_flag_default("PEEK_SENSORS", True)
        self.notify_on = _env_flag_default("PEEK_NOTIFY", True)
        self.extras_interval = max(2.0, _env_float("PEEK_EXTRAS_INTERVAL", 10.0))
        self.ttl_hours = max(1.0, min(72.0, _env_float("PEEK_MESSAGE_TTL_HOURS", 6.0)))
        try:
            self.notify_port = int(os.environ.get("PEEK_NOTIFY_PORT") or NOTIFY_PORT)
        except ValueError:
            self.notify_port = NOTIFY_PORT

        def path(name):
            return os.path.join(self.state, name) if self.state else None
        self.outbox = Outbox(path("outbox.json"), self.ttl_hours, host)
        self.cfg, _w = clean_config({})
        self.extras = ExtraSensors(self.sensors_on, self.extras_interval, [])
        self.engine = AlertEngine(lambda: flatten(self.snapshot_fn()), self.outbox, host,
                                  history_path=path("history.json"))
        self.hook = Hook(host)
        self.watcher = OutboxWatcher(self.outbox, outbox_dir(self.state)) if self.state else None
        self.server = None
        self._sig = None
        self._stop = threading.Event()
        self._link_lost = False
        self.outbox.on_new(self._on_message)

    # -- config ------------------------------------------------------------
    def load_config(self):
        """(Re)read alerts.json and apply it. -> list of lines for the journal."""
        cfg, warns, status = load_config_file(self.alerts_file)
        lines = ["alerts.json: " + w for w in warns]
        if status == "error":
            return lines + (["keeping the previous settings"] if self._sig else [])
        self.cfg = cfg
        self.engine.configure(cfg["rules"], self.alerts_on)
        self.extras.configure(self.sensors_on, self.extras_interval, cfg["sensors"])
        self.hook.configure(cfg["hook"], cfg["hook_min_severity"], cfg["quiet"])
        return lines

    def _file_sig(self):
        try:
            st = os.stat(self.alerts_file)
            return (st.st_mtime, st.st_size)
        except OSError:
            return None

    def _watch_config(self, stop):
        while not stop.wait(5.0):
            sig = self._file_sig()
            if sig == self._sig:
                continue
            self._sig = sig
            for line in self.load_config():
                print(line, flush=True)
            print("alerts.json reloaded: %d rule(s), %d sensor(s)"
                  % (len(self.cfg["rules"]), len(self.cfg["sensors"])), flush=True)

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        self._sig = self._file_sig()
        lines = self.load_config()
        for line in lines:
            print(line, flush=True)
        print("alerts: %d rule(s)%s, %d custom sensor(s)"
              % (len(self.cfg["rules"]), "" if self.alerts_on else " (PEEK_ALERTS=0: off)",
                 len(self.cfg["sensors"])), flush=True)

        if not self.state:
            print("warning: no writable state directory, so messages are kept in memory "
                  "only (they do not survive a restart) and the local endpoint is off. "
                  "Add StateDirectory=peekesp to the unit, or run: sudo peekesp update",
                  flush=True)
        else:
            ensure_outbox_dir(self.state)
            self.watcher.start()
            if self.notify_on:
                try:
                    key = load_or_create_key(self.state)
                except OSError as e:
                    key = None
                    print("warning: cannot keep a key in %s (%s); local endpoint is off"
                          % (self.state, e), flush=True)
                if key:
                    self.server = NotifyServer(self.outbox, key, self.notify_port)
                    if self.server.start():
                        print("notify: 127.0.0.1:%d, drop folder %s"
                              % (self.notify_port, outbox_dir(self.state)), flush=True)
                    else:
                        print("warning: local endpoint: " + self.server.error, flush=True)

        self.extras.start()
        self.engine.start()
        self._stop.set()
        stop = self._stop = threading.Event()
        threading.Thread(target=self._watch_config, args=(stop,), daemon=True).start()

    def stop(self):
        self._stop.set()
        for part in (self.extras, self.engine, self.watcher, self.server):
            if part is not None:
                part.stop()

    # -- messages -------------------------------------------------------------
    def _on_message(self, msg):
        """Every new message, whoever sent it: a rule, a security tool, a test."""
        if time.time() - msg["ts"] > 6 * 3600:
            return                       # old news: filed quietly, as the phone does
        self.hook.fire(msg)

    def link_note(self, lost):
        """The relay link dropped or came back. Local only: the phone is exactly
        what cannot be told."""
        if lost:
            m = {"severity": "warning", "title": "PeekESP cannot reach the relay",
                 "body": "This machine has no route to the relay. Your phone will see "
                         "it as offline until it is back.", "source": "PeekESP",
                 "id": "link-lost", "ts": int(time.time())}
        else:
            m = {"severity": "info", "title": "PeekESP is back online",
                 "body": "The relay is reachable again.", "source": "PeekESP",
                 "id": "link-ok", "ts": int(time.time())}
        self.hook.fire(m)

    def problems(self):
        out = []
        if self.server is not None and self.server.error:
            out.append("local endpoint: " + self.server.error)
        if self.engine.last_error:
            out.append("alert rules: " + self.engine.last_error)
        if self.outbox.save_error:
            out.append("outbox: " + self.outbox.save_error)
        return out


_services = None


# ==========================================================================
#  Subcommands: notify, rules, quiet, hook, sensors, alerts
#
#  Reached as `peekesp notify ...` and so on. They edit alerts.json through the
#  same validation the agent reads it with, so a command can never write a file
#  the agent would then reject, and the running agent picks the change up
#  within five seconds without a restart.
# ==========================================================================
SUBCOMMANDS = ("notify", "rules", "quiet", "hook", "sensors", "alerts")


def _read_raw(path):
    """alerts.json as a dict; {} if there is none. Raises ValueError if it is
    there and is not valid JSON - never silently replaced, it is someone's work."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("%s is not a JSON object" % path)
    return raw


def _write_raw(path, raw):
    """Atomic, 0640, in the service group, so the agent can read it and nobody
    else can write it."""
    tmp = path + ".new"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o640)
        if os.geteuid() == 0:
            try:
                import grp
                os.chown(tmp, 0, grp.getgrnam(SERVICE_USER).gr_gid)
            except (KeyError, OSError):
                pass
        os.replace(tmp, path)
    except PermissionError:
        raise SystemExit("error: cannot write %s - this needs root:  sudo peekesp %s"
                         % (path, " ".join(sys.argv[1:])))


def _edit(path, fn):
    """Load, let fn change a CLEANED copy of the config, validate, write."""
    try:
        raw = _read_raw(path)
    except ValueError as e:
        raise SystemExit("error: %s\n  fix or remove it first: sudo peekesp alerts edit" % e)
    cfg, _w = clean_config(raw)
    fn(cfg)
    out, warns = clean_config(cfg)
    if warns:
        raise SystemExit("error: " + "; ".join(warns))
    # Keep every key the person had, and replace only the ones we manage.
    raw.update({"rules": out["rules"], "quiet": out["quiet"], "hook": out["hook"],
                "hook_min_severity": out["hook_min_severity"], "sensors": out["sensors"]})
    _write_raw(path, raw)
    return out


def _load_cli(path):
    """Load for a command that is about to SHOW settings. If the file cannot be
    read or parsed, say so and stop: printing the defaults instead would look
    like the real rules, and someone would act on them."""
    cfg, warns, status = load_config_file(path)
    if status == "error":
        raise SystemExit("error: " + "; ".join(warns))
    for w in warns:
        print("note: " + w, file=sys.stderr)
    return cfg, status


def _quiet_line(q):
    if not q["enabled"]:
        return "quiet hours  off"
    days = ("daily" if len(q["days"]) == 7
            else ",".join(DAYS[d] for d in q["days"]) or "no days")
    return "quiet hours  %s-%s, %s, critical %s" % (
        q["start"], q["end"], days, "allowed" if q["allow_critical"] else "silenced too")


def _cmd_rules(argv, path):
    ap = argparse.ArgumentParser(prog="peekesp rules", description="Alert rules that run on this machine.")
    sub = ap.add_subparsers(dest="action")
    sub.add_parser("list")
    for name in ("on", "off", "rm"):
        sub.add_parser(name).add_argument("id")
    sub.add_parser("reset")
    a = sub.add_parser("add")
    a.add_argument("metric")
    a.add_argument("op", choices=OPS)
    a.add_argument("limit", nargs="?", type=float)
    a.add_argument("--for", dest="hold", type=int, default=0, metavar="SECONDS")
    a.add_argument("--severity", default="warning")
    a.add_argument("--id", default="")
    a.add_argument("--min", type=float, default=0.0)
    a.add_argument("--unplugged", action="store_true")
    a.add_argument("--no-recovery", action="store_true")
    a.add_argument("--title", default="")
    a.add_argument("--body", default="")
    args = ap.parse_args(argv)
    action = args.action or "list"

    if action == "list":
        cfg, status = _load_cli(path)
        for r in cfg["rules"]:
            print("%-18s %-3s  %s" % (r["id"], "on" if r["enabled"] else "off", describe_rule(r)))
        if not cfg["rules"]:
            print("(no rules)")
        print("\nplaceholders for --title / --body: {machine} {label} {value} {limit} "
              "{duration} {typical}")
        return 0

    def find(cfg):
        hit = [r for r in cfg["rules"] if r["id"] == args.id]
        if not hit:
            raise SystemExit("error: no rule called '%s'. See: peekesp rules" % args.id)
        return hit[0]

    if action in ("on", "off"):
        def f(cfg):
            find(cfg)["enabled"] = action == "on"
        _edit(path, f)
        print("%s %s" % (args.id, action))
    elif action == "rm":
        def f(cfg):
            cfg["rules"].remove(find(cfg))
        _edit(path, f)
        print("removed " + args.id)
    elif action == "reset":
        def f(cfg):
            cfg["rules"] = default_rules()
        _edit(path, f)
        print("default rules restored")
    else:
        if args.op != "unusual" and args.limit is None:
            raise SystemExit("error: '%s' needs a limit:  rules add %s %s LIMIT"
                             % (args.op, args.metric, args.op))
        if not is_known_severity(args.severity):
            raise SystemExit("error: severity is info, warning or critical")

        def f(cfg):
            cfg["rules"].append({
                "id": args.id, "metric": args.metric, "op": args.op,
                "limit": args.limit if args.limit is not None else 0.0, "for": args.hold,
                "severity": args.severity, "min": args.min, "unplugged": args.unplugged,
                "recovery": not args.no_recovery, "title": args.title, "body": args.body})
        out = _edit(path, f)
        print("added: " + describe_rule(out["rules"][-1]))
    return 0


def _cmd_quiet(argv, path):
    ap = argparse.ArgumentParser(
        prog="peekesp quiet",
        description="Quiet hours for the hook (and only for the hook: the phone has its own).")
    ap.add_argument("times", nargs="*", metavar="off | START END",
                    help="e.g.  22:00 07:00")
    ap.add_argument("--days", default="all",
                    help="the days the window BEGINS: all, weekdays, weekends, or "
                         "mon,tue,... (a Friday night runs into Saturday morning)")
    ap.add_argument("--block-critical", action="store_true",
                    help="silence critical messages too")
    args = ap.parse_args(argv)

    if not args.times:
        cfg, _s = _load_cli(path)
        print(_quiet_line(cfg["quiet"]))
        return 0

    def f(cfg):
        if args.times == ["off"]:
            cfg["quiet"]["enabled"] = False
            return
        if len(args.times) != 2 or not (_hhmm(args.times[0]) and _hhmm(args.times[1])):
            raise SystemExit("error: give two 24-hour times, e.g.  peekesp quiet 22:00 07:00")
        d = args.days.lower()
        if d == "all":
            days = list(range(7))
        elif d == "weekdays":
            days = [0, 1, 2, 3, 4]
        elif d == "weekends":
            days = [5, 6]
        else:
            try:
                days = sorted(set(DAYS.index(x.strip()[:3]) for x in d.split(",") if x.strip()))
            except ValueError:
                raise SystemExit("error: --days is all, weekdays, weekends or mon,tue,...")
        cfg["quiet"].update({"enabled": True, "start": _hhmm(args.times[0]),
                             "end": _hhmm(args.times[1]), "days": days,
                             "allow_critical": not args.block_critical})
    out = _edit(path, f)
    print(_quiet_line(out["quiet"]))
    return 0


def _cmd_hook(argv, path):
    ap = argparse.ArgumentParser(
        prog="peekesp hook",
        description="A command run for every new message: a buzzer, wall, a webhook, ntfy ...")
    sub = ap.add_subparsers(dest="action")
    sub.add_parser("show")
    sub.add_parser("off")
    s = sub.add_parser("set", help="peekesp hook set [--min warning] /path/to/program [args]")
    s.add_argument("--min", default="warning", help="lowest severity that runs it")
    s.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)

    if args.action in (None, "show"):
        cfg, _s = _load_cli(path)
        if not cfg["hook"]:
            print("hook  none")
        else:
            print("hook  %s   (from %s up)" % (" ".join(shlex.quote(a) for a in cfg["hook"]),
                                                cfg["hook_min_severity"]))
        print(_quiet_line(cfg["quiet"]))
        return 0

    def f(cfg):
        if args.action == "off":
            cfg["hook"] = []
            return
        cmd = [c for c in args.command if c != "--"] if args.command[:1] == ["--"] else args.command
        if not cmd:
            raise SystemExit("error: give the command to run")
        if not is_known_severity(args.min):
            raise SystemExit("error: --min is info, warning or critical")
        cfg["hook"], cfg["hook_min_severity"] = cmd, norm_severity(args.min)
    out = _edit(path, f)
    if out["hook"] and not shutil.which(out["hook"][0]) and not os.path.exists(out["hook"][0]):
        print("note: %s was not found on this machine" % out["hook"][0], file=sys.stderr)
    print("hook  " + (" ".join(shlex.quote(a) for a in out["hook"]) or "none"))
    return 0


def _cmd_sensors(argv, path):
    ap = argparse.ArgumentParser(prog="peekesp sensors",
                                 description="GPU, NPU, fans and your own sensors.")
    sub = ap.add_subparsers(dest="action")
    sub.add_parser("list", help="what is being read right now, and from where")
    t = sub.add_parser("test", help="read one of your own sensors once")
    t.add_argument("key")
    sub.add_parser("rm").add_argument("key")
    a = sub.add_parser("add")
    a.add_argument("key")
    a.add_argument("source", choices=SOURCES)
    a.add_argument("target", help="the command line, the file path, or the URL")
    a.add_argument("--label", default="")
    a.add_argument("--unit", default="")
    a.add_argument("--max", type=float, default=None)
    a.add_argument("--interval", type=float, default=10.0)
    a.add_argument("--scale", type=float, default=1.0, help="multiply by this, e.g. 0.001")
    a.add_argument("--regex", default="")
    a.add_argument("--json-path", default="")
    args = ap.parse_args(argv)
    action = args.action or "list"

    if action == "list":
        cfg, _s = _load_cli(path)
        x = ExtraSensors(_env_flag_default("PEEK_SENSORS", True), 10.0, cfg["sensors"])
        print("reading (about 2 seconds) ...", flush=True)
        x.collect_once(force=True)
        time.sleep(2.0)
        x.collect_once(force=True)               # a second pass, for rates (NPU load)
        lines = x.describe()
        print("\n".join(lines) if lines else
              "nothing found. A GPU needs nvidia-smi or the amdgpu driver; fans and disk "
              "temperatures need hwmon; an NPU needs the intel_vpu driver.")
        return 0

    if action == "test":
        cfg, _s = _load_cli(path)
        spec = next((s for s in cfg["sensors"] if s["key"] == args.key), None)
        if spec is None:
            raise SystemExit("error: no sensor called '%s'. See: peekesp sensors" % args.key)
        v, err = read_custom(spec)
        print(("%g %s" % (v, spec["unit"])).strip() if err is None else "failed: " + err)
        return 0 if err is None else 1

    if action == "rm":
        def f(cfg):
            before = len(cfg["sensors"])
            cfg["sensors"] = [s for s in cfg["sensors"] if s["key"] != args.key]
            if len(cfg["sensors"]) == before:
                raise SystemExit("error: no sensor called '%s'" % args.key)
        _edit(path, f)
        print("removed " + args.key)
        return 0

    def f(cfg):
        spec = {"key": args.key, "source": args.source, "label": args.label,
                "unit": args.unit, "max": args.max, "interval": args.interval,
                "scale": args.scale, "regex": args.regex, "json_path": args.json_path}
        spec[{"command": "command", "file": "path", "http": "url"}[args.source]] = args.target
        cfg["sensors"] = [s for s in cfg["sensors"] if s["key"] != args.key] + [spec]
    out = _edit(path, f)
    spec = out["sensors"][-1]
    v, err = read_custom(spec)
    print("added %s; right now it %s" % (
        args.key, ("reads %g %s" % (v, spec["unit"])).strip() if err is None
        else "cannot be read (%s)" % err))
    return 0


def _cmd_alerts(argv, path):
    ap = argparse.ArgumentParser(prog="peekesp alerts")
    sub = ap.add_subparsers(dest="action")
    sub.add_parser("summary")
    sub.add_parser("path")
    sub.add_parser("init", help="write the default rules to the file if there is none")
    c = sub.add_parser("check", help="validate the file")
    c.add_argument("file", nargs="?")
    args = ap.parse_args(argv)
    action = args.action or "summary"

    if action == "path":
        print(path)
    elif action == "init":
        if not os.path.exists(path):
            cfg, _w = clean_config({})
            _write_raw(path, cfg)
            print("wrote " + path)
        else:
            print(path + " already exists")
    elif action == "check":
        p = args.file or path
        cfg, warns, status = load_config_file(p)
        for w in warns:
            print("problem: " + w)
        if status == "error":
            print("%s: NOT loaded (the agent keeps its previous settings)" % p)
            return 1
        print("%s: %d rule(s), %d sensor(s)%s" % (
            p, len(cfg["rules"]), len(cfg["sensors"]),
            "" if status != "missing" else "  (no file: defaults)"))
        return 1 if warns else 0
    else:
        cfg, warns, status = load_config_file(path)
        if status == "error":
            print("settings  UNREADABLE: " + "; ".join(warns))
            return 0
        on = sum(1 for r in cfg["rules"] if r["enabled"])
        print("rules     %d (%d on)%s" % (len(cfg["rules"]), on,
                                           "" if status != "missing" else ", defaults"))
        print("sensors   %d of your own" % len(cfg["sensors"]))
        print(_quiet_line(cfg["quiet"]).replace("quiet hours  ", "quiet     "))
        print("hook      %s" % (" ".join(shlex.quote(a) for a in cfg["hook"]) or "none"))
        if warns:
            print("problems  " + "; ".join(warns))
    return 0


def _find_outbox_for_cli(explicit=None):
    """The drop folder this user can write to, or None."""
    if explicit:
        return explicit
    cands = []
    if os.environ.get("PEEK_STATE_DIR"):
        cands.append(os.path.join(os.environ["PEEK_STATE_DIR"], "outbox"))
    cands.append("/var/lib/peekesp/outbox")
    home = os.environ.get("HOME", "")
    if home and home != "/":
        cands.append(os.path.join(home, ".local", "state", "peekesp", "outbox"))
    for d in cands:
        if os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK):
            return d
    return None


def _cmd_notify(argv):
    ap = argparse.ArgumentParser(
        prog="peekesp notify",
        description="Leave a message for your phone, through this machine's agent.")
    ap.add_argument("--title", default="", help="up to 80 characters")
    ap.add_argument("--message", "--body", dest="body", default="", help="up to 400 characters")
    ap.add_argument("--severity", default="info",
                    help="info, warning or critical (red / yellow also work). "
                         "Critical rings the phone like an alarm.")
    ap.add_argument("--source", default="", help="who is speaking, shown on the phone")
    ap.add_argument("--id", default="", help="same id twice counts once, so a retry is safe")
    ap.add_argument("--ts", default="", help="when it happened, epoch seconds")
    ap.add_argument("--http", action="store_true",
                    help="POST to the local endpoint instead of dropping a file (needs the key)")
    ap.add_argument("--port", type=int, default=NOTIFY_PORT)
    ap.add_argument("--outbox", metavar="DIR", help="folder to drop the file in")
    a = ap.parse_args(argv)

    if not (a.title or a.body):
        ap.error("give --title or --message")
    if not is_known_severity(a.severity):
        print("note: '%s' is not a severity the phone knows; it will show as info"
              % a.severity, file=sys.stderr)
    raw = {"title": a.title, "body": a.body, "severity": a.severity}
    for k in ("source", "id", "ts"):
        if getattr(a, k):
            raw[k] = getattr(a, k)

    if a.http:
        import urllib.error
        import urllib.request
        key = ""
        for d in ([os.environ["PEEK_STATE_DIR"]] if os.environ.get("PEEK_STATE_DIR") else []) \
                + ["/var/lib/peekesp"]:
            try:
                with open(os.path.join(d, "notify.key")) as fh:
                    key = fh.read().strip()
                break
            except OSError:
                continue
        if not key:
            print("cannot read the key in /var/lib/peekesp/notify.key - run this as root "
                  "or as a member of the peekesp group", file=sys.stderr)
            return 1
        req = urllib.request.Request(
            "http://127.0.0.1:%d/notify" % a.port, data=json.dumps(raw).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                print(r.read().decode())
            return 0
        except urllib.error.HTTPError as e:
            print("refused: HTTP %d %s" % (e.code, e.reason), file=sys.stderr)
        except OSError as e:
            print("could not reach the agent on port %d: %s" % (a.port, e), file=sys.stderr)
        return 1

    folder = _find_outbox_for_cli(a.outbox)
    if not folder:
        print("no outbox folder this user can write to.\n"
              "  Is the agent installed and has it started once?  peekesp status\n"
              "  Not root? Join the service group, then log in again:\n"
              "    sudo usermod -aG peekesp $USER", file=sys.stderr)
        return 1
    try:
        print("queued: " + drop_file(folder, raw))
    except OSError as e:
        print("could not write to %s: %s" % (folder, e), file=sys.stderr)
        return 1
    return 0


def run_subcommand(argv):
    name, rest = argv[0], argv[1:]
    path = ALERTS_FILE
    if name == "notify":
        return _cmd_notify(rest)
    return {"rules": _cmd_rules, "quiet": _cmd_quiet, "hook": _cmd_hook,
            "sensors": _cmd_sensors, "alerts": _cmd_alerts}[name](rest, path)


def snapshot(messages=True):
    """One reading.

    messages=False leaves the outbox out. The LAN endpoint passes it: that
    listens on every interface with no password, and a message such as "new
    login from 203.0.113.9" is not for whoever happens to be on the network.
    The relay path keeps them, because that is how they reach the phone.
    """
    snap = _standard()
    svc = _services
    if svc is not None:
        plain, metrics = svc.extras.payload()
        for k, v in plain.items():
            snap.setdefault(k, v)            # never shadow a standard reading
        if metrics:
            snap["metrics"] = metrics
        if messages:
            msgs = svc.outbox.active()
            if msgs:
                snap["messages"] = msgs
    return snap


def _standard():
    with _lock:
        rolling = dict(_state)
    used_pct, total, free = _storage()
    bat_pct, bat_charging, bat_ac, bat_minutes = _battery()
    return {
        "host": socket.gethostname()[:19],       # the sketch stores 20 bytes
        "cpu_percent": round(rolling["cpu_percent"], 1),
        "ram_percent": round(_ram_percent(), 1),
        "storage_percent": round(used_pct, 1),
        "storage_total_gb": round(total / (1024 ** 3), 1),
        "storage_free_gb": round(free / (1024 ** 3), 1),
        "cpu_temp_c": round(_cpu_temp_c(), 1),
        "battery_percent": bat_pct,          # -1 on a machine with no battery
        "battery_charging": bat_charging,
        "battery_ac": bat_ac,
        "battery_minutes": bat_minutes,      # -1 when the rate is unknown
        "uptime_seconds": _uptime_seconds(),
        "net_rx_kbps": round(rolling["net_rx_kbps"], 1),
        "net_tx_kbps": round(rolling["net_tx_kbps"], 1),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.split("?")[0] not in (PATH, "/"):
            self.send_error(404)
            return
        body = json.dumps(snapshot(messages=False)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass          # a poll every 5 seconds would otherwise flood the journal


# --------------------------------------------------------------------------
#  Push mode - for when the ESP32 cannot reach this machine at all.
#
#  Serving works only if something can open a connection TO this box: a port
#  forward, or a WireGuard tunnel terminating here. Behind CGNAT, or on a
#  network you do not administer, neither is possible. Pushing inverts that:
#  this side dials out to a Cloudflare Worker, the ESP32 dials out to the same
#  Worker, and nothing needs an inbound port.
# --------------------------------------------------------------------------
def push_loop(url, token, interval):
    import urllib.error
    import urllib.request

    fails = 0
    net_fails, first_fail, link_lost = 0, 0.0, False
    while True:
        body = json.dumps(snapshot()).encode()
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            if fails:
                print("push recovered after %d failure(s)" % fails, flush=True)
            fails = net_fails = 0
            if link_lost:
                link_lost = False
                if _services is not None:
                    _services.link_note(False)
        except urllib.error.HTTPError as e:
            # 401 will never fix itself, so say plainly what is wrong rather
            # than burying it in a retry count. It is also not "the link is
            # down": telling someone that about a wrong code sends them to
            # check the router.
            print("push rejected: HTTP %s %s" % (e.code, e.reason), flush=True)
            fails += 1
            net_fails = 0
        except Exception as e:
            fails += 1
            if fails <= 3 or fails % 20 == 0:
                print("push failed (%d): %s" % (fails, e), flush=True)
            if net_fails == 0:
                first_fail = time.time()
            net_fails += 1
            if net_fails >= 3 and time.time() - first_fail >= 30 and not link_lost:
                link_lost = True
                print("relay unreachable for %ds" % (time.time() - first_fail), flush=True)
                if _services is not None:
                    _services.link_note(True)
        time.sleep(interval)


# Settings arrive from the environment so the systemd unit can stay fixed:
# changing the poll interval rewrites one line of a config file rather than
# regenerating and reloading a unit.
def _env_flag(name):
    return (os.environ.get(name, "") or "").strip().lower() in ("1", "true", "yes", "on")


def _env_flag_default(name, default):
    """Like _env_flag, but unset means `default` - the new features are on
    unless turned off, so an existing install picks them up when it updates."""
    v = (os.environ.get(name, "") or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_float(name, fallback):
    try:
        return float(os.environ.get(name, "") or fallback)
    except ValueError:
        return fallback


def main():
    global _services

    # `peekesp notify|rules|quiet|hook|sensors|alerts` land here. They are not
    # about telemetry, so they run before anything samples this machine.
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        sys.exit(run_subcommand(sys.argv[1:]))

    ap = argparse.ArgumentParser(description="PeekESP telemetry agent")
    ap.add_argument("--pair-code", metavar="CODE",
                    default=os.environ.get("PEEK_PAIR_CODE", ""),
                    help="the code shown on the device; derives everything else "
                         "(or set PEEK_PAIR_CODE)")
    ap.add_argument("--relay-base", metavar="URL",
                    default=os.environ.get("PEEK_RELAY_BASE") or RELAY_BASE,
                    # %(default)s, not the constant: this way --help shows what
                    # the service will actually use, environment included,
                    # rather than what it would use if nothing were configured.
                    help="relay to pair against (or set PEEK_RELAY_BASE; "
                         "default: %(default)s)")
    ap.add_argument("--push", metavar="URL",
                    help="POST telemetry to this Cloudflare Worker /ingest URL")
    ap.add_argument("--token", default=os.environ.get("PEEK_PUSH_TOKEN", ""),
                    help="bearer token for --push (or set PEEK_PUSH_TOKEN)")
    ap.add_argument("--interval", type=float, default=_env_float("PEEK_INTERVAL", 5.0),
                    help="seconds between pushes (or set PEEK_INTERVAL; "
                         "default: %(default)s)")
    ap.add_argument("--serve", action="store_true", default=_env_flag("PEEK_SERVE"),
                    help="also listen on :%d while pushing (or set PEEK_SERVE=1)" % PORT)
    ap.add_argument("--no-serve", action="store_true",
                    help="do not listen on :%d, push only" % PORT)
    ap.add_argument("--once", action="store_true",
                    help="print one reading as JSON and exit - what this "
                         "machine would report")
    ap.add_argument("--verify", action="store_true",
                    help="check the pairing code, print the stream it derives, "
                         "and exit without pushing anything")
    ap.add_argument("--command", metavar="VERB", nargs="+",
                    help="leave a command for the display and exit: "
                         + ", ".join(COMMANDS) + ", page N, bright N")
    ap.add_argument("--version", action="store_true",
                    help="print this agent's version and exit")
    ap.add_argument("--check-update", action="store_true",
                    help="say whether a newer release exists and exit: "
                         "0 up to date, 10 newer available, 1 could not check")
    args = ap.parse_args()

    # The one-shot actions come first, before anything samples this machine or
    # opens a socket - none of them is about telemetry.
    if args.version:
        print(AGENT_VERSION)
        return

    if args.check_update:
        status, message = update_status(latest_release())
        print(message)
        raise SystemExit(status)

    if args.command:
        if not args.pair_code:
            ap.error("--command needs a pairing code (--pair-code, or PEEK_PAIR_CODE)")
        if len(args.command) > 2:
            ap.error("--command takes a verb and at most one number")
        try:
            text = normalise_command(*args.command)
        except ValueError as e:
            ap.error(str(e))
        ok, message = send_command(args.pair_code, text, args.relay_base)
        print(message)
        raise SystemExit(0 if ok else 1)

    if args.verify:
        if not args.pair_code:
            ap.error("--verify needs --pair-code")
        try:
            print(pair_derive(args.pair_code)["stream"])
        except ValueError as e:
            ap.error(str(e))
        return

    if args.pair_code:
        try:
            d = pair_derive(args.pair_code)
        except ValueError as e:
            ap.error(str(e))
        args.push = "%s/ingest/%s" % (args.relay_base.rstrip("/"), d["stream"])
        args.token = d["push"]
        # The stream, not the code: the code is the secret and the journal is
        # not a secret. The stream id is what you compare against the device's
        # own "[pair] code X -> stream Y" line when they will not meet.
        print("paired to stream %s" % d["stream"], flush=True)

    if args.push and not args.token:
        ap.error("--push needs --token (or the PEEK_PUSH_TOKEN environment variable)")

    threading.Thread(target=_sampler, daemon=True).start()
    if not _primed.wait(SAMPLE_SECONDS * 3):   # wait for a real delta, not a guess
        print("warning: first sample did not arrive; rates may read 0", flush=True)

    # Before any of the serve/push argument checks: "show me what this machine
    # reports" is a question about this machine, not about where it sends it.
    _services = Services(socket.gethostname()[:19], snapshot_fn=lambda: snapshot(messages=False),
                         state=False if args.once else None)

    if args.once:
        # Ask every sensor source right now instead of waiting for the thread,
        # so this really is "can the agent see my GPU". Twice, because a rate
        # such as NPU load needs two samples.
        _services.load_config()
        _services.extras.collect_once(force=True)
        time.sleep(1.0)
        _services.extras.collect_once(force=True)
        print(json.dumps(snapshot(), indent=2))
        return

    # Listening is opt-in as soon as something is being pushed. Opening
    # 0.0.0.0:6595 as a side effect of pushing is a port on the LAN that nobody
    # asked for; --serve turns it back on for a device that polls directly.
    serving = args.serve or not args.push
    if args.no_serve:
        serving = False
    if not args.push and not serving:
        ap.error("--no-serve with nothing to push to would do nothing")

    _services.start()

    if args.push:
        print("pushing to %s every %gs" % (args.push, args.interval), flush=True)
        if not serving:
            push_loop(args.push, args.token, args.interval)
            return
        threading.Thread(target=push_loop,
                         args=(args.push, args.token, args.interval),
                         daemon=True).start()

    print("peek-agent on http://%s:%d%s" % (BIND, PORT, PATH), flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()