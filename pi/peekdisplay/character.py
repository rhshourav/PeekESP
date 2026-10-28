"""Text for panels that have no framebuffer.

A 16x2 character LCD has thirty-two cells and no pixels. A MAX7219 chain is
eight rows of thirty-two dots. Neither can hold the dashboard, and pretending
otherwise produces a grey smear, so these get their own presentation: one fact
at a time, rotated.

The rotation is the design, not a compromise. A gauge is for glancing at, and
a glance at two lines of text is faster than a glance at a bar chart - what a
16x2 loses is history and comparison, not legibility.
"""

from __future__ import annotations

import textwrap
import unicodedata

from .layout import fmt_ago, fmt_capacity, fmt_rate, fmt_uptime

# How many extra sensors join the rotation. A machine can report two dozen; a
# rotation that long would take three minutes to come round, and the ones people
# put in a reading first (GPU, NPU) are the ones worth the screen time.
MAX_EXTRA_FIELDS = 6


def _ascii(text):
    """Text that every character panel can show.

    An HD44780 and the matrix's CP437 font disagree with Unicode about anything
    past ASCII: a degree sign is a different glyph, or a box, or a crash,
    depending on the charmap. The standard fields already write "48C" for that
    reason. Extras and alert text come from outside - a sensor's unit, a
    security tool's title - so they are folded to ASCII here: accents lose
    their marks, the degree sign goes, and anything else becomes "?".
    """
    text = str(text).replace("\u00b0C", "C").replace("\u00b0", "")
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(ch))
    return text.encode("ascii", "replace").decode("ascii")


def _pct(value):
    return "--" if value is None else "%d%%" % round(value)


def fields(machine, extras=True):
    """The rotation, in order. (label, value) with the label kept short.

    GPU, NPU, fans and anything else a machine reports come after the standard
    fields, so a reading without them rotates exactly as it always did.
    """
    out = [
        ("CPU", _pct(machine.cpu_percent)),
        ("RAM", _pct(machine.ram_percent)),
    ]
    if machine.storage_percent is not None:
        free = fmt_capacity(machine.storage_free_gb)
        out.append(("DISK", "%s %s fr" % (_pct(machine.storage_percent), free)))
    if machine.cpu_temp_c is not None:
        out.append(("TEMP", "%.0fC" % machine.cpu_temp_c))
    if machine.rx_kbps is not None or machine.tx_kbps is not None:
        out.append(("NET", "%s/%s" % (fmt_rate(machine.rx_kbps),
                                      fmt_rate(machine.tx_kbps))))
    if machine.has_battery:
        out.append(("BATT", "%d%%%s" % (machine.battery_percent,
                                        " CHG" if machine.battery_charging else "")))
    if machine.uptime_seconds is not None:
        out.append(("UP", fmt_uptime(machine.uptime_seconds)))
    if extras:
        for extra in machine.extras[:MAX_EXTRA_FIELDS]:
            out.append((_ascii(extra.short), _ascii(extra.text())))
    return out


def _fit(text, width):
    return text[:width] if len(text) > width else text


def _pad(text, width):
    """Trailing spaces matter: a character LCD does not clear what it wrote.

    Writing a shorter string over a longer one leaves the tail of the old
    value on screen, so "100%" becoming "9%" reads as "9%0%" until something
    longer happens to overwrite it.
    """
    return _fit(text, width).ljust(width)


def two_line(machine, snapshot, step, cols=16, offline_after_s=150,
             machine_index=0, machine_count=1, extras=True):
    """Two lines for a character LCD. ``step`` advances the rotation."""
    age = snapshot.age_of(machine)
    online = age < offline_after_s

    # Line 1 is stable: which machine, and whether to believe line 2. A
    # hostname that scrolled would make the whole display feel unreadable, so
    # it is truncated and the page marker earns its two characters.
    marker = ""
    if machine_count > 1:
        marker = "%d/%d" % (machine_index + 1, machine_count)
    head_room = cols - (len(marker) + 1 if marker else 0)
    line1 = _fit(machine.host, head_room)
    if marker:
        line1 = line1.ljust(head_room) + " " + marker

    if not online:
        return [_pad(line1, cols), _pad("OFFLINE %s" % fmt_ago(age), cols)]

    rows = fields(machine, extras)
    if not rows:
        return [_pad(line1, cols), _pad("no data", cols)]
    label, value = rows[step % len(rows)]
    line2 = "%s %s" % (label, value)
    return [_pad(line1, cols), _pad(line2, cols)]


