"""Alerts on the display: what to show, when, and whether to make a noise.

The agents decide *what is wrong* - they run the alert rules and carry messages
from security tools. This is the other end: a screen on a shelf that can show
it. Three kinds of thing become an alert here:

  * a message a machine left in its reading (a rule that tripped, a tool that
    saw something), with the severity the sender gave it;
  * a machine going offline, and coming back - which the display can see for
    itself, from the age the relay reports;
  * this display losing the relay.

What the display does with one depends on severity, like the phone:

    info       a notice on screen for a few seconds. No sound.
    warning    on screen for half a minute, and two short beeps.
    critical   on screen, and a buzzer that repeats, until a button is pressed
               or ten minutes pass.

Nothing here knows what a panel is. It hands the renderer one Alert to draw and
tells an Alarm to be loud or quiet; the Alarm is a GPIO buzzer, or nothing.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from dataclasses import dataclass, field

from .relay import AuthError, severity_rank

log = logging.getLogger("peekdisplay")

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class AlertHardwareError(RuntimeError):
    """A buzzer or button could not be opened. The message names the fix."""


# ---------------------------------------------------------------------------
#  Quiet hours
# ---------------------------------------------------------------------------


@dataclass
class Quiet:
    """A window in which the display stays out of the way.

    `days` are the days the window BEGINS, so a Friday 22:00-07:00 window still
    covers Saturday at 03:00, which is what anyone setting it means.
    """

    enabled: bool = False
    start: int = 0                  # minutes after midnight
    end: int = 0
    days: tuple = (0, 1, 2, 3, 4, 5, 6)     # Monday = 0
    allow_critical: bool = True

    def active(self, dt):
        if not self.enabled or self.start == self.end:
            return False
        minute = dt.hour * 60 + dt.minute
        weekday = dt.weekday()
        if self.start < self.end:
            return weekday in self.days and self.start <= minute < self.end
        if minute >= self.start:
            return weekday in self.days
        if minute < self.end:
            return (weekday - 1) % 7 in self.days
        return False

    def allows(self, severity, dt):
        """May something of this severity be shown, or sound, right now?"""
        if not self.active(dt):
            return True
        return severity == "critical" and self.allow_critical


def _hhmm(text):
    parts = text.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    return h * 60 + m if h < 24 and m < 60 else None


def parse_quiet(spec, days="all", allow_critical=True):
    """"22:00-07:00" -> Quiet. A spec that does not parse disables quiet hours
    and says why: failing open (making noise) is the safer mistake to make."""
    spec = (spec or "").strip()
    if not spec:
        return Quiet()
    start_s, _, end_s = spec.partition("-")
    start, end = _hhmm(start_s), _hhmm(end_s)
    if start is None or end is None:
        log.warning("PEEK_QUIET must look like 22:00-07:00, not %r; quiet hours are off", spec)
        return Quiet()
    d = (days or "all").strip().lower()
    if d in ("all", "daily", ""):
        chosen = tuple(range(7))
    elif d == "weekdays":
        chosen = (0, 1, 2, 3, 4)
    elif d == "weekends":
        chosen = (5, 6)
    else:
        try:
            chosen = tuple(sorted(set(DAYS.index(x.strip()[:3]) for x in d.split(",")
                                      if x.strip())))
        except ValueError:
            log.warning("PEEK_QUIET_DAYS is all, weekdays, weekends or mon,tue,...; "
                        "using every day")
            chosen = tuple(range(7))
    return Quiet(True, start, end, chosen, bool(allow_critical))


# ---------------------------------------------------------------------------
#  Settings
# ---------------------------------------------------------------------------


@dataclass
class Settings:
    enabled: bool = True
    offline_alerts: bool = True
    offline_severity: str = "warning"
    info_seconds: float = 10.0
    warning_seconds: float = 30.0
    critical_seconds: float = 600.0
    # A message older than this when the display first sees it is filed quietly.
    # The agents keep a message in every reading for hours so a phone that was
    # off can catch up; a display that has just been powered on is in the same
    # position, and must not ring for something from last night.
    max_age_s: float = 900.0
    quiet: Quiet = field(default_factory=Quiet)
    quiet_screen: str = "none"          # none | dim | off

    @classmethod
    def from_config(cls, cfg):
        severity = str(cfg.get("PEEK_OFFLINE_SEVERITY", "warning")).lower()
        screen = str(cfg.get("PEEK_QUIET_SCREEN", "none")).lower()
        if screen not in ("none", "dim", "off"):
            log.warning("PEEK_QUIET_SCREEN is none, dim or off, not %r", screen)
            screen = "none"
        return cls(
            enabled=cfg.flag("PEEK_ALERTS", True),
            offline_alerts=cfg.flag("PEEK_OFFLINE_ALERTS", True),
            offline_severity=severity if severity in ("info", "warning", "critical")
            else "warning",
            info_seconds=max(1.0, cfg.float("PEEK_INFO_SECONDS", 10)),
            warning_seconds=max(1.0, cfg.float("PEEK_ALERT_SECONDS", 30)),
            critical_seconds=max(5.0, cfg.float("PEEK_CRITICAL_SECONDS", 600)),
            max_age_s=max(30.0, cfg.float("PEEK_ALERT_MAX_AGE", 900)),
            quiet=parse_quiet(cfg.get("PEEK_QUIET", ""), cfg.get("PEEK_QUIET_DAYS", "all"),
                              cfg.flag("PEEK_QUIET_CRITICAL", True)),
            quiet_screen=screen,
        )


# ---------------------------------------------------------------------------
#  The centre
# ---------------------------------------------------------------------------


@dataclass
class Alert:
    key: str
    severity: str
    title: str
    body: str = ""
    source: str = ""
    host: str = ""
    born: float = 0.0               # monotonic
    expires: float = 0.0
    acked: bool = False


class AlertCenter:
    """Turns successive snapshots into a short list of things worth showing."""

    SEEN_FOR = 24 * 3600.0

    def __init__(self, settings=None, offline_after_s=150, clock=time.time,
                 mono=time.monotonic):
        self.settings = settings or Settings()
        self._offline_after = int(offline_after_s)
        self._clock, self._mono = clock, mono
        self._alerts = []
        self._seen = {}                 # (host, id) -> monotonic first seen
        self._online = {}               # host -> bool, once known
        self._offline_since = {}
        self._link_down_since = None
        self._link_alerted = False

    # -- ingesting -----------------------------------------------------------
    def update(self, snapshot, error=None):
        """Look at one poll. Returns the alerts that are NEW and may be shown."""
        s = self.settings
        if not s.enabled:
            return []
        now_w, now_m = self._clock(), self._mono()
        new = []

        # The relay link. Judged first, because everything below is only
        # meaningful when this display actually heard from the relay: a Pi that
        # lost its network would otherwise report every machine as having died
        # at the same moment, which is exactly backwards.
        reachable = error is None
        if reachable:
            if self._link_alerted:
                self._link_alerted = False
                self._push(new, "link-up", "info", "Relay reachable again",
                           "", "PeekESP display", "", now_w, now_m)
            self._link_down_since = None
        elif not isinstance(error, AuthError):
            if self._link_down_since is None:
                self._link_down_since = now_m
            if not self._link_alerted and now_m - self._link_down_since >= 60.0:
                self._link_alerted = True
                self._push(new, "link-down", "warning", "No connection to relay",
                           str(error)[:120], "PeekESP display", "", now_w, now_m)

        if reachable and snapshot.stale_s == 0:
            self._messages(snapshot, new, now_w, now_m)
            if s.offline_alerts:
                self._presence(snapshot, new, now_w, now_m)

        self._prune(now_m)
        return new

    def _messages(self, snapshot, new, now_w, now_m):
        for machine in snapshot.machines:
            for msg in machine.messages:
                key = (machine.host, msg.id)
                if key in self._seen:
                    continue
                self._seen[key] = now_m
                if msg.ts is not None and now_w - msg.ts > self.settings.max_age_s:
                    continue                    # old news: seen, never shown
                self._push(new, "msg:%s:%s" % key, msg.severity, msg.title, msg.body,
                           msg.source, machine.host, now_w, now_m)

    def _presence(self, snapshot, new, now_w, now_m):
        for machine in snapshot.machines:
            online = snapshot.age_of(machine) < self._offline_after
            was = self._online.get(machine.host)
            self._online[machine.host] = online
            if was is None or was == online:
                continue                        # first sight, or no change
            if not online:
                self._offline_since[machine.host] = now_w
                self._push(new, "off:%s:%d" % (machine.host, now_w),
                           self.settings.offline_severity, "%s went offline" % machine.host,
                           "No reading for %d min" % max(1, int(snapshot.age_of(machine) // 60)),
                           "PeekESP display", machine.host, now_w, now_m)
            else:
                self._offline_since.pop(machine.host, None)
                self._push(new, "on:%s:%d" % (machine.host, now_w), "info",
                           "%s is back online" % machine.host, "",
                           "PeekESP display", machine.host, now_w, now_m)

    def _push(self, new, key, severity, title, body, source, host, now_w, now_m):
        """Add an alert unless quiet hours say it must not be seen."""
        s = self.settings
        if not s.quiet.allows(severity, datetime.datetime.fromtimestamp(now_w)):
            return
        ttl = {"critical": s.critical_seconds, "warning": s.warning_seconds}.get(
            severity, s.info_seconds)
        alert = Alert(key=key, severity=severity, title=title, body=body,
                      source=source, host=host, born=now_m, expires=now_m + ttl)
        self._alerts.append(alert)
        new.append(alert)

    def _prune(self, now_m):
        self._alerts = [a for a in self._alerts if not a.acked and a.expires > now_m]
        for k in [k for k, t in self._seen.items() if now_m - t > self.SEEN_FOR]:
            del self._seen[k]

    # -- reading ---------------------------------------------------------------
    def active(self):
        """What may be shown right now, most severe first, then newest.

        Quiet hours are re-checked on every call and not only at creation, so a
        critical alert that began before the window does not keep a bedroom lit
        once it can no longer be allowed.
        """
        now_m = self._mono()
        now = datetime.datetime.fromtimestamp(self._clock())
        live = [a for a in self._alerts
                if not a.acked and a.expires > now_m
                and self.settings.quiet.allows(a.severity, now)]
        live.sort(key=lambda a: (-severity_rank(a.severity), -a.born))
        return live

    def current(self):
        """(the alert to draw, how many are active) or None."""
        live = self.active()
        return (live[0], len(live)) if live else None

    def critical_active(self):
        return any(a.severity == "critical" for a in self.active())

    def quiet_now(self):
        return self.settings.quiet.active(datetime.datetime.fromtimestamp(self._clock()))

    def acknowledge(self):
        """Dismiss everything on screen. What the button does."""
        for a in self._alerts:
            a.acked = True


# ---------------------------------------------------------------------------
#  Making a noise
# ---------------------------------------------------------------------------


class Alarm:
    """No buzzer. Every method is safe to call and does nothing."""

    ringing = False

    def ring(self):
        """Begin the repeating critical pattern. Idempotent."""

    def chirp(self):
        """Two short beeps, once."""

    def stop(self):
        """Be quiet."""

    def close(self):
        self.stop()


def _default_output(pin, active_low):
    try:
        from gpiozero import OutputDevice
    except ImportError as err:
        raise AlertHardwareError(
            "a buzzer needs gpiozero:\n    sudo apt install python3-gpiozero\n(%s)" % err
        ) from err
    try:
        return OutputDevice(int(pin), active_high=not active_low, initial_value=False)
    except Exception as err:        # noqa: BLE001 - gpiozero raises several kinds
        raise AlertHardwareError("cannot open GPIO %s for the buzzer: %s" % (pin, err)) from err


class GpioAlarm(Alarm):
    """An ACTIVE buzzer on one GPIO pin: one that makes its own tone when powered.

    A passive buzzer is a speaker and needs a waveform, not a level; wired to
    this it clicks once and goes quiet. Neither kind should be driven directly
    from a pin if it draws more than a few milliamps - use a transistor.

    The pattern runs on its own thread so a slow redraw (an e-paper refresh is
    seconds) cannot stretch a beep, and a stalled panel cannot leave it on.
    """

    BEEP = 0.22

    def __init__(self, pin, active_low=False, factory=None):
        try:
            self._out = (factory or _default_output)(pin, active_low)
        except AlertHardwareError:
            raise
        except Exception as err:    # noqa: BLE001 - whatever the pin library raised
            raise AlertHardwareError("cannot open GPIO %s for the buzzer: %s" % (pin, err)) from err
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._mode = None

    @property
    def ringing(self):
        return self._mode == "critical" and self._thread is not None and self._thread.is_alive()

    def _start(self, mode, target):
        self._stop.set()                        # a fresh event per start, so a
        stop = self._stop = threading.Event()   # restart never races its predecessor
        self._mode = mode
        self._thread = threading.Thread(target=target, args=(stop,), daemon=True)
        self._thread.start()

    def ring(self):
        with self._lock:
            if self.ringing:
                return
            self._start("critical", self._critical)

    def chirp(self):
        with self._lock:
            if self.ringing:
                return                          # already louder than a chirp
            self._start("chirp", self._chirp)

    def stop(self):
        with self._lock:
            self._stop.set()
            self._mode = None
        try:
            self._out.off()
        except Exception:               # noqa: BLE001 - never mask the caller's error
            pass

    def close(self):
        self.stop()
        try:
            self._out.close()
        except Exception:               # noqa: BLE001
            pass

    def _beeps(self, stop, count):
        for _ in range(count):
            if stop.is_set():
                return
            self._out.on()
            stop.wait(self.BEEP)
            self._out.off()
            stop.wait(self.BEEP)

    def _chirp(self, stop):
        try:
            self._beeps(stop, 2)
        finally:
            self._out.off()

    def _critical(self, stop):
        try:
            while not stop.is_set():
                self._beeps(stop, 4)
                stop.wait(1.0)
        finally:
            self._out.off()             # whatever ended this, never leave it on


class AckButton:
    """A push button between a GPIO pin and ground. Press = acknowledge."""

    def __init__(self, pin, callback, factory=None):
        if factory is None:
            try:
                from gpiozero import Button
            except ImportError as err:
                raise AlertHardwareError(
                    "a button needs gpiozero:\n    sudo apt install python3-gpiozero\n(%s)" % err
                ) from err
            factory = lambda p: Button(int(p), pull_up=True, bounce_time=0.05)   # noqa: E731
        try:
            self._button = factory(pin)
        except Exception as err:        # noqa: BLE001
            raise AlertHardwareError("cannot open GPIO %s for the button: %s" % (pin, err)) from err
        self._button.when_pressed = callback

    def close(self):
        try:
            self._button.close()
        except Exception:               # noqa: BLE001
            pass


def make_alarm(pin, active_low=False, factory=None):
    """A GpioAlarm if there is a pin, else the silent one."""
    if pin in (None, ""):
        return Alarm()
    return GpioAlarm(pin, active_low, factory)
