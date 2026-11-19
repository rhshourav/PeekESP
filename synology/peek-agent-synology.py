#!/usr/bin/env python3
# PEEKESP_SYNOLOGY_AGENT  <- the installer greps for this, so a downloaded 404 page is never installed
"""
peek-agent-synology.py - the PeekESP agent for Synology DSM 7.x.

Reports the same telemetry the Windows and DietPi agents do, read the way a NAS
wants it read, plus the one thing only a NAS can say: a RAID array has lost a
drive.

    { "host": "DiskStation", "cpu_percent": 4.1, "ram_percent": 38.2,
      "storage_percent": 61.0, "storage_total_gb": 10900.0, "storage_free_gb": 4250.0,
      "cpu_temp_c": 47.0, "battery_percent": -1, "uptime_seconds": 271830,
      "net_rx_kbps": 56.0, "net_tx_kbps": 4.0,
      "raid_degraded": false, "raid_status": "OK" }

Two outputs, and each is its own switch. Neither depends on the other:

    widget feed   GET http://<nas>:8080/telemetry on your LAN. The Android widget
                  (or anything else that can do an HTTP GET) reads it directly: no
                  internet, no relay, no account.                (peekesp serve on|off)
    relay push    POST to the PeekESP relay every few seconds, so the ESP32 and the
                  phone can see this NAS from anywhere.          (peekesp push on|off)

Standard library only, on purpose. DSM ships a Python 3 for its own use and there
is no pip worth trusting on a NAS, so this file is the whole program. It is
written for Python 3.7+ (DSM 7 is on 3.8) and every reading comes from /proc,
/sys or one tiny subprocess, so there is nothing to install.

THE RULE THAT SHAPES THE WHOLE FILE: a reading must never wait on anything that
can block. snapshot() assembles values that background threads already cached,
plus a few procfs reads (which are RAM). Anything that touches a drive, launches
a process or could hang on a stale mount lives in a sampler thread. That is
what keeps a sleeping disk asleep, and it is why a wedged sensor can make one
number stale but can never stop the push or the widget feed.

Settings live in /usr/local/etc/peekesp/config.json (0600). Editing it by hand is
a supported path: the agent notices within a few seconds, or on SIGHUP.

Exit codes (--healthcheck): 0 healthy, 1 not healthy, 2 running but not paired yet.
Exit code 3 when run: another agent already holds the lock (see acquire_instance_lock).

PeekESP by rhshourav  -  Discord: rhshourav
  repo  https://github.com/rhshourav/PeekESP
  site  https://rhshourav.github.io/PeekESP/
"""

import argparse
import copy
import glob
import hashlib
import ipaddress
import json
import logging
import logging.handlers
import os
import random
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Kept in step with the VERSION file at the repository root.
__version__ = "2.0.1"

AUTHOR = "rhshourav"
DISCORD = "rhshourav"
REPO_URL = "https://github.com/rhshourav/PeekESP"
SITE_URL = "https://rhshourav.github.io/PeekESP/"

# Identical to the Windows agent, deliberately. Cloudflare's edge bans the default
# "Python-urllib/3.x" user agent outright (error 1010) before the relay ever runs,
# which surfaces as an opaque 403 that looks nothing like an auth problem.
USER_AGENT = "PeekESP-agent/1.0 (+https://github.com/rhshourav/PeekESP)"

# Must match DEFAULT_RELAY_BASE in windows/peek_config.py and RELAY_BASE_URL in the
# firmware, or a paired device and this agent derive the same stream and then look
# for it on two different relays.
DEFAULT_RELAY_BASE = "https://peek-relay-b9a8.onrender.com"

# ---- the sampling rhythm ---------------------------------------------------
SAMPLE_SECONDS = 2.0         # CPU and network are rates, so they need two samples
SLOW_SECONDS = 20.0          # volumes and CPU temperature: slow to change, cheap to cache
UPS_SECONDS = 30.0
PUSH_TIMEOUT = 15.0          # a sleeping relay (free hosting) can take a while to answer
FIRST_PUSH_GRACE = 90.0      # see Collector.ready()
CONFIRM_SECONDS = 60.0       # how long a RAID problem must persist before a phone is rung
STATUS_SECONDS = 2.0
CONFIG_POLL_SECONDS = 5.0
SERVE_RETRY_SECONDS = 30.0

# DSM builds md0 (the DSM system partition) and md1 (swap) across every drive in
# the box. Data lives in md2 and up. See summarise_raid() for why these two are
# reported but do not trip the alarm.
SYSTEM_ARRAYS = frozenset({"md0", "md1"})

log = logging.getLogger("peekesp")


