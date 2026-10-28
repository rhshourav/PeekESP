"""The render loop.

Polling and drawing are deliberately on one thread. The ESP32 splits them
across two cores because a blocking TLS handshake there would drop an LVGL
frame; here there are no frames to drop - a Pi redrawing a static dashboard
every five seconds is not animating anything, and a thread would buy nothing
but a race over the snapshot.
"""

from __future__ import annotations

import logging
import signal
import time

from . import alerts as alerts_mod
from . import character, layout, panels
from .relay import AuthError, Relay, RelayError

log = logging.getLogger("peekdisplay")

# The least time between two urgent redraws on a panel that limits its own
# refreshes. A new alert is worth one e-paper flash; a flapping machine is not
# worth one every few seconds.
URGENT_GAP_S = 30.0
# While a critical alert is up the loop ticks this fast: the blink needs it, and
# so does a button press, which should not wait out a five-second poll.
CRITICAL_BEAT_S = 1.0


def _on_sigterm(signum, frame):
    # systemd stops a service with SIGTERM, which by default ends the process on
    # the spot - past every `finally`. A buzzer that was sounding would be left
    # driven, and stay on after `systemctl stop`. Raising the exception the loop
    # already handles makes the shutdown path the same as Ctrl-C.
    raise KeyboardInterrupt


