"""
peek_alerts.py - rules that watch this PC and leave messages when one trips.

The phone has rules of its own and judges them on whatever it last read. These
run here, on every sample, whether or not the phone is looking - so a spike at
3 a.m. is caught while the phone is asleep and is waiting in the next reading.
Use one or the other for a given condition: both firing means two notifications.

A rule is a metric, a comparison and how long it must hold:

    {"id": "cpu_high", "metric": "cpu_percent", "op": "above", "limit": 90,
     "for": 300, "severity": "warning"}

op is "above", "below" or "unusual". Unusual is judged against this PC's own
last 24 hours, by median and median absolute deviation, so one spike cannot
stretch its own yardstick. It needs about six hours of history before it will
say anything, and keeps that history on disk so a restart does not forget.

What this cannot do is notice that THIS PC went offline: a machine that has
stopped cannot report it. That one is the phone's - it sees the relay go quiet.
The nearest thing available here is the relay link: the tray app tells you
when it cannot reach the relay, and when it can again.

Standard library only. No Win32 in here, so all of it is testable anywhere.
"""

import copy
import json
import math
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from peek_messages import norm_severity
from peek_sensors import KEY_RE, label_for_key, robust_scale, unit_for_key

OPS = ("above", "below", "unusual")
MAX_RULES = 50
CLEAR_HOLD = 30.0             # seconds a reading must stay normal before "recovered"
MIN_HISTORY = 360             # one-minute buckets: six hours
HIST_KEEP = 1440              # and a day
UNUSUAL_K = 5.0               # how many robust deviations counts as unusual

DEFAULT_QUIET = {
    "enabled": False, "start": "22:00", "end": "07:00",
    "days": [0, 1, 2, 3, 4, 5, 6],             # Monday = 0; the day the window begins
    "allow_critical": True,
}

DEFAULT_RULES = [
    {"id": "cpu_high", "metric": "cpu_percent", "op": "above", "limit": 90,
     "for": 300, "severity": "warning"},
    {"id": "ram_high", "metric": "ram_percent", "op": "above", "limit": 90,
     "for": 300, "severity": "warning"},
    {"id": "cpu_hot", "metric": "cpu_temp_c", "op": "above", "limit": 90,
     "for": 60, "severity": "critical"},
    {"id": "storage_full", "metric": "storage_percent", "op": "above", "limit": 92,
     "for": 0, "severity": "warning"},
    {"id": "battery_low", "metric": "battery_percent", "op": "below", "limit": 20,
     "for": 0, "severity": "warning", "unplugged": True},
    {"id": "battery_critical", "metric": "battery_percent", "op": "below", "limit": 8,
     "for": 0, "severity": "critical", "unplugged": True},
    {"id": "net_unusual", "metric": "net_kbps", "op": "unusual", "min": 2000,
     "for": 300, "severity": "warning"},
]


def default_alerts():
    rules = []
    for r in DEFAULT_RULES:
        r = dict(r)
        r.setdefault("enabled", True)
        r.setdefault("min", 0.0)
        r.setdefault("unplugged", False)
        r.setdefault("recovery", True)
        r.setdefault("title", "")
        r.setdefault("body", "")
        r["limit"] = float(r.get("limit", 0))
        r["min"] = float(r["min"])
        rules.append(r)
    return {
        "enabled": True,            # evaluate rules on this PC
        "local_toast": True,        # desktop notification for each message
        "local_ring": True,         # ring on this PC for a critical one
        "quiet": copy.deepcopy(DEFAULT_QUIET),
        "rules": rules,
    }