# ==========================================================================
#  Small helpers
# ==========================================================================
def read_text(path, limit=1 << 20):
    """File contents, or None. Never raises: a missing sensor is the normal
    case on a NAS, because the next model down has a different one."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return None


def run_cmd(argv, timeout=10):
    """stdout+stderr of a command, or None if it could not run or did not finish.

    argv is a list and there is no shell, so nothing in it is interpreted.
    Catches what launching a process can actually throw and nothing else: a bare
    `except Exception` once turned a missing import into "this machine has no
    sensor" for the whole life of the Windows agent.
    """
    try:
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout,
                           universal_newlines=True, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode, r.stdout


def find_exe(name, extra=()):
    exe = shutil.which(name)
    if exe:
        return exe
    for p in extra:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def fmt_ago(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return "%d s" % sec
    if sec < 3600:
        return "%d min" % (sec // 60)
    if sec < 86400:
        return "%d h %d min" % (sec // 3600, sec % 3600 // 60)
    return "%d d %d h" % (sec // 86400, sec % 86400 // 3600)


def fmt_gb(gb):
    if gb >= 1024:
        return "%.1f TB" % (gb / 1024.0)
    return "%.0f GB" % gb


def safe_id(s, limit=64):
    return re.sub(r"[^A-Za-z0-9._:@-]", "", str(s))[:limit]


# ==========================================================================
#  Pairing - byte-identical to windows/peek_pair.py, the firmware's
#  sha256_prefixed_hex() and derivePairing() in the Worker. Vector, from the
#  firmware's own comment:  K7M2P4QX9R -> stream 4b907ba136d0a7f2
# ==========================================================================
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 10


def normalise_code(code):
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def is_valid_code(code):
    c = normalise_code(code)
    return len(c) == CODE_LEN and all(ch in ALPHABET for ch in c)


def format_code(code):
    c = normalise_code(code)
    return "-".join(filter(None, (c[0:4], c[4:8], c[8:12])))


def _h(prefix, code, n):
    return hashlib.sha256((prefix + code).encode("ascii")).hexdigest()[:n]


def derive(code):
    """{'code', 'stream', 'push', 'read'} or ValueError on a malformed code."""
    c = normalise_code(code)
    if not is_valid_code(c):
        raise ValueError(
            "a pairing code is %d characters from %s (dashes and case are ignored; "
            "there is no 0, 1, I or O)" % (CODE_LEN, ALPHABET))
    return {"code": c,
            "stream": _h("peek-stream:", c, 16),
            "push": _h("peek-push:", c, 48),
            "read": _h("peek-read:", c, 48)}


# ==========================================================================
#  Configuration
# ==========================================================================
def config_dir():
    return os.environ.get("PEEK_CONFIG_DIR") or "/usr/local/etc/peekesp"


def run_dir():
    """tmpfs on DSM. Everything the agent writes continuously (status, log) goes
    here and never to a volume, so it cannot keep a drive awake."""
    d = os.environ.get("PEEK_RUN_DIR") or "/var/run"
    return d if os.path.isdir(d) else tempfile.gettempdir()


def status_path():
    return os.path.join(run_dir(), "peekesp.status.json")


_LOCK_FD = None


def acquire_instance_lock(path):
    """True if this is the only agent running. A kernel flock, so it cannot go stale:
    the kernel drops it the moment the process dies, however it dies - which a
    pidfile cannot promise after a kill -9 or a power cut."""
    global _LOCK_FD
    try:
        import fcntl
    except ImportError:                      # not Linux: no lock, and no harm
        return True
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return True                          # cannot even make the file: do not refuse to run
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _LOCK_FD = fd                            # held for the life of the process
    return True


def release_instance_lock():
    global _LOCK_FD
    if _LOCK_FD is not None:
        os.close(_LOCK_FD)
        _LOCK_FD = None


# "private" is what a home or office LAN actually looks like, and it includes
# 100.64.0.0/10 because that is where Tailscale lives - a NAS reached over a
# tailnet is a normal setup, and it is not the public internet.
PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7", "fe80::/10"))


def _bool(v):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "yes", "y", "enable", "enabled"):
        return True
    if s in ("0", "false", "off", "no", "n", "disable", "disabled"):
        return False
    raise ValueError("must be on or off")


def _num(lo, hi, cast=float):
    def parse(v):
        try:
            x = cast(str(v).strip())
        except ValueError:
            raise ValueError("must be a number")
        if not lo <= x <= hi:
            raise ValueError("must be between %g and %g" % (lo, hi))
        return x
    return parse


def _listify(v):
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).replace(";", ",").split(",") if x.strip()]


def _code(v):
    s = str(v or "").strip()
    if not s:
        return ""
    return derive(s)["code"]


def _url(v):
    s = str(v or "").strip().rstrip("/")
    if s and not s.lower().startswith(("http://", "https://")):
        raise ValueError("must start with https://")
    return s


def _ipv4(v):
    s = str(v).strip()
    try:
        ipaddress.IPv4Address(s)
    except ValueError:
        raise ValueError("must be an IPv4 address such as 0.0.0.0 or 192.168.1.10")
    return s


def _allow(v):
    out = []
    for tok in _listify(v):
        t = tok.lower()
        if t in ("private", "any"):
            out.append(t)
            continue
        try:
            out.append(str(ipaddress.ip_network(tok, strict=False)))
        except ValueError:
            raise ValueError("'%s' is not private, any or a network like 192.168.1.0/24" % tok)
    if not out:
        raise ValueError("needs at least one of: private, any, or a network like 192.168.1.0/24")
    return out


def _paths(v):
    out = _listify(v)
    for p in out:
        if not p.startswith("/"):
            raise ValueError("'%s' is not an absolute path" % p)
    return out


def _name(v):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(v or "")).strip()[:19]


def _ups(v):
    s = str(v).strip().lower()
    if s in ("auto", ""):
        return "auto"
    return "on" if _bool(s) else "off"


DEFAULTS = {
    "push": True,                       # the data-sending part: POST to the relay
    "serve": True,                      # the widget feed: GET /telemetry on the LAN
    "pair_code": "",
    "relay_base": DEFAULT_RELAY_BASE,
    "relay_url": "",                    # a named or private stream: wins over pairing
    "token": "",
    "interval": 5.0,
    "name": "",                         # shown on the display; default is the DSM hostname
    "serve_port": 8080,
    "serve_bind": "0.0.0.0",
    "serve_allow": ["private"],         # "private", "any", or networks like 192.168.1.0/24
    "volumes": [],                      # empty = every /volumeN, found automatically
    "drive_temp": True,
    "drive_interval": 300.0,
    "ups": "auto",
    "messages": True,                   # raise RAID / SMART problems as phone messages
    "message_ttl_hours": 6.0,
    "raid_system": False,               # count md0/md1 toward the RAID alarm
    "tls_verify": True,
}

PARSERS = {
    "push": _bool, "serve": _bool, "pair_code": _code, "relay_base": _url,
    "relay_url": _url, "token": lambda v: str(v or "").strip(),
    "interval": _num(1.0, 600.0), "name": _name,
    "serve_port": _num(1, 65535, int), "serve_bind": _ipv4, "serve_allow": _allow,
    "volumes": _paths, "drive_temp": _bool, "drive_interval": _num(30.0, 3600.0),
    "ups": _ups, "messages": _bool, "message_ttl_hours": _num(1.0, 72.0),
    "raid_system": _bool, "tls_verify": _bool,
}

# What `peekesp set <name> <value>` accepts, and the key it writes.
SETTABLE = {
    "push": "push", "serve": "serve", "interval": "interval", "port": "serve_port",
    "bind": "serve_bind", "allow": "serve_allow", "name": "name", "relay": "relay_base",
    "volumes": "volumes", "drive-temp": "drive_temp", "drive-interval": "drive_interval",
    "ups": "ups", "messages": "messages", "ttl-hours": "message_ttl_hours",
    "raid-system": "raid_system", "tls-verify": "tls_verify",
}

# Environment overrides: how the container is configured, and handy in tests.
# They win over the file and are never written back to it.
ENV_KEYS = {
    "PEEK_PAIR": "pair_code", "PEEK_PUSH": "push", "PEEK_SERVE": "serve",
    "PEEK_INTERVAL": "interval", "PEEK_PORT": "serve_port", "PEEK_BIND": "serve_bind",
    "PEEK_ALLOW": "serve_allow", "PEEK_NAME": "name", "PEEK_RELAY": "relay_base",
    "PEEK_VOLUMES": "volumes", "PEEK_DRIVE_TEMP": "drive_temp", "PEEK_UPS": "ups",
    "PEEK_MESSAGES": "messages", "PEEK_TLS_VERIFY": "tls_verify",
}


def validate(raw):
    """-> (clean config, warnings). A hand-edited file will have mistakes in it.
    A bad value falls back to its default with a reason; it never stops the agent
    and never takes its neighbours down with it."""
    cfg, warns = {}, []
    raw = raw if isinstance(raw, dict) else {}
    for key, default in DEFAULTS.items():
        if key not in raw:
            cfg[key] = copy.deepcopy(default)
            continue
        try:
            cfg[key] = PARSERS[key](raw[key])
        except (ValueError, TypeError) as e:
            cfg[key] = copy.deepcopy(default)
            warns.append("%s: %s - using %r" % (key, e, default))
    return cfg, warns


def load_config(directory=None):
    """-> (cfg, warnings). Never raises: a background agent that refuses to start
    over a stray comma is worse than one that starts with sane settings and says so."""
    path = os.path.join(directory or config_dir(), "config.json")
    raw, warns = {}, []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        # The file holds the means to push as this NAS. If it was made world
        # readable by hand, say so and fix it rather than carrying on.
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            try:
                os.chmod(path, 0o600)
                warns.append("config.json was readable by other users; tightened to 0600")
            except OSError:
                warns.append("config.json is readable by other users and could not be tightened")
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        warns.append("could not read %s - using defaults" % path)
    cfg, w = validate(raw)
    return cfg, warns + w


def save_config(cfg, directory=None):
    d = directory or config_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    path = os.path.join(d, "config.json")
    tmp = path + ".tmp"
    # Write-then-replace: a crash mid-write must not leave a truncated file that
    # holds the pairing.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({k: cfg[k] for k in DEFAULTS}, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def apply_env(cfg, environ):
    warns = []
    for var, key in ENV_KEYS.items():
        if environ.get(var, "") == "":
            continue
        try:
            cfg[key] = PARSERS[key](environ[var])
        except (ValueError, TypeError) as e:
            warns.append("%s: %s - ignored" % (var, e))
    return warns


def resolve_target(cfg):
    """-> (url, token, problem). A hand-set relay_url+token (a named or private
    stream) wins; otherwise both come from the pairing code."""
    if cfg["relay_url"] or cfg["token"]:
        if not cfg["relay_url"]:
            return None, None, "relay_url is empty"
        if not cfg["token"]:
            return None, None, "token is empty"
        if "/ingest" not in cfg["relay_url"]:
            return None, None, "relay_url should end in /ingest/<stream>"
        return cfg["relay_url"], cfg["token"], None
    if cfg["pair_code"]:
        d = derive(cfg["pair_code"])
        return "%s/ingest/%s" % (cfg["relay_base"].rstrip("/"), d["stream"]), d["push"], None
    return None, None, "not paired yet - run: peekesp pair CODE"


# ==========================================================================
#  /proc parsers. All pure: they take text, so the tests feed them fixtures
#  and nothing here needs a Synology to be checked.
# ==========================================================================
def parse_cpu_stat(text):
    """(busy, total) jiffies from the aggregate cpu line, or None.

    iowait counts as idle. A NAS spends much of its life waiting on disks, and a
    CPU that is waiting is not working: counting it as busy would make a RAID
    rebuild or a big copy read as 90% CPU on a box doing almost nothing. The
    guest columns are left out because the kernel has already folded them into
    user and nice; adding them again would count the same time twice.
    """
    for line in (text or "").splitlines():
        if not line.startswith("cpu "):
            continue
        try:
            f = [int(x) for x in line.split()[1:]]
        except ValueError:
            return None
        f += [0] * (8 - len(f))
        user, nice, system, idle, iowait, irq, softirq, steal = f[:8]
        idle_all = idle + iowait
        total = user + nice + system + idle_all + irq + softirq + steal
        return total - idle_all, total
    return None


def parse_meminfo(text):
    """-> (total_kb, available_kb) or None.

    MemAvailable, not MemFree. Linux fills spare RAM with filesystem cache, and
    DSM's Btrfs cache is greedy, so "used = total - free" reads 90% on a NAS that
    is almost idle. MemAvailable is the kernel's own answer to "how much could a
    program get without swapping", which is the question the gauge is asking.
    """
    vals = {}
    for line in (text or "").splitlines():
        k, _, v = line.partition(":")
        parts = v.split()
        if parts:
            try:
                vals[k.strip()] = int(parts[0])
            except ValueError:
                pass
    total = vals.get("MemTotal")
    if not total:
        return None
    if "MemAvailable" in vals:
        avail = vals["MemAvailable"]
    else:                                   # kernels before 3.14: approximate it
        avail = vals.get("MemFree", 0) + vals.get("Buffers", 0) + vals.get("Cached", 0)
    return total, min(avail, total)


def parse_net_dev(text):
    """/proc/net/dev -> {iface: (rx_bytes, tx_bytes)}."""
    out = {}
    for line in (text or "").splitlines()[2:]:
        name, sep, rest = line.partition(":")
        f = rest.split()
        if not sep or len(f) < 9:
            continue
        try:
            out[name.strip()] = (int(f[0]), int(f[8]))
        except ValueError:
            continue
    return out


# Only real NICs and bonds. A whitelist, not a blacklist: DSM boxes grow lo,
# docker0, veth*, br-*, ovs_*, virbr*, tun*, sit*, tap*, wg* and VLAN
# sub-interfaces, and every one of them carries traffic that is also counted on a
# NIC. "Exclude the bad ones" is a list that is wrong again after the next
# package install.
NIC_RE = re.compile(r"^(eth\d+|bond\d+)$")


def select_ifaces(names, sysfs="/sys"):
    """Which interfaces to sum, without counting any byte twice.

    A bond's slaves (eth0, eth1) carry exactly the bytes the bond (bond0) does,
    so summing both doubles the number. The slaves are dropped. ovs_eth0 is
    excluded by NIC_RE for the same reason: Open vSwitch (installed by Virtual
    Machine Manager) makes it a copy of eth0's host traffic.
    """
    nics = sorted(n for n in names if NIC_RE.match(n))
    slaves = set()
    for n in nics:
        if n.startswith("bond"):
            s = read_text("%s/class/net/%s/bonding/slaves" % (sysfs, n))
            if s:
                slaves.update(s.split())
    return [n for n in nics if n not in slaves]


def wrap_delta(cur, prev):
    """Bytes moved since the last sample, or None if the counter was reset.

    /proc/net/dev is 64-bit, but some NIC drivers keep 32-bit counters and wrap
    at 4 GiB - a busy gigabit link does that every 40 seconds. A negative delta
    is a wrap if the implied step is plausible, and a reset otherwise; a reset
    is dropped rather than reported as a huge spike.
    """
    d = cur - prev
    if d >= 0:
        return d
    if prev < 2 ** 32 and cur + 2 ** 32 - prev < 2 ** 31:
        return cur + 2 ** 32 - prev
    return None


# ---- RAID -------------------------------------------------------------------
_MD_HEAD = re.compile(r"^(md\d+)\s*:\s*(.*)$")
_MD_DEV = re.compile(r"^[\w./-]+\[\d+\]((?:\([A-Z]\))*)$")
_MD_COUNT = re.compile(r"\[(\d+)/(\d+)\]\s*\[([U_]+)\]")
_MD_ACTION = re.compile(r"\b(resync|recovery|check|repair|reshape)\s*=\s*([0-9.]+%|DELAYED|PENDING)", re.I)
_RANK = ("FAILED", "DEGRADED", "REBUILDING", "SYNCING", "OK")


def parse_mdstat(text):
    """/proc/mdstat -> list of arrays. None if the file could not be read.

    Each array: name, level, state, members (what it should have), working (what
    it has), failed, action/progress (a rebuild or check under way) and a
    one-word status: OK, SYNCING, REBUILDING, DEGRADED or FAILED.
    """
    if text is None:
        return None
    blocks, cur = [], None
    for line in text.splitlines():
        m = _MD_HEAD.match(line)
        if m:
            cur = {"name": m.group(1), "rest": m.group(2), "extra": []}
            blocks.append(cur)
        elif cur is not None and line[:1] in (" ", "\t") and line.strip():
            cur["extra"].append(line)
        else:
            cur = None

    arrays = []
    for b in blocks:
        toks = b["rest"].split()
        state = toks[0] if toks else ""
        i = 1
        if i < len(toks) and toks[i].startswith("("):          # (auto-read-only)
            i += 1
        level = ""
        if i < len(toks) and not _MD_DEV.match(toks[i]):
            level = toks[i]
            i += 1
        devs = [t for t in toks[i:] if _MD_DEV.match(t)]
        failed = sum(1 for t in devs if "(F)" in t)
        body = " ".join(b["extra"])

        members = working = None
        mc = _MD_COUNT.search(body)
        if mc:
            members, working = int(mc.group(1)), int(mc.group(2))
            # The [UU_U] string is the same fact written another way; if the two
            # ever disagree, believe the worse one.
            working = min(working, mc.group(3).count("U"))
        action = progress = None
        ma = _MD_ACTION.search(body)
        if ma:
            action, progress = ma.group(1).lower(), ma.group(2)

        if state == "inactive":
            status = "FAILED"
        elif (members is not None and working is not None and working < members) or failed:
            status = "REBUILDING" if action == "recovery" else "DEGRADED"
        elif action in ("resync", "check", "repair", "reshape"):
            status = "SYNCING"
        else:
            status = "OK"
        arrays.append({"name": b["name"], "level": level, "state": state,
                       "members": members, "working": working, "failed": failed,
                       "action": action, "progress": progress, "status": status})
    return arrays


def summarise_raid(arrays, include_system=False):
    """-> {'degraded': bool, 'status': str, 'arrays': [...], 'system': [...]}.

    degraded means redundancy is gone or an array is down: DEGRADED, REBUILDING
    (still not protected until it finishes) and FAILED. SYNCING - a scheduled
    consistency check on a healthy array - is not degraded, and is shown quietly.

    md0 and md1 are the DSM system partition and swap, mirrored across *every*
    drive in the box, including one that belongs to no storage pool. Pull a spare
    bay's drive and they go [4/3] while your data is untouched. Raising a
    critical alarm for that would teach you to ignore the alarm. A drive that
    really is failing takes its slice of the data array with it, and that array
    does trip it. raid_system=true counts them anyway.
    """
    if arrays is None:
        return {"degraded": False, "status": "UNKNOWN", "arrays": [], "system": []}
    data = [a for a in arrays if include_system or a["name"] not in SYSTEM_ARRAYS]
    system = [a for a in arrays if not include_system and a["name"] in SYSTEM_ARRAYS]
    if not data:
        return {"degraded": False, "status": "NONE", "arrays": [], "system": system}
    worst = min((a["status"] for a in data), key=_RANK.index)
    return {"degraded": worst in ("FAILED", "DEGRADED", "REBUILDING"),
            "status": worst, "arrays": data, "system": system}


# ---- volumes ----------------------------------------------------------------
# Exactly /volume1, /volume2, ... and nothing under them. That one pattern is what
# keeps Docker's overlay mounts, Btrfs subvolumes, @sharesnap snapshots, USB
# shares (/volumeUSB1) and "/" (DSM's tiny system partition, which is always
# ~full and would drag the percentage up) out of a number that is meant to be
# "how full is my NAS".
VOLUME_RE = re.compile(r"/volume[0-9]+")
LOCAL_FS = frozenset({"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs"})


def _unescape_mount(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_mounts(text):
    out = []
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) >= 3:
            out.append((p[0], _unescape_mount(p[1]), p[2]))
    return out


def find_volumes(mounts, override=None):
    if override:
        return list(override)
    seen = {}
    for _dev, mp, fs in mounts:
        if VOLUME_RE.fullmatch(mp) and fs in LOCAL_FS:
            seen[mp] = True
    return sorted(seen, key=lambda p: int(p[len("/volume"):]))


def volume_usage(paths, statvfs=os.statvfs, stat=os.stat):
    """Aggregate usage over volumes, df-style.

    Used is blocks-minus-free-blocks; free is what an ordinary user can still
    write (f_bavail), so ext4's reserved blocks are not promised to anyone. The
    percentage is used/(used+free), which is how df and DSM's Storage Manager
    arrive at theirs. The same filesystem mounted twice is counted once.
    """
    vols, seen = [], set()
    for p in paths:
        try:
            dev = stat(p).st_dev
            if dev in seen:
                continue
            s = statvfs(p)
        except OSError:
            continue
        seen.add(dev)
        fr = s.f_frsize or s.f_bsize
        total, used, avail = s.f_blocks * fr, (s.f_blocks - s.f_bfree) * fr, s.f_bavail * fr
        denom = used + avail
        vols.append({"path": p, "total": total, "used": used, "avail": avail,
                     "percent": 100.0 * used / denom if denom > 0 else 0.0})
    if not vols:
        return None
    used = sum(v["used"] for v in vols)
    avail = sum(v["avail"] for v in vols)
    return {"volumes": vols, "total": sum(v["total"] for v in vols), "avail": avail,
            "percent": 100.0 * used / (used + avail) if used + avail > 0 else 0.0}


# ---- temperatures -----------------------------------------------------------
def _temp_c(raw):
    """sysfs reports millidegrees; a few drivers report degrees. Anything outside
    0-125 is a dead or fake sensor, not a CPU."""
    try:
        v = float(str(raw).strip())
    except ValueError:
        return None
    if v >= 1000:
        v /= 1000.0
    return v if 0 < v < 125 else None


def _hwmon_tier(name, label):
    """0 = the package/die sensor, 1 = a core or an SoC sensor, None = not a CPU.
    An unknown driver is skipped rather than guessed: hwmon also carries NVMe
    drives, chipsets and Wi-Fi, and reporting one of those as the CPU is worse
    than reporting nothing."""
    if name == "coretemp":
        return 0 if label.startswith("package") else 1
    if name in ("k10temp", "zenpower"):
        return 0 if label in ("", "tctl", "tdie") else 1
    if any(w in name for w in ("cpu", "soc", "armada", "core")):
        return 1
    if name == "acpitz":
        return 2
    return None


def _zone_tier(ztype):
    if ztype == "x86_pkg_temp":
        return 0
    if any(w in ztype for w in ("cpu", "soc", "core", "package")):
        return 1
    if ztype == "acpitz":
        return 2
    return None


def read_cpu_temp(sysfs="/sys"):
    """-> (celsius, source) or (-1.0, "").

    Best sensor wins, and within it the hottest reading. acpitz is last because
    an ACPI zone is wherever the board vendor put a sensor - often not the CPU,
    and on some boards a constant. It is a real reading of a real place, just
    not the die; the Windows agent makes the same trade for the same reason.
    """
    found = {}                                # tier -> [(value, source)]
    seen = set()
    for pattern in (sysfs + "/class/hwmon/hwmon*",
                    sysfs + "/devices/platform/coretemp.*/hwmon/hwmon*",
                    sysfs + "/devices/platform/coretemp.*"):
        for hw in sorted(glob.glob(pattern)):
            real = os.path.realpath(hw)
            if real in seen:
                continue
            seen.add(real)
            name = (read_text(hw + "/name") or os.path.basename(hw).split(".")[0]).strip().lower()
            if os.path.basename(hw).startswith("coretemp"):
                name = "coretemp"
            for f in sorted(glob.glob(hw + "/temp*_input")):
                label = (read_text(f.replace("_input", "_label")) or "").strip().lower()
                tier, v = _hwmon_tier(name, label), _temp_c(read_text(f))
                if tier is not None and v is not None:
                    found.setdefault(tier, []).append((v, "%s %s" % (name, label or os.path.basename(f))))
    for z in sorted(glob.glob(sysfs + "/class/thermal/thermal_zone*")):
        ztype = (read_text(z + "/type") or "").strip().lower()
        tier, v = _zone_tier(ztype), _temp_c(read_text(z + "/temp"))
        if tier is not None and v is not None:
            found.setdefault(tier, []).append((v, "thermal zone " + ztype))
    if not found:
        return -1.0, ""
    v, src = max(found[min(found)])
    return v, src.strip()


# ---- drives -----------------------------------------------------------------
DISK_RE = re.compile(r"^(sd[a-z]{1,2}|sata\d+|sas\d+|nvme\d+n\d+)$")
SMARTCTL_PATHS = ("/usr/syno/bin/smartctl", "/usr/bin/smartctl", "/usr/sbin/smartctl",
                  "/usr/local/bin/smartctl", "/opt/bin/smartctl")


def list_disks(sysfs="/sys"):
    """Internal drives. USB sticks and the DSM boot device are not part of the NAS's
    storage and are not worth waking a USB bridge to ask about."""
    out = []
    for p in sorted(glob.glob(sysfs + "/block/*")):
        name = os.path.basename(p)
        if not DISK_RE.match(name):
            continue
        if "/usb" in os.path.realpath(p):
            continue
        size = read_text(p + "/size")
        if size is None or not size.strip().isdigit() or int(size) == 0:
            continue                                   # an empty bay, or a card reader
        out.append(name)
    return out


def smart_standby(text):
    """True when smartctl declined to touch the drive because it is asleep.

    `-n standby` makes smartctl ask the drive its power mode first (an ATA CHECK
    POWER MODE, which does not spin it up) and stop with exit status 2 if the
    drive is in STANDBY or SLEEP. That status is shared with "could not open the
    device", so the exit code alone cannot tell the two apart - the message can.
    """
    return bool(re.search(r"Device is in (STANDBY|SLEEP)\b", text or "", re.I))


def parse_smart(text):
    """-> {'temp': float|None, 'health': 'PASSED'|'FAILED'|None} from `smartctl -H -A`."""
    text = text or ""
    temp = None
    best_id = 0
    for line in text.splitlines():
        p = line.split()
        # SATA: ID# NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW...
        if len(p) >= 10 and p[0] in ("194", "190") and "emp" in p[1]:
            try:
                raw = int(p[9])
            except ValueError:
                continue
            if raw > 150:                   # some drives pack min/max into the high bytes
                raw &= 0xFF
            if 0 < raw < 120 and int(p[0]) > best_id:   # 194 beats 190 (airflow)
                temp, best_id = float(raw), int(p[0])
    if temp is None:
        for rx in (r"^Temperature:\s+(\d+)\s+Celsius",                 # NVMe
                   r"^Current Drive Temperature:\s+(\d+)\s*C"):        # SAS / SCSI
            m = re.search(rx, text, re.M)
            if m and 0 < int(m.group(1)) < 120:
                temp = float(m.group(1))
                break
    health = None
    if re.search(r"overall-health self-assessment test result:\s*FAILED", text, re.I) or \
            re.search(r"SMART Health Status:\s*(?!OK\b)\S", text):
        health = "FAILED"
    elif re.search(r"test result:\s*PASSED|SMART Health Status:\s*OK", text, re.I):
        health = "PASSED"
    return {"temp": temp, "health": health}


def probe_drive(dev, smartctl, run=run_cmd):
    """One drive, once -> {'state': 'ok'|'standby'|'error', ...}.

    Never wakes a sleeping drive. `-n standby` is the whole mechanism (see
    smart_standby). An *awake* drive answers a SMART read in milliseconds and
    does not spin down or up because of it.
    """
    base = [smartctl, "-n", "standby", "-H", "-A"]
    for extra in ([], ["-d", "sat"]):
        r = run(base + extra + ["/dev/" + dev], 25)
        if r is None:
            return {"state": "error", "error": "smartctl did not run or took over 25 s"}
        _rc, text = r
        if smart_standby(text):
            return {"state": "standby"}
        got = parse_smart(text)
        if got["temp"] is not None or got["health"] is not None:
            return {"state": "ok", "temp": got["temp"], "health": got["health"]}
        # Only some drives, behind some controllers, need to be told they speak SAT.
        if not re.search(r"specify device type|-d \w+|Unknown USB bridge", text, re.I):
            break
    low = (text or "").lower()
    if "permission denied" in low or "operation not permitted" in low:
        return {"state": "error", "error": "smartctl needs root"}
    return {"state": "error", "error": "no temperature in smartctl output"}


def parse_upsc(text):
    """`upsc` key: value lines -> dict."""
    out = {}
    for line in (text or "").splitlines():
        k, sep, v = line.partition(":")
        if sep and re.match(r"^[a-z0-9_.]+$", k.strip()):
            out[k.strip()] = v.strip()
    return out


def ups_reading(d):
    """NUT's view of a UPS -> the battery_* fields the phone and the device already
    understand. Returns None if it does not look like a UPS reading."""
    try:
        pct = int(float(d["battery.charge"]))
    except (KeyError, ValueError):
        return None
    status = d.get("ups.status", "").upper().split()
    try:
        minutes = int(float(d["battery.runtime"]) // 60)
    except (KeyError, ValueError):
        minutes = -1
    return {"battery_percent": max(0, min(100, pct)),
            "battery_charging": "CHRG" in status,
            "battery_ac": "OL" in status,
            "battery_minutes": minutes}


# ==========================================================================
#  The collector: background samplers and a cache
# ==========================================================================
def run_loop(name, fn, every, stop, note_error):
    """Call fn() every few seconds until stop is set. fn may return the delay
    before its next call. An exception is recorded and the loop carries on:
    one broken sensor must not be able to take the others down, and it must not
    go unmentioned either."""
    while not stop.is_set():
        delay = None
        try:
            delay = fn()
        except Exception as e:                       # noqa: BLE001 - see docstring
            note_error(name, "%s: %s" % (type(e).__name__, e))
        stop.wait(delay if delay else every())


class Collector:
    def __init__(self, cfg, proc="/proc", sysfs="/sys", run=run_cmd,
                 statvfs=os.statvfs, clock=time.monotonic):
        self.cfg = cfg
        self.proc, self.sysfs = proc, sysfs
        self._run, self._statvfs, self._clock = run, statvfs, clock
        self._lock = threading.Lock()
        self.primed = threading.Event()
        self._slow_done = threading.Event()
        self._born = clock()
        self._stop = threading.Event()
        self._threads = []
        self.errors = {}
        # cached readings
        self._cpu = 0.0
        self._rx = self._tx = 0.0
        self._prev_cpu = None
        self._prev_net = {}
        self._prev_t = None
        self.ifaces = []
        self._usage = None
        self._volumes = []
        self._cpu_temp = (-1.0, "")
        self._drives = {}                  # dev -> probe result + "at"
        self._ups = None
        self._ups_miss = 0

    # -- lifecycle -----------------------------------------------------------
    def configure(self, cfg):
        self.cfg = cfg

    def start(self):
        self.stop()
        stop = self._stop = threading.Event()
        # No pre-sample here: the fast thread's first pass primes the counters and
        # its second, SAMPLE_SECONDS later, produces the first rate. Sampling twice
        # back to back would divide a few bytes by a few microseconds.
        specs = (("fast", self.sample_fast, lambda: SAMPLE_SECONDS),
                 ("slow", self.sample_slow, lambda: SLOW_SECONDS),
                 ("drives", self.sample_drives, lambda: self.cfg["drive_interval"]),
                 ("ups", self.sample_ups, lambda: UPS_SECONDS))
        self._threads = []
        for name, fn, every in specs:
            t = threading.Thread(target=run_loop, args=(name, fn, every, stop, self.note_error),
                                 name="peek-" + name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()

    def note_error(self, name, msg):
        with self._lock:
            new = self.errors.get(name) != msg
            self.errors[name] = msg
        if new:
            log.warning("sampler %s: %s", name, msg)

    # -- samplers ------------------------------------------------------------
    def sample_fast(self):
        now = self._clock()
        cpu = parse_cpu_stat(read_text(self.proc + "/stat"))
        nets = parse_net_dev(read_text(self.proc + "/net/dev"))
        names = select_ifaces(nets, self.sysfs)
        with self._lock:
            if cpu and self._prev_cpu:
                db, dt = cpu[0] - self._prev_cpu[0], cpu[1] - self._prev_cpu[1]
                if dt > 0 and db >= 0:                 # a reset keeps the last value
                    self._cpu = clamp(100.0 * db / dt)
            if cpu:
                self._prev_cpu = cpu
            if self._prev_t is not None and now - self._prev_t >= 0.5:
                elapsed = now - self._prev_t
                rx = tx = 0.0
                # Per interface, then summed: an interface that appears or resets
                # drops out of one sample instead of spiking the total.
                for n in names:
                    prev = self._prev_net.get(n)
                    if prev is None:
                        continue
                    drx, dtx = wrap_delta(nets[n][0], prev[0]), wrap_delta(nets[n][1], prev[1])
                    rx += drx or 0
                    tx += dtx or 0
                # KiB/s, the same unit the Windows agent calls "kbps": the firmware
                # prints it as "56K" and the phone charts it as-is.
                self._rx, self._tx = rx / 1024.0 / elapsed, tx / 1024.0 / elapsed
                self.primed.set()
            if self._prev_t is None or now - self._prev_t >= 0.5:
                self._prev_net = {n: nets[n] for n in names}
                self._prev_t = now
            self.ifaces = names

    def sample_slow(self):
        vols = find_volumes(parse_mounts(read_text(self.proc + "/mounts")), self.cfg["volumes"])
        usage = volume_usage(vols, self._statvfs)
        temp = read_cpu_temp(self.sysfs)
        with self._lock:
            self._volumes, self._usage, self._cpu_temp = vols, usage, temp
        self._slow_done.set()

    def sample_drives(self):
        if not self.cfg["drive_temp"]:
            with self._lock:
                self._drives = {}
            return 30.0
        smartctl = find_exe("smartctl", SMARTCTL_PATHS)
        disks = list_disks(self.sysfs)
        if not smartctl or not disks:
            with self._lock:
                self._drives = {}
            return 600.0              # nothing to read; look again much later, not never
        fresh = {}
        for dev in disks:
            r = probe_drive(dev, smartctl, self._run)
            r["at"] = time.time()
            with self._lock:
                old = self._drives.get(dev, {})
            if r["state"] == "standby":
                # Asleep: leave it alone, and remember what it said last. A failing
                # drive does not get better while it is dozing.
                r["temp"], r["health"] = old.get("temp"), old.get("health")
                r["temp_at"] = old.get("temp_at", 0)
            elif r["state"] == "ok":
                r["temp_at"] = r["at"]
            fresh[dev] = r
            if self._stop.wait(1.0):  # one drive at a time, a second apart
                return None
        with self._lock:
            self._drives = fresh
        return None

    def sample_ups(self):
        mode = self.cfg["ups"]
        if mode == "off":
            self._ups = None
            return 60.0
        exe = find_exe("upsc", ("/usr/syno/bin/upsc", "/usr/bin/upsc", "/usr/local/bin/upsc"))
        reading = None
        if exe:
            names = ["ups"]
            r = self._run([exe, "-l"], 5)
            if r and r[0] == 0:
                listed = [x.strip() for x in r[1].splitlines() if re.match(r"^[\w.-]+$", x.strip())]
                names = listed or names
            r = self._run([exe, "%s@localhost" % names[0]], 5)
            if r and r[0] == 0:
                reading = ups_reading(parse_upsc(r[1]))
        with self._lock:
            self._ups = reading
            self._ups_miss = 0 if reading else self._ups_miss + 1
            miss = self._ups_miss
        # A NAS with no UPS is the common case: ask less and less often.
        return min(600.0, UPS_SECONDS * (1 + miss)) if not reading else None

    # -- readings ------------------------------------------------------------
    def ready(self):
        """Hold the first push until the rates exist and the volumes have been
        looked for. At boot this can run before DSM has mounted /volume1, and
        pushing "0% full" for the first minute is a lie that fires phone rules.
        After FIRST_PUSH_GRACE seconds it goes anyway: a box with no volumes
        should still report."""
        if not (self.primed.is_set() and self._slow_done.is_set()):
            return self._clock() - self._born >= FIRST_PUSH_GRACE
        return self._usage is not None or self._clock() - self._born >= FIRST_PUSH_GRACE

    def ram_percent(self):
        m = parse_meminfo(read_text(self.proc + "/meminfo"))
        return clamp(100.0 * (m[0] - m[1]) / m[0]) if m else 0.0

    def uptime(self):
        t = read_text(self.proc + "/uptime")
        try:
            return int(float((t or "0").split()[0]))
        except (ValueError, IndexError):
            return 0

    def raid(self):
        return summarise_raid(parse_mdstat(read_text(self.proc + "/mdstat")),
                              self.cfg["raid_system"])

    def drive_view(self):
        """-> (hottest fresh temperature or -1, {dev: probe})."""
        ttl = self.cfg["drive_interval"] * 3 + 60
        now = time.time()
        with self._lock:
            drives = {k: dict(v) for k, v in self._drives.items()}
        temps = [d["temp"] for d in drives.values()
                 if d.get("temp") is not None and now - d.get("temp_at", 0) <= ttl]
        return (max(temps) if temps else -1.0), drives

    def ups_view(self):
        with self._lock:
            return dict(self._ups) if self._ups else None

    def reading(self):
        """The standard fields, all cached or procfs."""
        with self._lock:
            cpu, rx, tx = self._cpu, self._rx, self._tx
            usage, temp = self._usage, self._cpu_temp[0]
        r = self.raid()
        ups = self.ups_view() or {"battery_percent": -1, "battery_charging": False,
                                  "battery_ac": True, "battery_minutes": -1}
        gb = 1024.0 ** 3
        snap = {
            "cpu_percent": round(cpu, 1),
            "ram_percent": round(self.ram_percent(), 1),
            "storage_percent": round(usage["percent"], 1) if usage else 0.0,
            "storage_total_gb": round(usage["total"] / gb, 1) if usage else 0.0,
            "storage_free_gb": round(usage["avail"] / gb, 1) if usage else 0.0,
            "cpu_temp_c": round(temp, 1),
            "battery_percent": ups["battery_percent"],       # -1 on a NAS with no UPS
            "battery_charging": ups["battery_charging"],
            "battery_ac": ups["battery_ac"],
            "battery_minutes": ups["battery_minutes"],
            "uptime_seconds": self.uptime(),
            "net_rx_kbps": round(rx, 1),
            "net_tx_kbps": round(tx, 1),
            "raid_degraded": r["degraded"],
            "raid_status": r["status"],
        }
        return snap, usage


# ==========================================================================
#  Messages: what only a NAS can notice, put in front of a phone
# ==========================================================================
class Episodes:
    """Turns "this is wrong right now" into the `messages` list of a reading.

    A reading is the latest state, not a queue (PROTOCOL.md section 2), so a
    message has to be in every reading for as long as the problem lasts, with the
    same id each time so the phone counts it once. Three things make that safe
    for a RAID alarm that can ring a phone at 3 a.m.:

      * A problem must hold for CONFIRM_SECONDS before it is raised, and be gone
        that long before it is cleared. Arrays are still assembling for a moment
        at boot; that is not an incident.
      * The id contains the time the problem began, so a second failure next
        month is a new alert, and a restart of this agent in the middle of one is
        not (the open episodes are kept in state.json, written only when one
        begins or ends - rarely, so it cannot keep a drive awake).
      * When it clears, an info message says so and stays in the readings for
        message_ttl_hours, so a phone that was off can still see how it ended.
    """

    def __init__(self, host, ttl_hours=6.0, path=None, confirm_s=CONFIRM_SECONDS,
                 clock=time.time):
        self.host = safe_id(host, 19) or "nas"
        self.ttl = ttl_hours * 3600.0
        self.path = path
        self.confirm_s = confirm_s
        self.clock = clock
        self.active = {}
        self.pending = {}
        self.clearing = {}
        self.done = []
        self._load()

    def configure(self, host, ttl_hours):
        self.host = safe_id(host, 19) or "nas"
        self.ttl = ttl_hours * 3600.0

    def _load(self):
        txt = read_text(self.path) if self.path else None
        try:
            data = json.loads(txt) if txt else {}
        except ValueError:
            data = {}
        for key, a in (data.get("active") or {}).items():
            if isinstance(a, dict) and isinstance(a.get("since"), (int, float)):
                self.active[str(key)] = {k: a.get(k, "") for k in
                                         ("since", "sev", "title", "body", "ok_title", "ok_body")}

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"active": self.active}, fh)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save %s: %s", self.path, e)

    def update(self, conds, now=None):
        """conds: {key: {sev, title, body, ok_title, ok_body}} - what is wrong now."""
        now = self.clock() if now is None else now
        changed = False
        for key, c in conds.items():
            self.clearing.pop(key, None)
            if key in self.active:
                self.active[key].update(c)
                continue
            first = self.pending.setdefault(key, now)
            if now - first >= self.confirm_s:
                self.active[key] = dict(c, since=first)
                del self.pending[key]
                changed = True
                log.warning("raised: %s", c["title"])
        for key in [k for k in self.pending if k not in conds]:
            del self.pending[key]                       # it flapped: start the clock again
        for key in [k for k in self.active if k not in conds]:
            first = self.clearing.setdefault(key, now)
            if now - first >= self.confirm_s:
                a = self.active.pop(key)
                del self.clearing[key]
                changed = True
                self.done.append({"id": self._id(key, a["since"]) + "-ok", "severity": "info",
                                  "title": a["ok_title"], "body": a["ok_body"],
                                  "ts": int(now), "until": now + self.ttl})
                log.info("cleared: %s", a["ok_title"])
        self.done = [d for d in self.done if d["until"] > now]
        if changed:
            self._save()

    def _id(self, key, since):
        return safe_id("%s:%s-%d" % (self.host, key, int(since)), 64)

    def messages(self, now=None):
        now = self.clock() if now is None else now
        out = [{"id": self._id(k, a["since"]), "severity": a["sev"], "title": a["title"][:80],
                "body": a["body"][:400], "source": "PeekESP NAS", "ts": int(a["since"])}
               for k, a in self.active.items()]
        out += [{"id": d["id"], "severity": d["severity"], "title": d["title"][:80],
                 "body": d["body"][:400], "source": "PeekESP NAS", "ts": d["ts"]}
                for d in self.done if d["until"] > now]
        out.sort(key=lambda m: (m["severity"] != "critical", -m["ts"]))
        return out[:10]                                 # the phone reads at most ten


def raid_conditions(raid, host):
    out = {}
    for a in raid["arrays"]:
        if a["status"] not in ("FAILED", "DEGRADED", "REBUILDING"):
            continue
        key = "raid-" + a["name"]
        if a["status"] == "FAILED":
            out[key] = {"sev": "critical", "title": "RAID array offline on %s" % host,
                        "body": "%s (%s) is not running. Its data is unavailable until it is repaired."
                                % (a["name"], a["level"] or "array"),
                        "ok_title": "RAID back online on %s" % host,
                        "ok_body": "%s is running again." % a["name"]}
            continue
        have = "%s of %s drives" % (a["working"], a["members"]) if a["members"] else "a failed drive"
        extra = (" Rebuilding: %s." % a["progress"]) if a["status"] == "REBUILDING" else ""
        out[key] = {"sev": "critical", "title": "RAID degraded on %s" % host,
                    "body": "%s (%s) has %s active. Your data is not protected against another "
                            "failure until it is repaired.%s" % (a["name"], a["level"] or "array", have, extra),
                    "ok_title": "RAID back to normal on %s" % host,
                    "ok_body": "%s has all its drives again." % a["name"]}
    return out


def smart_conditions(drives, host):
    out = {}
    for dev, d in drives.items():
        if d.get("health") == "FAILED":
            out["smart-" + safe_id(dev, 12)] = {
                "sev": "critical", "title": "Drive %s is failing on %s" % (dev, host),
                "body": "SMART reports %s as failed. Back up now and replace it." % dev,
                "ok_title": "Drive %s passes SMART again on %s" % (dev, host),
                "ok_body": "SMART no longer reports %s as failed." % dev}
    return out


# ==========================================================================
#  The widget feed: GET /telemetry on the LAN
# ==========================================================================
def parse_allow(items):
    """-> (networks, anyone). 'private' expands to the ranges a LAN uses."""
    nets, anyone = [], False
    for tok in items:
        if tok == "any":
            anyone = True
        elif tok == "private":
            nets.extend(PRIVATE_NETS)
        else:
            try:
                nets.append(ipaddress.ip_network(tok, strict=False))
            except ValueError:
                pass
    return nets, anyone


def allowed(addr, allow):
    nets, anyone = allow
    if anyone:
        return True
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return any(ip in n for n in nets if n.version == ip.version)


class LanServer:
    """Unauthenticated, like the Windows agent's serve mode, so it does two things
    to be a good neighbour on a NAS: it answers only the networks in serve_allow
    (private ranges unless you say otherwise, because a NAS is exactly the kind of
    box that ends up port-forwarded by accident), and it never carries messages -
    "RAID degraded" is for your phone, not for whoever is on the guest Wi-Fi."""

    def __init__(self, snapshot_fn, bind, port, allow):
        self.snapshot_fn = snapshot_fn
        self.bind, self.port, self.allow = bind, port, allow
        self._httpd = None
        self.error = ""

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 5                       # a stalled client cannot hold a thread

            def _send(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not allowed(self.client_address[0], owner.allow):
                    return self._send(403, b'{"error":"forbidden"}')
                if self.path.split("?")[0] not in ("/telemetry", "/"):
                    return self._send(404, b'{"error":"not found"}')
                try:
                    body = json.dumps(owner.snapshot_fn()).encode()
                except Exception:               # noqa: BLE001 - answer, do not die
                    return self._send(500, b'{"error":"reading failed"}')
                self._send(200, body)

            def log_message(self, *args):
                pass                              # a poll every 5 s would flood any log

        return Handler

    def start(self):
        try:
            self._httpd = ThreadingHTTPServer((self.bind, self.port), self._handler())
        except OSError as e:
            self._httpd = None
            self.error = "%s:%d - %s" % (self.bind, self.port, e.strerror or e)
            return False
        threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.4},
                         name="peek-serve", daemon=True).start()
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
#  The relay push
# ==========================================================================
CA_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/certs/ca-bundle.crt",
              "/etc/ssl/cert.pem", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem")


def make_ssl_context(verify=True):
    """Default verification, with a safety net for an odd Python build.

    A Python whose OpenSSL was compiled with trust-store paths that do not exist
    on this box fails every https push with CERTIFICATE_VERIFY_FAILED, which looks
    like a network fault and is not. If the defaults point nowhere, borrow the
    system bundle. Turning verification off is a setting (tls_verify) and a loud
    one, because the push token travels in that connection.
    """
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    p = ssl.get_default_verify_paths()
    if not ((p.cafile and os.path.exists(p.cafile)) or (p.capath and os.path.isdir(p.capath))):
        for f in CA_BUNDLES:
            if os.path.exists(f):
                try:
                    ctx.load_verify_locations(f)
                    break
                except (OSError, ssl.SSLError):
                    continue
    return ctx


HINTS = {401: "token rejected - is the pairing code right?", 403: "blocked at the edge or refused",
         404: "wrong URL or stream", 429: "rate limited", 500: "the relay's secrets are not set",
         502: "the relay is waking up or down", 503: "the relay is waking up or down"}


def push_once(url, token, body, timeout=PUSH_TIMEOUT, ctx=None):
    """-> (code, message). 0 means the request never got an answer at all."""
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token,
                 "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            r.read()
            return 200, "OK"
    except urllib.error.HTTPError as e:
        hint = HINTS.get(e.code, "")
        return e.code, "HTTP %d %s%s" % (e.code, e.reason, " - " + hint if hint else "")
    except ssl.SSLError as e:
        return 0, "TLS: %s (see tls_verify in the README if your Python has no CA bundle)" % e
    except Exception as e:                          # noqa: BLE001 - timeouts, DNS, resets
        reason = getattr(e, "reason", e)
        return 0, str(reason)


def push_delay(interval, code, fails):
    """Seconds to wait after an attempt. A relay that is down gets backed off so a
    NAS does not hammer it every five seconds for a day; a refusal that will not
    fix itself (bad token, wrong stream) is retried slowly."""
    if code == 200:
        return interval
    if code in (401, 403, 404, 500):
        return max(interval, 60.0)
    return min(60.0, interval * (2 ** min(fails, 4)))


# ==========================================================================
#  The agent
# ==========================================================================
class Agent:
    def __init__(self, directory=None, overrides=None, environ=None, log_file=None):
        self.dir = directory or config_dir()
        self.overrides = overrides or {}
        self.environ = os.environ if environ is None else environ
        self.stop_ev = threading.Event()
        self.reload_ev = threading.Event()
        self.started = time.time()
        self._st_lock = threading.Lock()
        self.push_st = {"enabled": False, "state": "disabled", "detail": "", "last_ok": 0,
                        "last_try": 0, "fails": 0, "stream": ""}
        self.serve_st = {"enabled": False, "state": "disabled", "detail": "",
                         "port": 0, "bind": ""}
        self.cfg, self.warnings = self._load()
        self.collector = Collector(self.cfg)
        self.episodes = Episodes(self.host(), self.cfg["message_ttl_hours"],
                                 os.path.join(self.dir, "state.json"))
        self.server = None
        self._serve_key = None
        self._serve_retry = 0.0
        self._push_stop = None
        self._ctx = None
        self._ctx_key = None
        self._mtime = self._cfg_mtime()

    # -- configuration -------------------------------------------------------
    def host(self):
        # The sketch stores 20 bytes for a hostname, so 19 plus the terminator.
        return (self.cfg["name"] or socket.gethostname())[:19]

    def _cfg_mtime(self):
        """A signature of the file, not just its timestamp: two edits inside one
        clock tick would share an mtime, and a changed size is the cheap tell."""
        try:
            st = os.stat(os.path.join(self.dir, "config.json"))
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self):
        cfg, warns = load_config(self.dir)
        warns += apply_env(cfg, self.environ)
        for k, v in self.overrides.items():           # one-run flags: --interval, --no-push ...
            cfg[k] = v
        return cfg, warns

    def reload(self):
        self.reload_ev.clear()
        self._mtime = self._cfg_mtime()
        self.cfg, self.warnings = self._load()
        for w in self.warnings:
            log.warning("config: %s", w)
        self.collector.configure(self.cfg)
        self.episodes.configure(self.host(), self.cfg["message_ttl_hours"])
        log.info("settings reloaded (push %s, widget feed %s)",
                 "on" if self.cfg["push"] else "off", "on" if self.cfg["serve"] else "off")
        self.apply()

    def apply(self):
        self._apply_push()
        self._apply_serve()

    # -- snapshot ------------------------------------------------------------
    def snapshot(self, messages=True):
        snap, usage = self.collector.reading()
        out = {"host": self.host()}
        out.update(snap)
        metrics = {}
        dt, _drives = self.collector.drive_view()
        if dt >= 0:
            metrics["drive_temp_c"] = {"value": round(dt, 1), "label": "Drive temperature",
                                       "unit": "\u00b0C"}
        if usage and len(usage["volumes"]) > 1:
            # Aggregated, a nearly full SSD volume hides behind a roomy HDD one.
            for v in usage["volumes"]:
                n = v["path"][len("/volume"):]
                if n.isdigit():
                    metrics["volume%s_percent" % n] = {"value": round(v["percent"], 1),
                                                       "label": "Volume %s" % n,
                                                       "unit": "%", "max": 100}
        if metrics:
            out["metrics"] = metrics
        if messages and self.cfg["messages"]:
            msgs = self.episodes.messages()
            if msgs:
                out["messages"] = msgs
        return out

    def snapshot_lan(self):
        # No messages here, by design - see LanServer.
        return self.snapshot(messages=False)

    def conditions(self):
        host = self.host()
        conds = raid_conditions(self.collector.raid(), host)
        conds.update(smart_conditions(self.collector.drive_view()[1], host))
        return conds

    # -- the relay push ------------------------------------------------------
    def _set_push(self, state, detail="", **kw):
        with self._st_lock:
            self.push_st.update(state=state, detail=detail, **kw)

    def _apply_push(self):
        want = self.cfg["push"]
        running = self._push_stop is not None and not self._push_stop.is_set()
        with self._st_lock:
            self.push_st["enabled"] = want
        if running and not want:
            self._push_stop.set()
            self._set_push("disabled")
            log.info("relay push is off")
        elif want and not running:
            self._push_stop = threading.Event()
            threading.Thread(target=self._push_loop, args=(self._push_stop,),
                             name="peek-push", daemon=True).start()
            log.info("relay push is on")

    def _ssl(self):
        key = self.cfg["tls_verify"]
        if self._ctx is None or self._ctx_key != key:
            self._ctx, self._ctx_key = make_ssl_context(key), key
            if not key:
                log.warning("tls_verify is off: the relay's identity is NOT being checked, "
                            "and the push token travels in that connection")
        return self._ctx

    def _push_loop(self, stop):
        fails = 0
        last_state = None
        while not stop.is_set():
            cfg = self.cfg
            url, token, problem = resolve_target(cfg)
            if problem:
                unpaired = not (cfg["pair_code"] or cfg["relay_url"] or cfg["token"])
                self._set_push("unpaired" if unpaired else "error", problem)
                if last_state != "p:" + problem:
                    log.warning("push: %s", problem)
                    last_state = "p:" + problem
                stop.wait(3.0)
                continue
            with self._st_lock:
                self.push_st["stream"] = url.rsplit("/", 1)[-1]
            if not self.collector.ready():
                self._set_push("warming", "waiting for the first readings")
                stop.wait(1.0)
                continue
            now = time.time()
            if cfg["messages"]:
                self.episodes.update(self.conditions(), now)
            body = json.dumps(self.snapshot(messages=True), separators=(",", ":")).encode()
            code, msg = push_once(url, token, body, PUSH_TIMEOUT, self._ssl())
            if code == 200:
                if fails or last_state != "ok":
                    log.info("push: relay accepted the reading%s",
                             " (recovered after %d failure(s))" % fails if fails else "")
                fails, last_state = 0, "ok"
                self._set_push("ok", "", last_ok=now, last_try=now, fails=0)
            else:
                fails += 1
                self._set_push("error", msg, last_try=now, fails=fails)
                if fails <= 3 or fails % 20 == 0:
                    log.warning("push failed (%d): %s", fails, msg)
                last_state = "error"
            delay = push_delay(cfg["interval"], code, fails)
            if code != 200:
                delay *= random.uniform(0.9, 1.1)    # many NASes, one relay: do not retry in lockstep
            stop.wait(delay)

    # -- the widget feed -----------------------------------------------------
    def _apply_serve(self):
        cfg = self.cfg
        with self._st_lock:
            self.serve_st.update(enabled=cfg["serve"], port=cfg["serve_port"],
                                 bind=cfg["serve_bind"])
        if not cfg["serve"]:
            if self.server:
                self.server.stop()
                self.server = None
                self._serve_key = None
                log.info("widget feed is off")
            with self._st_lock:
                self.serve_st.update(state="disabled", detail="")
            return
        allow = parse_allow(cfg["serve_allow"])
        key = (cfg["serve_bind"], cfg["serve_port"])
        if self.server and self._serve_key == key and not self.server.error:
            self.server.allow = allow                      # allow-list changes need no rebind
            return
        if self.server:
            self.server.stop()
        srv = LanServer(self.snapshot_lan, key[0], key[1], allow)
        if srv.start():
            self.server, self._serve_key = srv, key
            with self._st_lock:
                self.serve_st.update(state="listening", detail="")
            log.info("widget feed on http://%s:%d/telemetry (%s)", key[0], key[1],
                     ", ".join(cfg["serve_allow"]))
        else:
            self.server, self._serve_key = None, None
            with self._st_lock:
                self.serve_st.update(state="error", detail=srv.error)
            log.warning("widget feed could not start: %s - will retry", srv.error)
            self._serve_retry = time.time() + SERVE_RETRY_SECONDS

    # -- status --------------------------------------------------------------
    def status_doc(self):
        with self._st_lock:
            push, serve = dict(self.push_st), dict(self.serve_st)
        _t, drives = self.collector.drive_view()
        raid = self.collector.raid()
        return {
            "version": __version__, "pid": os.getpid(), "started": self.started,
            "updated": time.time(), "host": self.host(), "interval": self.cfg["interval"],
            "push": push, "serve": serve,
            "raid": {"status": raid["status"], "degraded": raid["degraded"],
                     "arrays": [{k: a[k] for k in ("name", "level", "status", "members", "working", "progress")}
                                for a in raid["arrays"]],
                     "system": [a["name"] for a in raid["system"]]},
            "drives": {d: {k: v.get(k) for k in ("state", "temp", "health", "error")}
                       for d, v in drives.items()},
            "reading": self.snapshot(messages=False),
            "messages": len(self.episodes.messages()),
            "warnings": list(self.warnings) + ["%s: %s" % kv for kv in self.collector.errors.items()],
        }

    def write_status(self):
        path = status_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.status_doc(), fh)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError as e:
            if not getattr(self, "_status_warned", False):
                log.warning("cannot write %s: %s", path, e)
                self._status_warned = True

    # -- main loop -----------------------------------------------------------
    def run(self, install_signals=True):
        if install_signals:
            signal.signal(signal.SIGTERM, lambda *_: self.stop_ev.set())
            signal.signal(signal.SIGINT, lambda *_: self.stop_ev.set())
            signal.signal(signal.SIGHUP, lambda *_: self.reload_ev.set())
        log.info("PeekESP Synology agent %s - by %s - %s", __version__, AUTHOR, REPO_URL)
        for w in self.warnings:
            log.warning("config: %s", w)
        self.collector.start()
        self.apply()
        last_status = last_poll = 0.0
        while not self.stop_ev.is_set():
            now = time.time()
            if now - last_poll >= CONFIG_POLL_SECONDS:
                last_poll = now
                if self._cfg_mtime() != self._mtime:
                    self.reload_ev.set()
            if self.reload_ev.is_set():
                self.reload()
            if self.cfg["serve"] and self.server is None and now >= self._serve_retry:
                self._apply_serve()
            if now - last_status >= STATUS_SECONDS:
                last_status = now
                self.write_status()
            self.stop_ev.wait(0.5)
        self.shutdown()

    def shutdown(self):
        log.info("stopping")
        if self._push_stop:
            self._push_stop.set()
        if self.server:
            self.server.stop()
        self.collector.stop()
        try:
            os.unlink(status_path())
        except OSError:
            pass


# ==========================================================================
#  Health, status and the human-readable views
# ==========================================================================
def read_status(path=None):
    txt = read_text(path or status_path())
    try:
        return json.loads(txt) if txt else None
    except ValueError:
        return None


def check_status(st, now=None):
    """-> (exit_code, message). 0 healthy, 1 not, 2 running but not paired."""
    now = time.time() if now is None else now
    if not st:
        return 1, "the agent is not running (no status file)"
    if now - st.get("updated", 0) > 15:
        return 1, "the agent has stopped updating its status (hung, or just killed)"
    try:
        os.kill(int(st["pid"]), 0)
    except PermissionError:
        pass
    except (OSError, KeyError, ValueError):
        return 1, "the agent's process is gone"
    parts = []
    push, serve = st.get("push", {}), st.get("serve", {})
    if push.get("enabled"):
        if push.get("state") == "unpaired":
            return 2, "running, but not paired yet - run: peekesp pair CODE"
        if push.get("state") != "ok":
            return 1, "relay push: %s%s" % (push.get("state"),
                                            " - " + push["detail"] if push.get("detail") else "")
        parts.append("relay accepted the last push %s ago" % fmt_ago(now - push.get("last_ok", now)))
    if serve.get("enabled"):
        if serve.get("state") != "listening":
            return 1, "widget feed: %s%s" % (serve.get("state"),
                                             " - " + serve["detail"] if serve.get("detail") else "")
        parts.append("widget feed listening on port %s" % serve.get("port"))
    return 0, ", ".join(parts) or "running, with both outputs switched off"


def healthcheck(wait=0, out=print):
    deadline = time.time() + wait
    announced = False
    while True:
        code, msg = check_status(read_status())
        if code in (0, 2) or time.time() >= deadline:
            break
        if wait and not announced:
            out("waiting up to %d s for the first successful push (a sleeping relay can take "
                "a minute to answer)..." % wait)
            announced = True
        time.sleep(1.5)
    out(("ok: " if code == 0 else "not ok: ") + msg)
    return code


def format_status(st, now=None):
    now = time.time() if now is None else now
    code, why = check_status(st, now)
    if not st or code == 1 and "not running" in why:
        return "PeekESP agent: not running"
    r, p, s = st.get("reading", {}), st.get("push", {}), st.get("serve", {})
    lines = ["PeekESP agent %s   host: %s   up %s" % (
        st.get("version"), st.get("host"), fmt_ago(now - st.get("started", now)))]

    if not p.get("enabled"):
        lines.append("  relay push    OFF")
    elif p.get("state") == "ok":
        lines.append("  relay push    ON   ok - last push %s ago - stream %s" % (
            fmt_ago(now - p.get("last_ok", now)), p.get("stream") or "?"))
    else:
        lines.append("  relay push    ON   %s%s" % (p.get("state"),
                                                   " - " + p["detail"] if p.get("detail") else ""))
    if not s.get("enabled"):
        lines.append("  widget feed   OFF")
    elif s.get("state") == "listening":
        lines.append("  widget feed   ON   listening on %s:%s" % (s.get("bind"), s.get("port")))
    else:
        lines.append("  widget feed   ON   %s - %s" % (s.get("state"), s.get("detail")))

    if r:
        temp = "%.0f\u00b0" % r["cpu_temp_c"] if r.get("cpu_temp_c", -1) >= 0 else "--"
        lines.append("  reading       CPU %.0f%%  RAM %.0f%%  storage %.0f%% (%s free)  temp %s  "
                     "RX %.0fK TX %.0fK" % (
                         r.get("cpu_percent", 0), r.get("ram_percent", 0), r.get("storage_percent", 0),
                         fmt_gb(r.get("storage_free_gb", 0)), temp,
                         r.get("net_rx_kbps", 0), r.get("net_tx_kbps", 0)))
    rd = st.get("raid", {})
    arr = ", ".join("%s %s %s" % (a["name"], a.get("level") or "", a["status"])
                    + (" %s/%s" % (a["working"], a["members"]) if a.get("members") else "")
                    for a in rd.get("arrays", []))
    lines.append("  RAID          %s%s" % (rd.get("status", "?"), "   (" + arr + ")" if arr else ""))
    drives = st.get("drives", {})
    if drives:
        asleep = sum(1 for d in drives.values() if d.get("state") == "standby")
        temps = [d["temp"] for d in drives.values() if d.get("temp") is not None]
        lines.append("  drives        %d found, %d asleep (left alone)%s" % (
            len(drives), asleep, ", hottest %.0f\u00b0" % max(temps) if temps else ""))
    if st.get("messages"):
        lines.append("  messages      %d in the readings (phone alerts)" % st["messages"])
    for w in st.get("warnings", [])[:4]:
        lines.append("  warning       " + w)
    return "\n".join(lines)


def doctor(cfg, out=print):
    """What can this NAS report, and from where? The thing to paste into an issue."""
    root = os.geteuid() == 0 if hasattr(os, "geteuid") else False
    out("PeekESP Synology agent %s  -  doctor" % __version__)
    out("")
    out("platform")
    ver = read_text("/etc.defaults/VERSION") or ""
    m = re.search(r'productversion="([^"]*)"', ver)
    b = re.search(r'buildnumber="([^"]*)"', ver)
    out("  DSM          %s" % (("%s-%s" % (m.group(1), b.group(1) if b else "?")) if m else
                               "not detected (this does not look like a Synology)"))
    out("  model        %s" % ((read_text("/proc/sys/kernel/syno_hw_version") or "unknown").strip()))
    out("  python       %s" % sys.version.split()[0])
    out("  kernel       %s" % (os.uname().release if hasattr(os, "uname") else "?"))
    out("  running as   %s%s" % ("root" if root else "a normal user",
                                 "" if root else "  (smartctl needs root: run `sudo peekesp doctor`)"))
    out("")
    c = Collector(cfg)
    c.sample_fast()
    time.sleep(1.2)
    c.sample_fast()
    c.sample_slow()
    snap, usage = c.reading()
    out("readings")
    out("  CPU          %.1f%%   (iowait counted as idle)" % snap["cpu_percent"])
    out("  RAM          %.1f%%   (MemAvailable, so DSM's filesystem cache is not counted as used)" % snap["ram_percent"])
    out("  uptime       %s" % fmt_ago(snap["uptime_seconds"]))
    temp, src = read_cpu_temp()
    out("  CPU temp     %s" % ("%.1f C   from %s" % (temp, src) if temp >= 0 else
                               "none found (reported as -1; this model may not expose one)"))
    out("  network      %s" % (", ".join(c.ifaces) or "no eth*/bond* interface found"))
    out("")
    out("volumes")
    if usage:
        for v in usage["volumes"]:
            out("  %-10s %s total, %s free, %.0f%% used" % (
                v["path"], fmt_gb(v["total"] / 1024.0 ** 3), fmt_gb(v["avail"] / 1024.0 ** 3), v["percent"]))
    else:
        out("  none found. Expected mount points like /volume1; if yours differ, set them with:")
        out("    peekesp set volumes /your/mountpoint")
    out("")
    out("RAID  (/proc/mdstat)")
    arrays = parse_mdstat(read_text("/proc/mdstat"))
    if arrays is None:
        out("  /proc/mdstat is not readable")
    for a in arrays or []:
        sysmark = "  <- DSM system/swap: shown, but does not trip the alarm" if (
            a["name"] in SYSTEM_ARRAYS and not cfg["raid_system"]) else ""
        out("  %-5s %-8s %-10s %s%s%s" % (
            a["name"], a["level"] or "-", a["status"],
            "%s/%s drives" % (a["working"], a["members"]) if a["members"] else "",
            "  %s %s" % (a["action"], a["progress"]) if a["action"] else "", sysmark))
    out("")
    out("drives  (smartctl -n standby: a sleeping drive is skipped, never woken)")
    smartctl = find_exe("smartctl", SMARTCTL_PATHS)
    disks = list_disks()
    out("  smartctl     %s" % (smartctl or "not found - drive temperatures are unavailable"))
    if smartctl:
        for dev in disks:
            r = probe_drive(dev, smartctl)
            if r["state"] == "ok":
                out("  %-8s     %s  health %s" % (
                    dev, "%.0f C" % r["temp"] if r["temp"] is not None else "no temp", r["health"] or "?"))
            elif r["state"] == "standby":
                out("  %-8s     asleep - left alone" % dev)
            else:
                out("  %-8s     %s" % (dev, r["error"]))
        if not disks:
            out("  no internal drives found under /sys/block")
    synodir = "/run/synostorage/disks"
    if os.path.isdir(synodir):
        files = []
        for d in sorted(os.listdir(synodir))[:8]:
            try:
                files += ["%s/%s" % (d, f) for f in sorted(os.listdir(os.path.join(synodir, d)))[:6]]
            except OSError:
                pass
        out("  %s exists (not read automatically; its layout varies by DSM release):" % synodir)
        out("    %s" % (", ".join(files) or "(empty)"))
    out("")
    out("UPS")
    upsc = find_exe("upsc", ("/usr/syno/bin/upsc", "/usr/bin/upsc"))
    if upsc:
        c.sample_ups()
        u = c.ups_view()
        out("  upsc         %s" % upsc)
        out("  reading      %s" % (("%d%%  %s" % (u["battery_percent"], "on AC" if u["battery_ac"] else "ON BATTERY"))
                                   if u else "no UPS answered (is one enabled in Control Panel > Hardware & Power?)"))
    else:
        out("  upsc not found; battery is reported as -1 (no battery)")
    out("")
    out("settings")
    out("  relay push   %s    widget feed  %s (%s:%d, allow %s)" % (
        "ON" if cfg["push"] else "OFF", "ON" if cfg["serve"] else "OFF",
        cfg["serve_bind"], cfg["serve_port"], ", ".join(cfg["serve_allow"])))
    url, _tok, problem = resolve_target(cfg)
    out("  paired       %s" % ("yes, stream %s..." % url.rsplit("/", 1)[-1][:6] if url else problem))
    out("")
    out("PeekESP by %s - Discord: %s - %s - %s" % (AUTHOR, DISCORD, REPO_URL, SITE_URL))
    return 0


def mask(cfg):
    safe = dict(cfg)
    if safe["pair_code"]:
        c = format_code(safe["pair_code"])
        safe["pair_code"] = c[:4] + "-****-**"
    if safe["token"]:
        safe["token"] = safe["token"][:4] + "..."
    return safe


# ==========================================================================
#  Command line
# ==========================================================================
def setup_logging(log_file=None):
    root = logging.getLogger("peekesp")
    root.setLevel(logging.INFO)
    root.handlers = []
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if log_file:
        # tmpfs, and capped: this is a log, not a database. With a file the
        # stderr handler is left off so the supervisor's crash capture holds
        # tracebacks and nothing else.
        try:
            h = logging.handlers.RotatingFileHandler(log_file, maxBytes=128 * 1024, backupCount=1)
            h.setFormatter(fmt)
            root.addHandler(h)
            return
        except OSError as e:
            sys.stderr.write("cannot open log file %s: %s\n" % (log_file, e))
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    root.addHandler(h)


def about():
    return ("PeekESP Synology agent %s\n  by %s  -  Discord: %s\n  repo  %s\n  site  %s"
            % (__version__, AUTHOR, DISCORD, REPO_URL, SITE_URL))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="PeekESP agent for Synology DSM 7.x. With no action flag it runs.",
        epilog="PeekESP by %s - Discord: %s - %s" % (AUTHOR, DISCORD, SITE_URL))
    ap.add_argument("--once", action="store_true", help="print one reading and exit")
    ap.add_argument("--test", action="store_true", help="one reading and one real push; report what happened")
    ap.add_argument("--doctor", action="store_true", help="what can this NAS report, and from where")
    ap.add_argument("--status", action="store_true", help="show the running agent's status")
    ap.add_argument("--show", action="store_true", help="print the settings (secrets hidden)")
    ap.add_argument("--healthcheck", action="store_true", help="exit 0 if the running agent is healthy")
    ap.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="with --healthcheck: wait this long")
    ap.add_argument("--pair", metavar="CODE", help="pair with the code on the device's screen")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="change a setting (repeatable): " + ", ".join(sorted(SETTABLE)))
    ap.add_argument("--interval", type=float, help="seconds between pushes, for this run only")
    ap.add_argument("--port", type=int, help="widget feed port, for this run only")
    ap.add_argument("--no-push", action="store_true", help="relay push off, for this run only")
    ap.add_argument("--no-serve", action="store_true", help="widget feed off, for this run only")
    ap.add_argument("--log-file", metavar="PATH", help="log here (rotated) instead of stderr")
    ap.add_argument("--config-dir", metavar="DIR", help="where config.json lives")
    ap.add_argument("--about", "--version", action="store_true", dest="about")
    a = ap.parse_args(argv)

    if a.about:
        print(about())
        return 0
    directory = a.config_dir or config_dir()
    setup_logging(a.log_file)

    if a.healthcheck:
        return healthcheck(a.wait)
    if a.status:
        print(format_status(read_status()))
        return 0

    if a.pair:
        try:
            d = derive(a.pair)
        except ValueError as e:
            print("error: %s" % e, file=sys.stderr)
            return 2
        cfg, _w = load_config(directory)
        cfg.update(pair_code=d["code"], push=True, relay_url="", token="")
        try:
            save_config(cfg, directory)
        except OSError as e:
            print("error: cannot save settings: %s (run as root?)" % e, file=sys.stderr)
            return 1
        print("paired with %s  (stream %s)" % (format_code(d["code"]), d["stream"]))
        return 0

    if a.set:
        cfg, _w = load_config(directory)
        for item in a.set:
            k, sep, v = item.partition("=")
            k = k.strip().lower().replace("_", "-")
            if not sep or k not in SETTABLE:
                print("error: unknown setting '%s'. Settings: %s" % (k, ", ".join(sorted(SETTABLE))),
                      file=sys.stderr)
                return 2
            try:
                cfg[SETTABLE[k]] = PARSERS[SETTABLE[k]](v)
            except (ValueError, TypeError) as e:
                print("error: %s %s" % (k, e), file=sys.stderr)
                return 2
            print("%s = %s" % (k, cfg[SETTABLE[k]] if k != "name" or cfg["name"] else "(hostname)"))
            if k == "tls-verify" and not cfg["tls_verify"]:
                print("warning: with this off the relay's identity is not checked and the push "
                      "token travels in that connection", file=sys.stderr)
            if k == "relay" and cfg["relay_base"].lower().startswith("http://"):
                print("warning: http:// sends the push token unencrypted", file=sys.stderr)
        try:
            save_config(cfg, directory)
        except OSError as e:
            print("error: cannot save settings: %s (run as root?)" % e, file=sys.stderr)
            return 1
        return 0

    cfg, warns = load_config(directory)
    warns += apply_env(cfg, os.environ)
    if a.show:
        print(json.dumps(mask(cfg), indent=2))
        for w in warns:
            print("warning: " + w, file=sys.stderr)
        return 0
    if a.doctor:
        return doctor(cfg)

    if a.once or a.test:
        c = Collector(cfg)
        c.sample_fast()
        time.sleep(1.2)                 # a real CPU/network delta, not a guess
        c.sample_fast()
        c.sample_slow()
        c.sample_drives()
        c.sample_ups()
        ag = Agent(directory, environ=os.environ)
        ag.collector = c
        snap = ag.snapshot(messages=True)
        print(json.dumps(snap, indent=2))
        if not a.test:
            return 0
        url, token, problem = resolve_target(ag.cfg)
        if problem:
            print("\npush: skipped - %s" % problem)
            return 1
        print("\npushing to %s/<stream> ..." % url.rsplit("/", 1)[0])
        code, msg = push_once(url, token, json.dumps(snap, separators=(",", ":")).encode(),
                              PUSH_TIMEOUT, make_ssl_context(ag.cfg["tls_verify"]))
        print("push: %s" % ("relay accepted it (HTTP 200)" if code == 200 else msg))
        if code == 0:
            print("      a sleeping relay can take up to a minute on the first request; run it again")
        return 0 if code == 200 else 1

    overrides = {}
    if a.interval is not None:
        overrides["interval"] = max(1.0, min(600.0, a.interval))
    if a.port is not None:
        overrides["serve_port"] = a.port
    if a.no_push:
        overrides["push"] = False
    if a.no_serve:
        overrides["serve"] = False
    if not acquire_instance_lock(os.path.join(run_dir(), "peekesp.lock")):
        log.error("another PeekESP agent is already running on this machine; exiting")
        return 3
    Agent(directory, overrides, log_file=a.log_file).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