def one_line(machine, snapshot, step, cols=16, offline_after_s=150, extras=True):
    """A single line, for a 16x1 LCD or a narrow matrix."""
    age = snapshot.age_of(machine)
    if age >= offline_after_s:
        return [_pad("%s OFF" % _fit(machine.host, cols - 4), cols)]
    rows = fields(machine, extras)
    if not rows:
        return [_pad(_fit(machine.host, cols), cols)]
    label, value = rows[step % len(rows)]
    return [_pad("%s %s" % (label, value), cols)]


def marquee(machines, snapshot, offline_after_s=150, extras=True):
    """One string for a scrolling LED matrix.

    Everything on one pass rather than a rotation: a matrix is already showing
    one character at a time, so adding a second timer on top of the scroll
    would mean a field could vanish mid-word.
    """
    if not machines:
        return "PEEK  no machines reporting"

    parts = []
    for machine in machines:
        age = snapshot.age_of(machine)
        if age >= offline_after_s:
            parts.append("%s OFFLINE %s" % (machine.host, fmt_ago(age)))
            continue
        bits = ["%s" % machine.host,
                "CPU %s" % _pct(machine.cpu_percent),
                "RAM %s" % _pct(machine.ram_percent)]
        if machine.storage_percent is not None:
            bits.append("DISK %s" % _pct(machine.storage_percent))
        if machine.cpu_temp_c is not None:
            bits.append("%.0fC" % machine.cpu_temp_c)
        if machine.has_battery:
            bits.append("BAT %d%%" % machine.battery_percent)
        if extras:
            for extra in machine.extras[:3]:
                bits.append("%s %s" % (_ascii(extra.short), _ascii(extra.text())))
        parts.append("  ".join(bits))

    # Trailing spaces so the message does not butt against its own head when
    # the scroll wraps.
    return "   *   ".join(parts) + "      "


# ---------------------------------------------------------------------------
#  Alerts
# ---------------------------------------------------------------------------
# An alert replaces the dashboard for as long as it lasts, so these are shaped
# like the dashboard's own presentations: two padded lines, one padded line, or
# one string to scroll.

_TAG = {"critical": "!!!", "warning": "!!", "info": "i"}
_WORD = {"critical": "CRITICAL", "warning": "WARNING", "info": "NOTICE"}


def _pages(text, width):
    """Cut text into screens of `width` cells, on word boundaries where it can.

    A character LCD cannot scroll a line without redrawing it, and a title cut
    off at sixteen characters is often the part that mattered, so a long one is
    shown as consecutive screens instead of truncated.
    """
    out = textwrap.wrap(text, width=width, break_long_words=True) if text else []
    return out or [""]


def alert_lines(alert, count, cols, rows, tick):
    """Lines for a character LCD. ``tick`` advances through long text."""
    tag = _TAG.get(alert.severity, "i")
    who = _ascii(alert.host or alert.source or "")
    marker = ""
    if count > 1:
        marker = " +%d" % (count - 1)
    pages = _pages(_ascii(alert.title), cols)
    if alert.body:
        pages += _pages(_ascii(alert.body), cols)
    page = pages[tick % len(pages)]

    if rows < 2:
        return [_pad("%s %s" % (tag, page), cols)]
    head = "%s %s" % (tag, who)
    head = _fit(head, cols - len(marker)).ljust(cols - len(marker)) + marker
    lines = [_pad(head, cols), _pad(page, cols)]
    return lines[:rows]


def alert_marquee(alert, count):
    """One string for a scrolling matrix, to be shown before the dashboard."""
    more = "  (+%d more)" % (count - 1) if count > 1 else ""
    where = " %s" % _ascii(alert.host) if alert.host else ""
    text = "%s %s%s: %s" % (_TAG.get(alert.severity, "i"), _WORD.get(alert.severity, ""),
                            where, _ascii(alert.title))
    if alert.body:
        text += " - " + _ascii(alert.body)
    return text + more + "      "
