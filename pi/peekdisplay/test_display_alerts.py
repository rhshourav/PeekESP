"""
test_display_alerts.py - extra sensors, messages, alerts and the buzzer.

    python3 -m unittest discover -s tests -v

No display library and no GPIO are needed: the buzzer is driven through a fake
pin, panels are fakes or the png panel, and time is injected. What this cannot
cover is the pin itself - whether a buzzer is wired to GPIO 18 and is the active
kind. `peek-display --test-alert critical` is for that.
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import unittest.mock
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from peekdisplay import alerts, app, config, layout, panels   # noqa: E402
from peekdisplay.relay import (                                          # noqa: E402
    AuthError, Extra, Machine, RelayError, Snapshot, norm_severity,
    parse_extras, parse_messages, parse_payload)


# ---------------------------------------------------------------- helpers
class Clock:
    """Wall and monotonic time that only moves when told to."""

    def __init__(self, when=None):
        self.w = (when or datetime(2026, 10, 21, 12, 0)).timestamp()   # a Wednesday, noon
        self.m = 1000.0

    def wall(self):
        return self.w

    def mono(self):
        return self.m

    def advance(self, s):
        self.w += s
        self.m += s


def local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi).timestamp()


def machine(host="studio", age=3, messages=None, **kw):
    m = Machine(host=host, age_s=age, cpu_percent=10.0, ram_percent=20.0)
    m.messages = parse_messages({"messages": messages or []})
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def snap(*machines, stale=0):
    return Snapshot(machines=list(machines), stale_s=stale)


def center(clock=None, **overrides):
    clock = clock or Clock()
    settings = alerts.Settings(**overrides)
    return alerts.AlertCenter(settings, offline_after_s=150,
                              clock=clock.wall, mono=clock.mono), clock


class FakePin:
    """Records what the buzzer did. `levels` is the sequence of on/off."""

    def __init__(self):
        self.levels, self.closed = [], False
        self.lock = threading.Lock()

    def on(self):
        with self.lock:
            self.levels.append(1)

    def off(self):
        with self.lock:
            self.levels.append(0)

    def close(self):
        self.closed = True

    @property
    def is_on(self):
        return bool(self.levels and self.levels[-1])

    def count_on(self):
        return self.levels.count(1)


def alarm_with_pin(beep=0.01):
    pin = FakePin()
    a = alerts.GpioAlarm(18, factory=lambda p, low: pin)
    a.BEEP = beep
    return a, pin


def wait_for(pred, secs=3.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


# ------------------------------------------------------------ extra sensors
class TestExtras(unittest.TestCase):
    def test_plain_keys_and_metrics(self):
        raw = {"host": "s", "gpu_percent": 42.5, "gpu_temp_c": 61, "npu_percent": 7,
               "fan_rpm": 1200,
               "metrics": {"vram_gb": 8.5,
                           "solar_w": {"value": 812, "label": "Solar", "unit": "W", "max": 5000}}}
        got = {e.key: e for e in parse_extras(raw)}
        self.assertEqual(set(got), {"gpu_percent", "gpu_temp_c", "npu_percent", "fan_rpm",
                                    "vram_gb", "solar_w"})
        self.assertEqual((got["gpu_percent"].unit, got["gpu_percent"].short), ("%", "GPU"))
        self.assertEqual(got["gpu_temp_c"].text(), "61\u00b0C")
        self.assertEqual(got["fan_rpm"].text(), "1200rpm")
        self.assertEqual((got["solar_w"].label, got["solar_w"].maximum), ("Solar", 5000.0))
        self.assertEqual(got["vram_gb"].text(), "8.5GB")

    def test_the_protocol_rules(self):
        raw = {"gpu_percent": -1,                    # no such sensor: skipped, never 0
               "cpu_percent": 5, "cpu_temp_c": 50,   # standard names are not extras
               "age_s": 3, "latest_fw": "1.0",       # relay bookkeeping: no recognised ending
               "Bad_Key_c": 1, "x" * 40 + "_c": 1,   # not a-z0-9_ / too long
               "flag_w": True,                       # a bool is not 1.0
               "ok_w": 5,
               "metrics": {"cpu_percent": 9, "shadow_w": {"value": -2}, "good_w": {"value": 3}}}
        self.assertEqual([e.key for e in parse_extras(raw)], ["ok_w", "good_w"])

    def test_at_most_24(self):
        raw = {"s%02d_w" % i: float(i) for i in range(40)}
        self.assertEqual(len(parse_extras(raw)), 24)

    def test_a_reading_without_them_has_none(self):
        m = Machine.parse({"host": "s", "cpu_percent": 5})
        self.assertEqual((m.extras, m.messages), ([], []))

    def test_extras_ride_through_a_whole_payload(self):
        snap_ = parse_payload({"devices": [
            {"host": "a", "gpu_percent": 10, "messages": [{"title": "t", "severity": "red"}]},
            {"host": "b"}]})
        a, b = snap_.machines
        self.assertEqual(a.extras[0].key, "gpu_percent")
        self.assertEqual(a.messages[0].severity, "critical")
        self.assertEqual(b.extras, [])

    def test_short_labels_fit_sixteen_cells(self):
        self.assertEqual(Extra("custom_thing_w", 1, "Custom Thing", "W").short, "CUSTO")
        self.assertEqual(Extra("a_w", 1, "", "W").short, "A_W"[:5].replace("_", "") or "AW")


class TestMessages(unittest.TestCase):
    def test_severity_words(self):
        for w in ("critical", "RED", "high", "alert", "emergency", "fatal", "error"):
            self.assertEqual(norm_severity(w), "critical", w)
        for w in ("warning", "yellow", "amber", "orange", "warn", "medium"):
            self.assertEqual(norm_severity(w), "warning", w)
        for w in ("info", "", None, "blue", "reed"):
            self.assertEqual(norm_severity(w), "info", w)

    def test_fields_limits_and_defaults(self):
        msgs = parse_messages({"messages": [
            {"id": "a", "severity": "red", "title": "x" * 200, "message": "y" * 900,
             "source": "z" * 90, "ts": 1760000000000},
            {"body": "only a body"},
            {"severity": "red"},                     # says nothing: dropped
            "junk", 5]})
        self.assertEqual(len(msgs), 2)
        a, b = msgs
        self.assertEqual((len(a.title), len(a.body), len(a.source)), (80, 400, 40))
        self.assertEqual(a.ts, 1760000000.0)         # milliseconds
        self.assertEqual((b.title, b.source, b.ts), ("only a body", "machine", None))

    def test_an_id_less_message_has_a_stable_id(self):
        one = parse_messages({"messages": [{"title": "same", "severity": "red"}]})[0].id
        two = parse_messages({"messages": [{"title": "same", "severity": "red"}]})[0].id
        self.assertEqual(one, two)
        self.assertNotEqual(one, parse_messages({"messages": [{"title": "other"}]})[0].id)

    def test_control_characters_are_removed(self):
        m = parse_messages({"messages": [{"title": "a\x00b\nc\x1b[31m"}]})[0]
        self.assertNotIn("\x00", m.title)
        self.assertNotIn("\n", m.title)
        self.assertNotIn("\x1b", m.title)

    def test_at_most_ten(self):
        self.assertEqual(len(parse_messages({"messages": [{"title": "t%d" % i}
                                                          for i in range(30)]})), 10)


# -------------------------------------------------------------- quiet hours
class TestQuiet(unittest.TestCase):
    def d(self, wd, h, m=0):
        return datetime(2026, 10, 19 + wd, h, m)           # 2026-10-19 is a Monday

    def test_parse(self):
        q = alerts.parse_quiet("22:00-07:00")
        self.assertEqual((q.enabled, q.start, q.end), (True, 22 * 60, 7 * 60))
        self.assertFalse(alerts.parse_quiet("").enabled)
        with self.assertLogs("peekdisplay", "WARNING"):
            self.assertFalse(alerts.parse_quiet("night").enabled)
        with self.assertLogs("peekdisplay", "WARNING"):
            self.assertFalse(alerts.parse_quiet("25:00-07:00").enabled)
        self.assertEqual(alerts.parse_quiet("22:00-07:00", "weekdays").days, (0, 1, 2, 3, 4))
        self.assertEqual(alerts.parse_quiet("22:00-07:00", "fri,sat").days, (4, 5))

    def test_window_crosses_midnight(self):
        q = alerts.parse_quiet("22:00-07:00")
        self.assertTrue(q.active(self.d(0, 23)))
        self.assertTrue(q.active(self.d(1, 3)))
        self.assertFalse(q.active(self.d(1, 7)))
        self.assertFalse(q.active(self.d(1, 12)))

    def test_days_are_the_days_the_window_begins(self):
        q = alerts.parse_quiet("22:00-07:00", "fri")
        self.assertTrue(q.active(self.d(4, 23)))
        self.assertTrue(q.active(self.d(5, 3)))            # Saturday 03:00 belongs to Friday's
        self.assertFalse(q.active(self.d(4, 3)))
        self.assertFalse(q.active(self.d(5, 23)))

    def test_allows(self):
        q = alerts.parse_quiet("22:00-07:00")
        night, noon = self.d(0, 23), self.d(0, 12)
        self.assertTrue(q.allows("info", noon))
        self.assertFalse(q.allows("warning", night))
        self.assertTrue(q.allows("critical", night))
        self.assertFalse(alerts.parse_quiet("22:00-07:00", allow_critical=False)
                         .allows("critical", night))


# --------------------------------------------------------------- the centre
class TestAlertCenter(unittest.TestCase):
    def test_a_message_shows_once_however_often_it_is_resent(self):
        c, clk = center()
        m = machine(messages=[{"id": "fw-1", "severity": "red", "title": "Intrusion",
                               "ts": clk.w}])
        self.assertEqual(len(c.update(snap(m))), 1)
        for _ in range(5):
            clk.advance(5)
            self.assertEqual(c.update(snap(m)), [])                 # re-sent every reading
        self.assertEqual(len(c.active()), 1)

    def test_old_messages_are_filed_quietly_at_first_sight(self):
        c, clk = center()
        old = machine(messages=[{"id": "o", "severity": "red", "title": "Last night",
                                 "ts": clk.w - 7200}])
        self.assertEqual(c.update(snap(old)), [])
        self.assertEqual(c.active(), [])
        undated = machine(messages=[{"id": "u", "severity": "red", "title": "No ts"}])
        self.assertEqual(len(c.update(snap(undated))), 1)          # unknown age: show it

    def test_a_restart_does_not_ring_for_a_message_from_before_it(self):
        clk = Clock()
        msg = [{"id": "a", "severity": "red", "title": "x", "ts": clk.w - 3600}]
        fresh, _ = center(clk)                                      # the display just booted
        self.assertEqual(fresh.update(snap(machine(messages=msg))), [])

    def test_the_same_id_from_two_machines_is_two_alerts(self):
        c, clk = center()
        a = machine("a", messages=[{"id": "1", "title": "t", "severity": "red", "ts": clk.w}])
        b = machine("b", messages=[{"id": "1", "title": "t", "severity": "red", "ts": clk.w}])
        self.assertEqual(len(c.update(snap(a, b))), 2)

    def test_offline_and_back(self):
        c, clk = center()
        self.assertEqual(c.update(snap(machine("a", age=3))), [])    # first sight: nothing
        new = c.update(snap(machine("a", age=200)))
        self.assertEqual([(x.severity, x.title) for x in new],
                         [("warning", "a went offline")])
        self.assertEqual(c.update(snap(machine("a", age=210))), [])  # still offline: not again
        back = c.update(snap(machine("a", age=2)))
        self.assertEqual([(x.severity, x.title) for x in back], [("info", "a is back online")])

    def test_a_machine_that_was_already_offline_at_boot_is_not_news(self):
        c, _ = center()
        self.assertEqual(c.update(snap(machine("dead", age=9999))), [])

    def test_the_pi_losing_its_network_is_not_every_machine_dying(self):
        c, clk = center()
        c.update(snap(machine("a", age=3), machine("b", age=3)))
        # A failed poll keeps the old data and adds to its age (Snapshot.stale_s).
        raised = []
        for _ in range(3):
            clk.advance(70)
            raised += c.update(snap(machine("a", age=3), machine("b", age=3), stale=200),
                               RelayError("cannot reach relay"))
        self.assertEqual([x.title for x in raised], ["No connection to relay"])
        self.assertFalse(any("offline" in x.title for x in raised))

    def test_relay_down_for_a_minute_then_recovered(self):
        c, clk = center()
        c.update(snap(machine("a")))
        err = RelayError("timed out")
        self.assertEqual(c.update(snap(machine("a"), stale=10), err), [])      # a blip
        clk.advance(30)
        self.assertEqual(c.update(snap(machine("a"), stale=40), err), [])
        clk.advance(40)
        down = c.update(snap(machine("a"), stale=80), err)
        self.assertEqual([x.title for x in down], ["No connection to relay"])
        self.assertEqual(c.update(snap(machine("a"), stale=90), err), [])      # once
        up = c.update(snap(machine("a")))
        self.assertEqual([x.title for x in up], ["Relay reachable again"])

    def test_a_rejected_code_is_not_the_link_being_down(self):
        c, clk = center()
        for _ in range(4):
            clk.advance(30)
            c.update(snap(), AuthError("rejected"))
        self.assertEqual(c.active(), [])

    def test_offline_alerts_can_be_switched_off(self):
        c, _ = center(offline_alerts=False)
        c.update(snap(machine("a", age=3)))
        self.assertEqual(c.update(snap(machine("a", age=500))), [])

    def test_offline_severity_is_configurable(self):
        c, _ = center(offline_severity="critical")
        c.update(snap(machine("a", age=3)))
        self.assertEqual(c.update(snap(machine("a", age=500)))[0].severity, "critical")

    def test_durations_by_severity_and_ordering(self):
        c, clk = center(info_seconds=10, warning_seconds=30, critical_seconds=600)
        msgs = [{"id": "i", "title": "info", "severity": "info", "ts": clk.w},
                {"id": "w", "title": "warn", "severity": "warning", "ts": clk.w},
                {"id": "c", "title": "crit", "severity": "critical", "ts": clk.w}]
        c.update(snap(machine(messages=msgs)))
        self.assertEqual([a.title for a in c.active()], ["crit", "warn", "info"])
        self.assertEqual(c.current()[1], 3)
        clk.advance(11)
        self.assertEqual([a.title for a in c.active()], ["crit", "warn"])
        clk.advance(20)
        self.assertEqual([a.title for a in c.active()], ["crit"])
        clk.advance(580)
        self.assertEqual(c.active(), [])                             # ten minutes, like the phone

    def test_acknowledge_dismisses_everything(self):
        c, clk = center()
        c.update(snap(machine(messages=[
            {"id": "c", "title": "x", "severity": "red", "ts": clk.w},
            {"id": "w", "title": "y", "severity": "warn", "ts": clk.w}])))
        self.assertTrue(c.critical_active())
        c.acknowledge()
        self.assertEqual(c.active(), [])
        self.assertFalse(c.critical_active())

    def test_disabled_does_nothing(self):
        c, clk = center(enabled=False)
        m = machine(messages=[{"id": "c", "title": "x", "severity": "red", "ts": clk.w}])
        self.assertEqual(c.update(snap(m)), [])

    def test_quiet_hours_hide_everything_but_critical(self):
        night = Clock(datetime(2026, 10, 21, 23, 30))
        c, clk = center(night, quiet=alerts.parse_quiet("22:00-07:00"))
        c.update(snap(machine("a", age=3)))
        out = c.update(snap(machine("a", age=500, messages=[
            {"id": "c", "title": "crit", "severity": "red", "ts": clk.w},
            {"id": "w", "title": "warn", "severity": "warn", "ts": clk.w}])))
        self.assertEqual([a.title for a in out], ["crit"])           # the offline notice and the warning: hidden

    def test_quiet_does_not_queue_what_it_hides(self):
        clk = Clock(datetime(2026, 10, 21, 23, 30))
        c, _ = center(clk, quiet=alerts.parse_quiet("22:00-07:00"))
        c.update(snap(machine(messages=[{"id": "w", "title": "warn", "severity": "warn",
                                         "ts": clk.w}])))
        clk.advance(9 * 3600)                                         # morning
        c.update(snap(machine(messages=[{"id": "w", "title": "warn", "severity": "warn",
                                         "ts": clk.w - 9 * 3600}])))
        self.assertEqual(c.active(), [])                              # not shown at 07:30 for 23:30

    def test_a_critical_alert_that_outlasts_the_quiet_allowance_stops_showing(self):
        clk = Clock(datetime(2026, 10, 21, 21, 55))
        c, _ = center(clk, quiet=alerts.parse_quiet("22:00-07:00", allow_critical=False))
        c.update(snap(machine(messages=[{"id": "c", "title": "x", "severity": "red",
                                         "ts": clk.w}])))
        self.assertTrue(c.critical_active())
        clk.advance(600)                                              # quiet hours began
        self.assertFalse(c.critical_active())

    def test_seen_ids_are_forgotten_after_a_day(self):
        c, clk = center()
        m = machine(messages=[{"id": "a", "title": "x", "severity": "info", "ts": clk.w}])
        c.update(snap(m))
        clk.advance(25 * 3600)
        c.update(snap(machine("other")))
        self.assertEqual(len(c._seen), 0)


# --------------------------------------------------------------- the buzzer
class TestAlarm(unittest.TestCase):
    def test_critical_repeats_until_stopped_and_ends_off(self):
        a, pin = alarm_with_pin()
        a.ring()
        self.assertTrue(wait_for(lambda: pin.count_on() >= 9))       # more than one burst of four
        self.assertTrue(a.ringing)
        a.stop()
        self.assertTrue(wait_for(lambda: not a.ringing))
        time.sleep(0.05)
        n = pin.count_on()
        time.sleep(0.15)
        self.assertEqual(pin.count_on(), n)                           # it really stopped
        self.assertFalse(pin.is_on)                                   # and is not left driven

    def test_ring_is_idempotent(self):
        a, pin = alarm_with_pin(0.02)
        a.ring()
        t = a._thread
        for _ in range(5):
            a.ring()
        self.assertIs(a._thread, t)                                   # not a thread per call
        a.stop()

    def test_chirp_is_two_beeps(self):
        a, pin = alarm_with_pin()
        a.chirp()
        self.assertTrue(wait_for(lambda: pin.count_on() == 2 and not pin.is_on))
        time.sleep(0.1)
        self.assertEqual(pin.count_on(), 2)

    def test_a_chirp_does_not_interrupt_a_ring(self):
        a, pin = alarm_with_pin(0.02)
        a.ring()
        t = a._thread
        a.chirp()
        self.assertIs(a._thread, t)
        a.stop()

    def test_stop_mid_beep_leaves_it_off(self):
        a, pin = alarm_with_pin(0.5)                                  # long beeps
        a.ring()
        self.assertTrue(wait_for(lambda: pin.is_on))
        a.stop()
        self.assertFalse(pin.is_on)

    def test_restart_after_stop(self):
        a, pin = alarm_with_pin()
        a.ring()
        a.stop()
        wait_for(lambda: not a.ringing)
        a.ring()
        self.assertTrue(wait_for(lambda: a.ringing))
        a.stop()

    def test_close_releases_the_pin(self):
        a, pin = alarm_with_pin()
        a.ring()
        a.close()
        self.assertTrue(pin.closed)
        self.assertFalse(pin.is_on)

    def test_no_pin_means_a_silent_alarm(self):
        a = alerts.make_alarm("")
        a.ring()
        a.chirp()
        a.stop()
        self.assertFalse(a.ringing)

    def test_missing_gpiozero_says_what_to_install(self):
        with self.assertRaises(alerts.AlertHardwareError) as cm:
            alerts.GpioAlarm(18)             # gpiozero is not installed in this environment
        self.assertIn("python3-gpiozero", str(cm.exception))
        with self.assertRaises(alerts.AlertHardwareError) as cm:
            alerts.AckButton(17, lambda: None)
        self.assertIn("python3-gpiozero", str(cm.exception))

    def test_a_pin_that_cannot_be_opened_is_reported_not_raised_raw(self):
        def boom(pin, low):
            raise OSError("pin busy")
        with self.assertRaises(alerts.AlertHardwareError):
            alerts.GpioAlarm(18, factory=boom)

    def test_button_press_acknowledges(self):
        class Btn:
            when_pressed = None
        btn = Btn()
        pressed = []
        alerts.AckButton(17, lambda: pressed.append(1), factory=lambda p: btn)
        btn.when_pressed()
        self.assertEqual(pressed, [1])


# ------------------------------------------------------------ the display
class FakePanel(panels.Panel):
    def __init__(self, kind=panels.KIND_PIXEL, size=(240, 240), min_interval=0.0,
                 can_dim=True, mono=False, round_=False):
        self.kind, self.size, self.min_interval = kind, size, min_interval
        self.mono, self.round, self.name = mono, round_, "fake"
        self.shown, self.levels, self._can_dim = [], [], can_dim

    def display(self, payload):
        self.shown.append(payload)

    def set_level(self, level):
        self.levels.append(level)
        return self._can_dim


class FakeRelay:
    def __init__(self, *polls):
        self.polls, self.last = list(polls), Snapshot()

    def poll(self):
        item = self.polls.pop(0) if self.polls else KeyboardInterrupt()
        if isinstance(item, BaseException):
            raise item
        s, e = item
        self.last = s
        return s, e


class SpyAlarm(alerts.Alarm):
    def __init__(self):
        self.calls, self._ringing = [], False

    @property
    def ringing(self):
        return self._ringing

    def ring(self):
        self.calls.append("ring")
        self._ringing = True

    def chirp(self):
        self.calls.append("chirp")

    def stop(self):
        self.calls.append("stop")
        self._ringing = False


def display(panel=None, relay=None, **kw):
    clk = kw.pop("clock", None) or Clock()
    c = alerts.AlertCenter(kw.pop("settings", alerts.Settings()), offline_after_s=150,
                           clock=clk.wall, mono=clk.mono)
    alarm = kw.pop("alarm", SpyAlarm())
    d = app.Display(panel or FakePanel(), relay, alerts=c, alarm=alarm, **kw)
    return d, c, alarm, clk


class TestDisplay(unittest.TestCase):
    def crit(self, clk, **kw):
        return machine(messages=[{"id": "c", "severity": "red", "title": "Intrusion",
                                  "body": "New login", "ts": clk.w}], **kw)

    def test_a_critical_alert_rings_until_acknowledged(self):
        d, c, alarm, clk = display()
        d._new_alerts = d._to_chirp = c.update(snap(self.crit(clk)))
        d.drive_alarm()
        self.assertEqual(alarm.calls, ["ring"])
        d.drive_alarm()
        self.assertEqual(alarm.calls, ["ring", "ring"])             # idempotent by contract
        c.acknowledge()
        d.drive_alarm()
        self.assertEqual(alarm.calls[-1], "stop")

    def test_a_warning_chirps_once_not_every_beat(self):
        d, c, alarm, clk = display()
        new = c.update(snap(machine(messages=[{"id": "w", "severity": "warn", "title": "t",
                                               "ts": clk.w}])))
        d._new_alerts = d._to_chirp = new
        d.drive_alarm()
        d.drive_alarm()
        d.drive_alarm()
        self.assertEqual(alarm.calls.count("chirp"), 1)

    def test_info_makes_no_sound(self):
        d, c, alarm, clk = display()
        d._to_chirp = c.update(snap(machine(messages=[{"id": "i", "title": "t", "ts": clk.w}])))
        d.drive_alarm()
        self.assertEqual(alarm.calls, [])

    def test_quiet_hours_silence_the_buzzer_for_a_warning(self):
        clk = Clock(datetime(2026, 10, 21, 23, 30))
        d, c, alarm, _ = display(clock=clk, settings=alerts.Settings(
            quiet=alerts.parse_quiet("22:00-07:00")))
        d._to_chirp = c.update(snap(machine(messages=[
            {"id": "w", "severity": "warn", "title": "t", "ts": clk.w}])))
        d.drive_alarm()
        self.assertEqual(alarm.calls, [])

    def test_a_dashboard_is_drawn_when_nothing_is_wrong(self):
        panel = FakePanel()
        d, c, alarm, clk = display(panel)
        d.draw(snap(machine()))
        self.assertEqual(panel.shown[-1].size, (240, 240))

    def test_an_alert_replaces_the_dashboard_on_a_pixel_panel(self):
        panel = FakePanel()
        d, c, alarm, clk = display(panel)
        plain = display(FakePanel())[0]
        s = snap(self.crit(clk))
        plain.draw(snap(machine()))
        c.update(s)
        d.draw(s)
        self.assertNotEqual(panel.shown[-1].tobytes(), plain.panel.shown[-1].tobytes())

    def test_critical_blinks_only_where_redrawing_is_free(self):
        clk = Clock()
        free = FakePanel()
        d, c, _, _ = display(free, clock=clk)
        s = snap(self.crit(clk))
        c.update(s)
        with unittest.mock.patch.object(app.time, "monotonic", return_value=100.0):
            d.draw(s)
        with unittest.mock.patch.object(app.time, "monotonic", return_value=101.0):
            d.draw(s)
        self.assertNotEqual(free.shown[0].tobytes(), free.shown[1].tobytes())   # it blinked
        epaper = FakePanel(min_interval=180, mono=True)
        d2, c2, _, _ = display(epaper, clock=clk)
        c2.update(s)
        with unittest.mock.patch.object(app.time, "monotonic", return_value=100.0):
            d2.draw(s)
        with unittest.mock.patch.object(app.time, "monotonic", return_value=101.0):
            d2.draw(s)
        self.assertEqual(epaper.shown[0].tobytes(), epaper.shown[1].tobytes())  # it did not

    def test_a_character_panel_shows_the_alert_then_the_dashboard(self):
        panel = FakePanel(panels.KIND_TEXT, (16, 2))
        d, c, alarm, clk = display(panel)
        s = snap(self.crit(clk))
        d.draw(s)
        self.assertTrue(panel.shown[-1][0].startswith("CPU") or "studio" in panel.shown[-1][0])
        c.update(s)
        d.draw(s)
        self.assertEqual(len(panel.shown[-1]), 2)
        self.assertTrue(all(len(line) == 16 for line in panel.shown[-1]))
        self.assertTrue(panel.shown[-1][0].startswith("!!!"))

    def test_a_matrix_scrolls_the_alert_first(self):
        panel = FakePanel(panels.KIND_MARQUEE, (32, 8))
        d, c, alarm, clk = display(panel)
        s = snap(self.crit(clk))
        c.update(s)
        d.draw(s)
        self.assertTrue(panel.shown[-1].startswith("!!! CRITICAL studio: Intrusion"))
        self.assertIn("CPU 10%", panel.shown[-1])

    def test_extras_join_the_character_rotation(self):
        panel = FakePanel(panels.KIND_TEXT, (16, 2))
        d, c, alarm, clk = display(panel)
        m = machine(extras=[Extra("gpu_percent", 42.0, "GPU", "%")])
        seen = []
        for step in range(8):
            d.step = step
            d.draw(snap(m))
            seen.append(panel.shown[-1][1].strip())
        self.assertIn("GPU 42%", seen)

    def test_extras_can_be_switched_off(self):
        panel = FakePanel(panels.KIND_TEXT, (16, 2))
        d, c, alarm, clk = display(panel, extras=False)
        m = machine(extras=[Extra("gpu_percent", 42.0, "GPU", "%")])
        for step in range(12):
            d.step = step
            d.draw(snap(m))
            self.assertNotIn("GPU", panel.shown[-1][1])

    def test_the_footer_alternates_between_usual_and_extras(self):
        d, c, alarm, clk = display(FakePanel())
        m = machine(extras=[Extra("gpu_percent", 42.0, "GPU", "%")], storage_percent=50.0,
                    storage_free_gb=10.0, cpu_temp_c=40.0)
        frames = []
        for step in range(4):
            d.step = step
            d.draw(snap(m))
            frames.append(d.panel.shown[-1].tobytes())
        self.assertEqual(frames[0], frames[2] if False else frames[0])
        self.assertNotEqual(frames[0], frames[1])      # usual footer, then the extras page
        self.assertEqual(frames[0], frames[2])         # and back

    def test_with_two_machines_each_one_takes_its_turn_at_extras(self):
        d, c, alarm, clk = display(FakePanel())
        a = machine("a", extras=[Extra("gpu_percent", 1.0, "GPU", "%")], cpu_temp_c=40.0)
        b = machine("b", extras=[Extra("gpu_percent", 2.0, "GPU", "%")], cpu_temp_c=40.0)
        s = snap(a, b)
        shown = {0: set(), 1: set()}
        for step in range(8):
            d.step, d.index = step, step % 2           # rotate_seconds moves both together
            d.draw(s)
            shown[d.index].add(d.panel.shown[-1].tobytes())
        self.assertGreater(len(shown[0]), 1)           # machine A is not stuck on one footer
        self.assertGreater(len(shown[1]), 1)           # and neither is B

    def test_a_display_built_the_old_way_behaves_the_old_way(self):
        panel = FakePanel(panels.KIND_TEXT, (16, 2))
        d = app.Display(panel, FakeRelay())
        m = machine(extras=[Extra("gpu_percent", 42.0, "GPU", "%")])
        d.draw(snap(m))
        self.assertEqual(len(panel.shown[-1]), 2)
        d.drive_alarm()
        d.apply_screen_level()                          # no-ops without an alert centre
        self.assertEqual(panel.levels, [])


class TestQuietScreen(unittest.TestCase):
    def night(self, **kw):
        clk = Clock(datetime(2026, 10, 21, 23, 30))
        return display(clock=clk, quiet_screen=kw.pop("screen"), settings=alerts.Settings(
            quiet=alerts.parse_quiet("22:00-07:00")), **kw)

    def test_none_never_touches_the_panel(self):
        d, c, a, clk = self.night(screen="none")
        d.apply_screen_level()
        self.assertEqual(d.panel.levels, [])

    def test_off_blanks_at_night_and_restores_by_day(self):
        d, c, a, clk = self.night(screen="off")
        d.apply_screen_level()
        d.apply_screen_level()
        self.assertEqual(d.panel.levels, [0.0])                      # once, not every beat
        clk.advance(9 * 3600)
        d.apply_screen_level()
        self.assertEqual(d.panel.levels, [0.0, 1.0])

    def test_dim_goes_low_not_dark(self):
        d, c, a, clk = self.night(screen="dim")
        d.apply_screen_level()
        self.assertEqual(d.panel.levels, [0.15])

    def test_a_critical_alert_wakes_the_screen(self):
        d, c, a, clk = self.night(screen="off")
        d.apply_screen_level()
        c.update(snap(machine(messages=[{"id": "c", "severity": "red", "title": "x",
                                         "ts": clk.w}])))
        d.apply_screen_level()
        self.assertEqual(d.panel.levels, [0.0, 1.0])

    def test_a_panel_that_cannot_dim_says_so_once(self):
        d, c, a, clk = self.night(screen="off", panel=FakePanel(can_dim=False))
        with self.assertLogs("peekdisplay", "WARNING") as logs:
            d.apply_screen_level()
            clk.advance(9 * 3600)
            d.apply_screen_level()
        self.assertEqual(len(logs.output), 1)


class TestPanelLevels(unittest.TestCase):
    def test_oled_uses_contrast(self):
        calls = []

        class Dev:
            def contrast(self, v):
                calls.append(v)
        p = panels.LumaPanel(Dev(), 128, 64, mono=True, level_api="contrast")
        self.assertTrue(p.set_level(0.0))
        self.assertTrue(p.set_level(1.0))
        self.assertEqual(calls, [0, 255])

    def test_tft_backlight_is_a_switch(self):
        calls = []

        class Dev:
            def backlight(self, on):
                calls.append(on)
        p = panels.LumaPanel(Dev(), 240, 240, level_api="backlight")
        p.set_level(0.15)
        p.set_level(1.0)
        self.assertEqual(calls, [False, True])             # 15% is off: it cannot dim

    def test_a_tft_with_no_backlight_pin_reports_it_cannot(self):
        p = panels.LumaPanel(object(), 240, 240, level_api="backlight")
        self.assertFalse(p.set_level(0.0))

    def test_character_lcd_backlight(self):
        lcd = type("L", (), {"backlight_enabled": True})()
        p = panels.CharacterPanel.__new__(panels.CharacterPanel)
        p._lcd = lcd
        self.assertTrue(p.set_level(0.0))
        self.assertFalse(lcd.backlight_enabled)

    def test_a_library_that_raises_something_odd_cannot_stop_the_display(self):
        class Dev:
            def contrast(self, v):
                raise ValueError("unexpected")
        self.assertFalse(panels.LumaPanel(Dev(), 128, 64, level_api="contrast").set_level(0.0))

    def test_png_and_epaper_cannot(self):
        self.assertFalse(panels.PngPanel().set_level(0.0))


class TestLoop(unittest.TestCase):
    def test_run_rings_draws_and_always_ends_quiet(self):
        clk = Clock()
        bad = machine(messages=[{"id": "c", "severity": "red", "title": "x", "ts": clk.w}])
        relay = FakeRelay((snap(bad), None), (snap(bad), None))     # then KeyboardInterrupt
        panel = FakePanel()
        d, c, alarm, _ = display(panel, relay, clock=clk)
        d.interval = 1.0
        d.run()
        self.assertEqual(alarm.calls[0], "ring")
        self.assertEqual(alarm.calls[-1], "stop")                   # the finally
        self.assertGreaterEqual(len(panel.shown), 1)

    def test_a_new_alert_redraws_a_rate_limited_panel_early_but_not_endlessly(self):
        clk = Clock()
        epaper = FakePanel(min_interval=180, mono=True)
        d, c, alarm, _ = display(epaper, FakeRelay(), clock=clk)
        s = snap(machine())
        d.draw(s)
        d._last_draw = time.monotonic()                              # just drew
        d._new_alerts = c.update(snap(machine(messages=[
            {"id": "c", "severity": "red", "title": "x", "ts": clk.w}])))
        self.assertTrue(d._urgent())                                 # wants out now...
        since = time.monotonic() - d._last_draw
        self.assertLess(since, app.URGENT_GAP_S)                     # ...but the gap has not passed
        self.assertTrue(d._urgent_pending)                           # and it is remembered
        c.acknowledge()                                               # cleared before it was drawn
        self.assertTrue(d._urgent())
        d.draw(s)
        self.assertFalse(d._urgent_pending)

    def test_sigterm_in_a_real_process_stops_the_buzzer(self):
        script = textwrap.dedent('''
            import sys, time
            sys.path.insert(0, %r)
            from peekdisplay import alerts, app, panels
            from peekdisplay.relay import Snapshot, Machine, parse_messages
            class Relay:
                last = Snapshot()
                def poll(self):
                    m = Machine(host="h", cpu_percent=1.0, ram_percent=1.0)
                    m.messages = parse_messages({"messages": [{"id": "c", "severity": "red", "title": "x"}]})
                    return Snapshot(machines=[m]), None
            class Alarm(alerts.Alarm):
                def ring(self): open(%r, "a").write("ring\\n")
                def stop(self): open(%r, "a").write("stop\\n")
            class Panel(panels.Panel):
                size = (240, 240)
                def display(self, image): pass
            d = app.Display(Panel(), Relay(), alerts=alerts.AlertCenter(), alarm=Alarm())
            print("up", flush=True)
            d.run()
        ''')
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "log")
            path = os.path.join(tmp, "s.py")
            Path(path).write_text(script % (str(ROOT), log, log))
            proc = subprocess.Popen([sys.executable, path], stdout=subprocess.PIPE)
            self.addCleanup(proc.stdout.close)
            self.assertEqual(proc.stdout.readline().strip(), b"up")
            self.assertTrue(wait_for(lambda: os.path.exists(log) and "ring" in Path(log).read_text()))
            proc.terminate()                                          # what systemd sends
            proc.wait(10)
            self.assertEqual(Path(log).read_text().split()[-1], "stop")


# ------------------------------------------------------------ config + CLI
class TestConfig(unittest.TestCase):
    def cfg(self, **kv):
        return config.Config(dict(kv))

    def test_defaults_alerts_on_nothing_loud(self):
        s = alerts.Settings.from_config(self.cfg())
        self.assertTrue(s.enabled and s.offline_alerts)
        self.assertEqual((s.info_seconds, s.warning_seconds, s.critical_seconds), (10, 30, 600))
        self.assertFalse(s.quiet.enabled)
        self.assertEqual(self.cfg().get("PEEK_BUZZER_PIN", ""), "")

    def test_flag_and_float(self):
        c = self.cfg(PEEK_ALERTS="0", PEEK_EXTRAS="off", PEEK_INFO_SECONDS="abc")
        self.assertFalse(c.flag("PEEK_ALERTS", True))
        self.assertFalse(c.flag("PEEK_EXTRAS", True))
        self.assertTrue(self.cfg().flag("PEEK_ALERTS", False))        # unset keeps the default
        self.assertEqual(c.float("PEEK_INFO_SECONDS", 10), 10)

    def test_quiet_from_config(self):
        s = alerts.Settings.from_config(self.cfg(
            PEEK_QUIET="22:00-07:00", PEEK_QUIET_DAYS="weekdays", PEEK_QUIET_CRITICAL="0",
            PEEK_QUIET_SCREEN="dim"))
        self.assertEqual((s.quiet.days, s.quiet.allow_critical, s.quiet_screen),
                         ((0, 1, 2, 3, 4), False, "dim"))
        with self.assertLogs("peekdisplay", "WARNING"):
            self.assertEqual(alerts.Settings.from_config(
                self.cfg(PEEK_QUIET_SCREEN="sideways")).quiet_screen, "none")

    def test_the_file_format_carries_the_new_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "display.conf")
            Path(p).write_text("PEEK_QUIET=22:00-07:00   # nights\nPEEK_BUZZER_PIN=18\n"
                               "PEEK_ACK_PIN=17\n")
            c = config.load(p)
            self.assertEqual(c.get("PEEK_QUIET"), "22:00-07:00")
            self.assertEqual(c.int("PEEK_BUZZER_PIN"), 18)

    def test_a_panel_option_still_passes_through(self):
        c = self.cfg(PEEK_PANEL_ADDRESS="0x3D", PEEK_BUZZER_PIN="18")
        self.assertEqual(c.panel_options, {"address": 0x3D})        # PEEK_BUZZER_PIN is not one


class TestCommandLine(unittest.TestCase):
    def run_main(self, *argv):
        spec = __import__("importlib.util").util.spec_from_file_location(
            "peek_display_main", str(ROOT / "peek-display.py"))
        mod = __import__("importlib.util").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = mod.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_test_alert_draws_on_a_png(self):
        with tempfile.TemporaryDirectory() as d:
            for sev in ("critical", "warning", "info"):
                out_png = os.path.join(d, sev + ".png")
                rc, out, _ = self.run_main("--test-alert", sev, "--panel", "png", "--out", out_png,
                                           "--config", os.path.join(d, "none.conf"))
                self.assertIn(sev, out)
                self.assertEqual(rc, 0)
                self.assertTrue(os.path.getsize(out_png) > 500)
                self.assertIn("none configured", out)

    def test_a_buzzer_that_cannot_open_is_a_warning_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "c.conf")
            Path(conf).write_text("PEEK_BUZZER_PIN=18\nPEEK_ACK_PIN=17\n")
            rc, out, err = self.run_main("--test-alert", "--panel", "png",
                                         "--out", os.path.join(d, "o.png"), "--config", conf)
            self.assertEqual(rc, 0)
            self.assertIn("no buzzer", err)
            self.assertIn("no acknowledge button", err)
            self.assertIn("python3-gpiozero", err)

    def test_demo_shows_extras_on_the_extras_view(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a.png"), os.path.join(d, "b.png")
            for path, extra in ((a, []), (b, ["--extras-view"])):
                self.run_main("--demo", "--panel", "png", "--out", path, "--width", "240",
                              "--height", "240", "--config", os.path.join(d, "none"), *extra)
            self.assertNotEqual(Path(a).read_bytes(), Path(b).read_bytes())


class TestRendering(unittest.TestCase):
    """Alert frames at every geometry the README promises."""

    SIZES = [(240, 135, False, False), (240, 240, False, False), (320, 240, False, False),
             (128, 64, True, False), (128, 32, True, False), (240, 240, False, True),
             (160, 128, False, False), (64, 48, True, False)]

    def test_every_geometry_renders_every_severity(self):
        for w, h, mono, rnd in self.SIZES:
            lay = layout.Layout.for_panel(w, h, mono=mono, round_=rnd)
            for sev in ("info", "warning", "critical"):
                for flash in (False, True):
                    a = alerts.Alert("k", sev, "A very long title " * 6, "body " * 40,
                                     "src", "host")
                    img = layout.render(lay, [], 0, Snapshot(), alert=(a, 3), flash=flash)
                    self.assertEqual(img.size, (w, h))

    def test_critical_flash_inverts(self):
        lay = layout.Layout.for_panel(240, 240)
        a = alerts.Alert("k", "critical", "t", "", "s", "h")
        on = layout.render(lay, [], 0, Snapshot(), alert=(a, 1), flash=False)
        off = layout.render(lay, [], 0, Snapshot(), alert=(a, 1), flash=True)
        self.assertNotEqual(on.getpixel((2, 2)), off.getpixel((2, 2)))

    def test_text_stays_inside_the_circle(self):
        lay = layout.Layout.for_panel(240, 240, round_=True)
        a = alerts.Alert("k", "warning", "dhaka-laptop went offline", "No reading", "s", "h")
        img = layout.circular_mask(layout.render(lay, [], 0, Snapshot(), alert=(a, 1)))
        for x, y in ((3, 3), (236, 3), (3, 236), (236, 236)):
            self.assertEqual(img.getpixel((x, y)), (0, 0, 0))        # corners stay dark

    def test_wrap_prefers_a_hyphen(self):
        from PIL import Image, ImageDraw
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        fnt = layout.font(20, True)
        lines = layout.wrap_text(draw, "dhaka-laptop", fnt, 90, 3)
        self.assertTrue(lines[0].endswith("-"), lines)


if __name__ == "__main__":
    unittest.main()