class Display:
    """Owns a panel and a relay, and keeps them in step."""

    def __init__(self, panel, relay, interval=5, rotate_seconds=8,
                 offline_after_s=150, alerts=None, alarm=None, extras=True,
                 quiet_screen="none"):
        self.panel = panel
        self.relay = relay
        # Everything below is optional, and a Display built without it behaves
        # exactly as before: no alert centre, a silent alarm, the usual footer.
        self.alerts = alerts
        self.alarm = alarm or alerts_mod.Alarm()
        self.extras = bool(extras)
        self.quiet_screen = quiet_screen
        self._level = None
        self._level_warned = False
        self._new_alerts = []           # new since the last DRAW (drives urgency)
        self._to_chirp = []             # new since the last beat (drives the buzzer)
        self._alert_up = False
        self._urgent_pending = False
        self.interval = max(1.0, float(interval))
        self.rotate_seconds = max(1.0, float(rotate_seconds))
        self.offline_after_s = int(offline_after_s)

        self.index = 0
        self.step = 0
        self._layout = None
        self._last_draw = 0.0
        self._last_rotate = time.monotonic()
        self._error = None

        if panel.kind == panels.KIND_PIXEL:
            width, height = panel.size
            self._layout = layout.Layout.for_panel(
                width, height, mono=panel.mono, round_=panel.round
            )

    # -- one pass -----------------------------------------------------------

    def poll(self):
        snapshot, error = self.relay.poll()
        if error is not None and not isinstance(error, AuthError):
            log.warning("%s", error)
        self._error = error
        if self.alerts is not None:
            fresh = self.alerts.update(snapshot, error)
            self._new_alerts += fresh
            self._to_chirp += fresh
        return snapshot

    # -- alerts: sound, screen level, and what to draw -------------------------

    def _critical_up(self):
        return self.alerts is not None and self.alerts.critical_active()

    def drive_alarm(self):
        """Make the buzzer match what is on screen. Called every beat."""
        if self.alerts is None:
            return
        live = self.alerts.active()
        if any(a.severity == "critical" for a in live):
            self.alarm.ring()
        elif self.alarm.ringing:
            self.alarm.stop()           # acknowledged, expired, or quiet hours began
        # Consumed here and not at the next draw: a rate-limited panel can go
        # minutes between draws, and a warning must not chirp on every beat of
        # that wait.
        chirps, self._to_chirp = self._to_chirp, []
        for alert in chirps:
            # A warning is two beeps, once. It is not repeated: it is on screen
            # for half a minute, and nobody wants a buzzer for a machine that
            # went offline.
            if alert.severity == "warning" and alert in live and not self._critical_up():
                self.alarm.chirp()

    def apply_screen_level(self):
        """Dim or blank the panel for quiet hours, if it can and was told to."""
        if self.alerts is None or self.quiet_screen == "none":
            return
        want = 1.0
        if self.alerts.quiet_now() and not self.alerts.critical_active():
            want = 0.0 if self.quiet_screen == "off" else 0.15
        if want == self._level:
            return
        self._level = want
        if not self.panel.set_level(want) and not self._level_warned:
            self._level_warned = True
            log.warning("the %s panel cannot be dimmed or blanked, so PEEK_QUIET_SCREEN "
                        "has no effect on it; quiet hours still silence the buzzer",
                        self.panel.name)

    def _urgent(self):
        """Has something happened that should not wait for the next redraw?"""
        shown = self.alerts is not None and self.alerts.current() is not None
        if self._new_alerts or (self._alert_up and not shown):
            # Remembered until a draw actually happens. If the panel's own
            # refresh limit holds this redraw back, forgetting it would leave an
            # alert on the glass long after it was acknowledged.
            self._urgent_pending = True
        self._alert_up = shown
        return self._urgent_pending

    def advance(self, snapshot):
        """Move to the next machine once its time is up."""
        count = len(snapshot.machines)
        now = time.monotonic()
        if count > 1 and now - self._last_rotate >= self.rotate_seconds:
            self.index = (self.index + 1) % count
            self._last_rotate = now
        elif count <= 1:
            self.index = 0
        # The character panels rotate through fields rather than machines, and
        # do it on the same clock so the two never fight over the screen.
        self.step = int(now // self.rotate_seconds)

    def message_for(self, snapshot):
        """What to show instead of a dashboard, or None to draw one."""
        if isinstance(self._error, AuthError):
            return ("Pairing failed",
                    "the code was rejected",
                    "check the device's screen")
        if not snapshot.machines:
            if self._error is not None:
                return ("No connection", str(self._error)[:60])
            return ("No machines", "are reporting yet")
        return None

    def draw(self, snapshot):
        panel = self.panel
        self._new_alerts = []
        self._urgent_pending = False
        alert = self.alerts.current() if self.alerts is not None else None
        tick = int(time.monotonic())

        if panel.kind == panels.KIND_MARQUEE:
            message = self.message_for(snapshot)
            if message:
                panel.display("  ".join(message))
            else:
                text = character.marquee(
                    snapshot.machines, snapshot, self.offline_after_s,
                    extras=self.extras)
                if alert:
                    # The alert goes first, once, so it is the first thing read.
                    text = character.alert_marquee(*alert) + text
                panel.display(text)
            return

        if panel.kind == panels.KIND_TEXT:
            cols, rows = panel.size
            message = self.message_for(snapshot)
            if message:
                lines = [str(m)[:cols].ljust(cols) for m in message[:rows]]
                panel.display(lines + [" " * cols] * (rows - len(lines)))
                return
            if alert:
                panel.display(character.alert_lines(alert[0], alert[1], cols, rows, tick))
                return
            machine = snapshot.machines[self.index % len(snapshot.machines)]
            renderer = character.two_line if rows >= 2 else character.one_line
            panel.display(renderer(
                machine, snapshot, self.step, cols=cols,
                offline_after_s=self.offline_after_s, extras=self.extras,
                **({"machine_index": self.index % len(snapshot.machines),
                    "machine_count": len(snapshot.machines)} if rows >= 2 else {})
            ))
            return

        # Footer phase: which machine is up, counted so that with two machines
        # each still takes its turn at showing extras. Keyed off self.step
        # alone, machine A would always land on the usual footer and B always
        # on the extras, because both advance on the same clock.
        count = max(1, len(snapshot.machines))
        phase = 0
        if self.extras and snapshot.machines:
            machine = snapshot.machines[self.index % len(snapshot.machines)]
            pages = layout.extras_pages(self._layout, machine)
            if pages:
                # 0 is the usual footer; 1..n are the extras, a page at a time.
                phase = (self.step // count) % (1 + len(pages))
        # Blink only where redrawing is free. An e-paper panel limits its own
        # refreshes; flashing it would wear it out in a day.
        flash = bool(alert and alert[0].severity == "critical"
                     and panel.min_interval == 0 and tick % 2 == 0)
        image = layout.render(
            self._layout, snapshot.machines, self.index, snapshot,
            offline_after_s=self.offline_after_s,
            message=self.message_for(snapshot),
            alert=alert, phase=phase, flash=flash,
        )
        if panel.round:
            image = layout.circular_mask(image)
        if panel.mono:
            image = layout.to_mono(image)
        panel.display(image)

    def tick(self):
        """Poll if due, then draw. Returns the snapshot that was drawn."""
        snapshot = self.poll()
        self.advance(snapshot)
        self.drive_alarm()
        self.apply_screen_level()

        now = time.monotonic()
        if now - self._last_draw >= self.panel.min_interval:
            self.draw(snapshot)
            self._last_draw = now
        return snapshot

    # -- forever ------------------------------------------------------------

    def run(self):
        """Until interrupted. A relay failure ages the data, it does not stop."""
        # The rotation is usually faster than the poll, so the loop runs on the
        # shorter of the two and only fetches when the interval is actually up.
        beat = min(self.interval, self.rotate_seconds)
        if self.panel.kind == panels.KIND_MARQUEE:
            # A scroll blocks for as long as it takes to read; adding a sleep
            # on top would leave the matrix dark between passes.
            beat = 0.0

        next_poll = 0.0
        snapshot = self.relay.last
        try:
            signal.signal(signal.SIGTERM, _on_sigterm)
        except ValueError:
            pass                    # not the main thread; the caller owns signals
        try:
            while True:
                now = time.monotonic()
                if now >= next_poll:
                    snapshot = self.poll()
                    next_poll = now + self.interval
                self.advance(snapshot)
                self.drive_alarm()
                self.apply_screen_level()

                since = time.monotonic() - self._last_draw
                due = since >= self.panel.min_interval
                # A new alert (or one being cleared) redraws a rate-limited
                # panel early - once, and not more than every URGENT_GAP_S.
                urgent = self._urgent() and since >= min(URGENT_GAP_S, self.panel.min_interval)
                if due or urgent:
                    self.draw(snapshot)
                    self._last_draw = time.monotonic()

                step = beat
                if beat and self._critical_up():
                    step = min(beat, CRITICAL_BEAT_S)
                if step:
                    # Sleep only as long as is left, so a slow draw does not
                    # push the poll later and later.
                    slack = step - (time.monotonic() - now)
                    if slack > 0:
                        time.sleep(slack)
        except KeyboardInterrupt:
            log.info("stopping")
        finally:
            self.alarm.stop()       # never leave a buzzer driven


def self_test(panel):
    """Draw something whose correctness is obvious across the room.

    The point of this is wiring. A panel with its RGB order wrong, its rotation
    wrong, or one data line not connected still shows *something* when handed a
    dashboard - and a dashboard that is subtly wrong looks like a bug in the
    software. A named colour bar and a labelled frame do not: if the box is not
    on all four edges the geometry is wrong, and if the swatches are in the
    wrong order the colour order is.
    """
    from PIL import Image, ImageDraw

    if panel.kind == panels.KIND_MARQUEE:
        panel.display("PEEK SELF TEST 0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ  ")
        return "scrolled the alphabet - every character should be complete"

    if panel.kind == panels.KIND_TEXT:
        cols, rows = panel.size
        lines = ["".join(str(i % 10) for i in range(cols))]
        lines.append(("PEEK SELF TEST " * 3)[:cols])
        panel.display(lines[:rows])
        return ("row 0 counts 0-9 across every cell; if it stops short, cols "
                "is wrong")

    width, height = panel.size
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)

    # A one-pixel frame on the exact boundary. Any rotation or offset error
    # takes an edge off it.
    draw.rectangle((0, 0, width - 1, height - 1), outline=(255, 255, 255))

    swatches = [("R", (255, 0, 0)), ("G", (0, 255, 0)), ("B", (0, 0, 255)),
                ("W", (255, 255, 255))]
    band = max(6, height // 6)
    step = width // len(swatches)
    for i, (label, colour) in enumerate(swatches):
        x0 = i * step
        draw.rectangle((x0 + 2, height - band - 2, x0 + step - 2, height - 3),
                       fill=colour)
        draw.text((x0 + 4, height - band - 14), label, fill=(255, 255, 255))

    fnt = layout.font(max(10, min(width, height) // 8), True)
    caption = "%dx%d" % (width, height)
    w, h = layout.text_size(draw, caption, fnt)
    draw.text(((width - w) // 2, (height - h) // 2 - 6), caption,
              font=fnt, fill=(255, 255, 255))
    # Corner ticks, so a panel cropping a few columns is visible.
    for cx, cy in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        draw.line((cx, cy, cx, cy + (8 if cy == 0 else -8)), fill=(255, 0, 255))
        draw.line((cx, cy, cx + (8 if cx == 0 else -8), cy), fill=(255, 0, 255))

    if panel.round:
        image = layout.circular_mask(image)
    if panel.mono:
        image = layout.to_mono(image)
    panel.display(image)
    return ("a full white frame with magenta corner ticks, R G B W left to "
            "right, and the size in the middle")