# --------------------------------------------------------------------------
#  Validation
# --------------------------------------------------------------------------
def _hhmm(s):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(s or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


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
                warns.append(f"quiet hours: {k} must look like 22:00")
    days = raw.get("days", q["days"])
    if isinstance(days, list):
        d = sorted({int(x) for x in days
                    if isinstance(x, (int, float)) and 0 <= int(x) <= 6})
        q["days"] = d
    q["allow_critical"] = bool(raw.get("allow_critical", True))
    return q, warns


def clean_rule(raw, i, taken):
    """-> (rule or None, warning or None)."""
    if not isinstance(raw, dict):
        return None, f"rule #{i + 1}: not an object"
    metric = str(raw.get("metric", "")).strip().lower()
    if not KEY_RE.match(metric):
        return None, f"rule #{i + 1}: metric must be a-z, 0-9 and _"
    op = str(raw.get("op", "")).strip().lower()
    if op not in OPS:
        return None, f"rule '{metric}': op must be above, below or unusual"
    try:
        limit = float(raw.get("limit", 0.0))
        if op != "unusual" and (not math.isfinite(limit) or "limit" not in raw):
            raise ValueError
    except (TypeError, ValueError):
        return None, f"rule '{metric}': a numeric limit is needed"
    try:
        floor = float(raw.get("min", 0.0))
    except (TypeError, ValueError):
        floor = 0.0
    try:
        hold = int(max(0, min(86400, float(raw.get("for", 0)))))
    except (TypeError, ValueError):
        hold = 0
    rid = re.sub(r"[^a-z0-9_-]", "", str(raw.get("id", "")).lower())[:32] or f"rule{i + 1}"
    base, n = rid, 2
    while rid in taken:
        rid = f"{base}-{n}"
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


def clean_alerts(raw):
    """The "alerts" block of config.json -> (clean dict, warnings).

    Missing means the defaults. A rules list that is present but empty means
    the person deleted them all, and stays empty.
    """
    out = default_alerts()
    warns = []
    if not isinstance(raw, dict):
        return out, warns
    for k in ("enabled", "local_toast", "local_ring"):
        if k in raw:
            out[k] = bool(raw[k])
    out["quiet"], w = clean_quiet(raw.get("quiet"))
    warns += w
    if isinstance(raw.get("rules"), list):
        taken, rules = set(), []
        for i, r in enumerate(raw["rules"][:MAX_RULES]):
            rule, warn = clean_rule(r, i, taken)
            if rule:
                rules.append(rule)
            else:
                warns.append(warn)
        out["rules"] = rules
    return out, warns


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


def local_policy(severity, alerts, dt):
    """(show a notification, ring) for a message arriving on this PC.

    Quiet hours silence this PC. They do not hold anything back from the phone:
    that has quiet hours of its own, and a message dropped here would never
    reach its Alerts tab at all.
    """
    toast = bool(alerts.get("local_toast", True))
    ring = bool(alerts.get("local_ring", True)) and severity == "critical"
    q = alerts.get("quiet", {})
    if in_quiet(q, dt) and not (severity == "critical" and q.get("allow_critical", True)):
        return False, False
    return toast, ring


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
    s = f"{v:.1f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def fmt_value(v, unit):
    if not unit:
        return _num(v)
    return _num(v) + (unit if unit in ("%", "\u00b0C", "\u00b0F") else " " + unit)


def fmt_duration(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} s"
    if sec < 5400:
        return f"{round(sec / 60)} min"
    h, m = divmod(round(sec / 60), 60)
    return f"{h} h" + (f" {m} min" if m else "")


def describe_rule(r):
    """One line for the rules list: "CPU above 90% for 5 min - warning"."""
    label, unit = label_for_key(r["metric"]), unit_for_key(r["metric"])
    if r["op"] == "above":
        what = f"{label} above {fmt_value(r['limit'], unit)}"
    elif r["op"] == "below":
        what = f"{label} below {fmt_value(r['limit'], unit)}"
    else:
        what = f"{label} unusually high"
        if r["min"]:
            what += f" (over {fmt_value(r['min'], unit)})"
    if r["for"]:
        what += f" for {fmt_duration(r['for'])}"
    what += f"  \u2013  {r['severity']}"
    if r["unplugged"]:
        what += ", on battery only"
    return ("\u2611 " if r["enabled"] else "\u2610 ") + what


def fill(template, ctx):
    """{name} substitution. Done by hand rather than str.format so a template
    cannot reach into attributes, and an unknown {name} is left as written."""
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), m.group(0))), template)


# --------------------------------------------------------------------------
#  The engine
# --------------------------------------------------------------------------
class AlertEngine:
    def __init__(self, values_fn, outbox, alerts, host, history_path=None,
                 clock=time.time, interval=5.0):
        self.values_fn, self.outbox, self.host = values_fn, outbox, host
        self.history_path = Path(history_path) if history_path else None
        self._clock, self.interval = clock, interval
        self.state = {}               # rule id -> episode bookkeeping; survives configure()
        self.hist = {}                # metric -> [(minute, mean)]
        self._acc = {}                # metric -> [minute, sum, n]
        self.last_error = ""
        self.alerts = alerts
        self.rules = []
        self._tracked = set()
        self._stop = threading.Event()
        self._thread = None
        self._last_save = 0.0
        self.configure(alerts)
        self._load_history()

    def configure(self, alerts):
        """Swap in new rules without forgetting which are mid-incident, so
        saving settings during an alert does not make the phone ring again."""
        self.alerts = alerts
        self.rules = [r for r in alerts["rules"] if r["enabled"]] if alerts["enabled"] else []
        self._tracked = {r["metric"] for r in self.rules if r["op"] == "unusual"}
        live = {r["id"] for r in self.rules}
        self.state = {k: v for k, v in self.state.items() if k in live or v.get("active")}

    # -- thread ------------------------------------------------------------
    def start(self):
        self._stop.set()
        stop = self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(stop,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._save_history()

    def _loop(self, stop):
        while not stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:         # keep watching; say so, do not die quietly
                self.last_error = f"{type(e).__name__}: {e}"

    # -- history -----------------------------------------------------------
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
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.history_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({k: [[a, round(b, 2)] for a, b in v] for k, v in self.hist.items()}, fh)
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

    # -- evaluation --------------------------------------------------------
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
            title = f"{label} back to normal on {self.host}"
            body = f"{label} is {ctx['value']}."
            sev, mid = "info", f"pc-{rule['id']}-{episode}-ok"
        else:
            sev, mid = rule["severity"], f"pc-{rule['id']}-{episode}"
            if rule["op"] == "above":
                title = f"{label} high on {self.host}"
                body = f"{label} is {ctx['value']}, above {ctx['limit']} for {ctx['duration']}."
            elif rule["op"] == "below":
                title = f"{label} low on {self.host}"
                body = f"{label} is {ctx['value']}, below {ctx['limit']}."
            else:
                title = f"Unusual {label} on {self.host}"
                body = (f"{label} is {ctx['value']}, far above its usual "
                        f"{ctx['typical']} here, for {ctx['duration']}.")
            if rule["title"]:
                title = fill(rule["title"], ctx)
            if rule["body"]:
                body = fill(rule["body"], ctx)
        self.outbox.add({"id": mid, "severity": sev, "title": title, "body": body,
                         "source": "PeekESP rules"}, internal=True)