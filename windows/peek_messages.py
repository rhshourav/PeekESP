"""
peek_messages.py - messages a machine leaves for the phone.

The phone reads a "messages" list inside each reading (PROTOCOL.md section 2).
This module is the machine's side of that: an outbox that every reading
carries a window of, and three ways for a program on this PC to put something
in it.

    1. a local HTTP endpoint     POST http://127.0.0.1:6598/notify   (needs the key)
    2. a watched folder          drop a .json file in %APPDATA%\\PeekESP\\outbox
    3. the command line          peek-agent.exe notify --severity red --title ...

A reading is "the latest state", not a queue: a phone that looks every fifteen
minutes with the app closed would miss a message sent once. So a message stays
in every reading for message_ttl_hours (six by default), and its id stops it
becoming a second alert each time it is re-sent.

Standard library only.
"""

import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SEVERITIES = ("info", "warning", "critical")
_RANK = {"info": 0, "warning": 1, "critical": 2}

# Exactly the words PROTOCOL.md promises the phone understands.
_CRITICAL = {"critical", "red", "high", "alert", "emergency", "fatal", "error"}
_WARNING = {"warning", "yellow", "amber", "orange", "warn", "medium"}

TITLE_MAX, BODY_MAX, SOURCE_MAX = 80, 400, 40
MAX_PER_READING = 10           # the phone reads at most this many
MAX_STORED = 100
RATE_PER_MINUTE = 20           # across every outside channel, like the phone
DEFAULT_TTL_HOURS = 6
MAX_BODY_BYTES = 16384

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


def rank(severity):
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


def build(raw, now, default_source="Local app"):
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
    restart does not drop an alert that is still inside its window."""

    def __init__(self, path=None, ttl_hours=DEFAULT_TTL_HOURS, host="",
                 clock=time.time):
        self.path = Path(path) if path else None
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

    # -- configuration -------------------------------------------------------
    def configure(self, ttl_hours=None, host=None):
        with self._lock:
            if ttl_hours is not None:
                self.ttl = max(1.0, float(ttl_hours)) * 3600.0
            if host is not None:
                self.host = host

    def on_new(self, callback):
        """callback(message) runs once for each message first seen, never for a
        re-send and never for one restored from disk."""
        with self._lock:
            self._listeners.append(callback)

    # -- adding ----------------------------------------------------------------
    def add(self, raw, source=None, internal=False):
        """-> (status, message). status is new, duplicate, rate or empty.

        `internal` skips the per-minute limit: it is there to stop another
        program flooding the phone, not to stop this agent's own rules.
        """
        now = self._clock()
        m = build(raw, now, source or "Local app")
        if m is None:
            return "empty", None
        with self._lock:
            self._prune(now)
            base = m["id"] or uuid.uuid4().hex[:12]
            m["id"] = f"{self.host}:{base}"[:64] if self.host else base[:64]
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
            except Exception as e:         # a broken listener must not lose the message
                self.listener_error = repr(e)
        return "new", out

    # -- reading ---------------------------------------------------------------
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
            ordered = sorted(self._msgs, key=lambda m: (-rank(m["severity"]), -m["ts"]))
            return [self._public(m) for m in ordered[:limit]]

    def recent(self, n=20):
        with self._lock:
            return [self._public(m) for m in reversed(self._msgs[-n:])]

    # -- housekeeping ------------------------------------------------------
    def _prune(self, now):
        keep = [m for m in self._msgs if now - m["at"] <= self.ttl]
        if len(keep) != len(self._msgs):
            self._msgs = keep
            self._save()

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
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
#  Channel 2: a watched folder, and the command-line sender that feeds it
# --------------------------------------------------------------------------
def drop_file(folder, raw):
    """Write one message where the watcher will find it. Written under a
    temporary name and renamed, so the watcher never reads half a file."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    tmp = folder / (name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    final = folder / (name + ".json")
    os.replace(tmp, final)
    return final


class OutboxWatcher:
    """Polls a folder every couple of seconds; stdlib has no file-change API."""

    def __init__(self, outbox, folder, every=2.0):
        self.outbox, self.folder, self.every = outbox, Path(folder), every
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        self._stop.set()
        stop = self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(stop,), daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self, stop):
        while not stop.wait(self.every):
            self.scan()

    def _reject(self, p):
        try:
            d = self.folder / "rejected"
            d.mkdir(exist_ok=True)
            os.replace(p, d / p.name)
        except OSError:
            pass

    def scan(self):
        try:
            files = sorted(p for p in self.folder.glob("*.json"))[:20]
        except OSError:
            return
        for p in files:
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                st = p.stat()
                if st.st_size > MAX_BODY_BYTES:
                    self._reject(p)
                    continue
                with open(p, "r", encoding="utf-8") as fh:
                    obj = json.load(fh)
            except ValueError:
                # Either a bad file or one a naive tool is still writing in
                # place. Give it a few seconds before calling it bad.
                try:
                    if time.time() - p.stat().st_mtime > 5:
                        self._reject(p)
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
                        f"{p.name}/{i}".encode()).hexdigest()[:16]
                status, _m = self.outbox.add(raw, source="Local app")
                if status == "rate":
                    limited = True
                    break
            if limited:
                return                   # leave it; the next scan tries again
            if not msgs:
                self._reject(p)
                continue
            try:
                p.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------
#  Channel 1: a local HTTP endpoint for programs that would rather POST
# --------------------------------------------------------------------------
def _make_handler(outbox, key, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}
    key_b = key.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
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
                msgs = parse_payload(json.loads(self.rfile.read(n)))
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

    return Handler


class NotifyServer:
    """Loopback only. Binding 127.0.0.1 rather than 0.0.0.0 is the point: the
    key stops other programs on this PC, the bind stops everything else."""

    def __init__(self, outbox, key, port):
        self.outbox, self.key, self.port = outbox, key, port
        self._httpd = None
        self.error = ""

    def start(self):
        try:
            self._httpd = ThreadingHTTPServer(
                ("127.0.0.1", self.port), _make_handler(self.outbox, self.key, self.port))
        except OSError as e:
            self.error = f"port {self.port}: {e.strerror or e}"
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