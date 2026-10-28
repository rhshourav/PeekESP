# PeekESP on a Raspberry Pi

The same dashboard the ESP32 shows, on a screen attached to a Pi — colour TFT,
mono OLED, e-paper, a 16×2 character LCD, or an LED matrix.

Same pairing code, same relay, no port forward. A Pi can watch the machines
*and* be one of them: run [`dietpi/peek-agent.py`](../dietpi/) alongside this
and it appears in its own carousel.

It also shows **alerts**: a message a machine sends (a rule that tripped, a
security tool that saw something), a machine going offline, and this display
losing the relay. With an optional buzzer, a critical one is audible. And it
shows the **extra sensors** the agents now report: GPU, NPU, fans.

```bash
curl -fsSL https://raw.githubusercontent.com/rhshourav/PeekESP/main/pi/install.sh | sudo sh
```

It asks which panel is wired up, installs **only** that panel's driver, enables
the bus it needs, and starts a systemd service.

---

## Supported panels

| `PEEK_PANEL` | Hardware | Bus | Default size | Needs |
|---|---|---|---|---|
| `ili9341` | 2.4″ / 2.8″ colour TFT | SPI | 320×240 | `luma.lcd` |
| `st7789` | colour TFT | SPI | 240×240 | `luma.lcd` |
| `st7735` | 1.8″ colour TFT | SPI | 160×128 | `luma.lcd` |
| `gc9a01` | 1.28″ **round** TFT | SPI | 240×240 | `luma.lcd` |
| `ssd1306` | 0.96″ mono OLED | I²C or SPI | 128×64 | `luma.oled` |
| `sh1106` | 1.3″ mono OLED | I²C or SPI | 128×64 | `luma.oled` |
| `ssd1309` | 2.4″ mono OLED | I²C or SPI | 128×64 | `luma.oled` |
| `epaper` | Waveshare e-paper | SPI | from the panel | Waveshare SDK |
| `hd44780` | 16×2 character LCD | I²C backpack | 16×2 cells | `RPLCD` |
| `max7219` | 8×8 LED matrix chain | SPI | 32×8 | `luma.led_matrix` |
| `png` | none — writes a file | — | 240×135 | Pillow only |

Aliases: `oled`, `tft`, `round`, `16x2`, `lcd`, `matrix`, `eink`, `none`.

Any size can be overridden — the same driver ships behind several glass sizes:

```
PEEK_PANEL=ssd1306
PEEK_PANEL_WIDTH=128
PEEK_PANEL_HEIGHT=32
```

---

## Three kinds of display, three presentations

The hardware genuinely is three kinds, so pretending one layout fits produces a
grey smear on two of them.

**Framebuffer panels** — every colour TFT, the OLEDs, e-paper. These get the
dashboard. The layout is computed from the panel's dimensions rather than
hand-placed, so a size nobody has tested still comes out readable: above
240×240 the gauges are arcs, below that they are bars, and below 128×64 the
storage row and footer are dropped rather than crushed. A round panel insets
every row to the circle's actual chord at that height, so nothing is written
under the bezel.

**Character LCDs** — 16×2 is thirty-two cells. Line 1 holds the hostname and
which machine you are on; line 2 rotates through CPU, RAM, disk, temperature,
network, battery and uptime. Every line is padded to the full width, because a
character LCD clears nothing on its own — write "9%" over "100%" and you get
`9%0%` until something longer overwrites it.

**LED matrices** — eight rows of dots. Everything scrolls, once, in one pass.

---

## Wiring

Pin numbers are BCM. The defaults below are what the driver assumes; every one
is overridable with `PEEK_PANEL_GPIO_DC`, `PEEK_PANEL_GPIO_RST` and so on.

**SPI colour TFT** (ILI9341, ST7789, GC9A01)

| Panel | Pi BCM | Header pin |
|---|---|---|
| VCC | 3V3 | 1 |
| GND | GND | 6 |
| SCK / SCL | GPIO 11 | 23 |
| MOSI / SDA | GPIO 10 | 19 |
| CS | GPIO 8 (CE0) | 24 |
| DC | GPIO 25 | 22 |
| RST | GPIO 27 | 13 |
| LED / BL | 3V3 | 17 |

**I²C OLED** (SSD1306, SH1106)

| Panel | Pi BCM | Header pin |
|---|---|---|
| VCC | 3V3 | 1 |
| GND | GND | 6 |
| SDA | GPIO 2 | 3 |
| SCL | GPIO 3 | 5 |

Address is `0x3C` for most modules and `0x3D` for some. `i2cdetect -y 1` says
which; set `PEEK_PANEL_ADDRESS=0x3D` if needed.

**MAX7219 matrix**

| Panel | Pi BCM | Header pin |
|---|---|---|
| VCC | 5V | 2 |
| GND | GND | 6 |
| DIN | GPIO 10 | 19 |
| CS | GPIO 7 (CE1) | 26 |
| CLK | GPIO 11 | 23 |

If the modules are rotated or mirrored, `PEEK_PANEL_BLOCK_ORIENTATION=-90`
(or `0`, `90`) fixes it. Set `PEEK_PANEL_CASCADED` to how many are chained.

**16×2 character LCD, I²C backpack**

> **Check your backpack before wiring this to a Pi.** An HD44780 needs 5 V for
> a readable contrast, and most PCF8574 backpacks pull SDA and SCL up to
> whatever powers them. Pulling the Pi's 3.3 V I²C lines to 5 V is out of spec
> for the SoC. Use a backpack with 3.3 V-referenced pull-ups, a level shifter,
> or remove the backpack's pull-up resistors and rely on the Pi's own. Plenty
> of people wire it straight through and get away with it; it is still out of
> spec, and it is your Pi.

| Panel | Pi BCM | Header pin |
|---|---|---|
| VCC | 5V | 2 |
| GND | GND | 6 |
| SDA | GPIO 2 | 3 |
| SCL | GPIO 3 | 5 |

Address is usually `0x27` or `0x3F` — `PEEK_PANEL_ADDRESS=0x3F`.

**E-paper** — a Waveshare HAT seats on the header and needs no wiring. The
library is not on PyPI:

```bash
git clone --depth 1 https://github.com/waveshareteam/e-Paper
sudo cp -r e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd \
          /usr/lib/python3/dist-packages/
```

Then name the exact model — there is no autodetection, and the wrong module
drives the panel at the wrong resolution:

```
PEEK_PANEL=epaper
PEEK_PANEL_MODULE=waveshare_epd.epd2in13_V4
PEEK_PANEL_ROTATE=90
```

**Buzzer and acknowledge button** (optional; see *Alerts*)

| Part | Pi BCM | Header pin |
|---|---|---|
| Buzzer, via the transistor below | GPIO 18 | 12 |
| Button, other leg to GND | GPIO 17 | 11 |

`PEEK_BUZZER_PIN=18` and `PEEK_ACK_PIN=17` — any free GPIO will do. The button
needs nothing else: the Pi's internal pull-up is used, so it is simply a switch
between the pin and ground.

The buzzer must be an **active** one — a buzzer that makes its own tone when
powered. A *passive* buzzer is a speaker and needs a waveform, not a level; on
this it clicks once and goes quiet.

> **Do not drive a buzzer straight from a pin unless it is a module with its own
> driver.** A GPIO pin is good for a few milliamps and a bare active buzzer takes
> tens. Use an NPN transistor (a 2N2222 or BC547 will do): pin → 1 kΩ → base,
> emitter → GND, buzzer between 5 V and the collector, and a diode across the
> buzzer if it is the electromagnetic kind. A 3-pin buzzer *module* has that
> built in and connects to the pin directly. Many such modules switch on when the
> pin goes **low**: if yours is silent when it should sound, and sounds when it
> should not, set `PEEK_BUZZER_ACTIVE_LOW=1`.

---

## Prove the wiring before you blame the software

```bash
peek-display --self-test
```

A panel with its rotation wrong, its colour order reversed, or one data line
loose still shows *something* when handed a dashboard — and a dashboard that is
subtly wrong looks like a bug in the software. The test pattern cannot be
mistaken: a one-pixel white frame on all four edges, magenta ticks in each
corner, red-green-blue-white swatches left to right, and the panel's size in the
middle.

- An edge missing → rotation or size is wrong.
- Swatches in the wrong order → the colour order is `BGR`, not `RGB`.
- Corner ticks clipped → the panel is cropping; check width and height.
- Nothing at all → wiring, bus not enabled, or the wrong address.

Then, with invented data and no pairing code:

```bash
peek-display --demo
```

Then the alert path — the screen, the buzzer and the button together:

```bash
peek-display --test-alert critical      # or warning, or info
```

It draws a sample alert, rings the buzzer if one is configured (critical rings
until the test ends; warning is two beeps; info is silent), and if there is a
button it waits eight seconds for you to press it and says whether it heard you.
A missing `gpiozero` or a pin that cannot be opened is reported by name and does
not stop the display from running.

And without any hardware at all — this is how the layouts were checked:

```bash
peek-display --demo --panel png --width 240 --height 240 --out frame.png
peek-display --demo --panel png --width 128 --height 64 --scale 4
```

---

## Configuration

`/etc/peekesp/display.conf`, root-readable only, because the pairing code is the
credential.

| Key | Default | |
|---|---|---|
| `PEEK_PAIR_CODE` | — | as shown on the device |
| `PEEK_RELAY_BASE` | the public Worker | your own deployment, if you have one |
| `PEEK_PANEL` | `png` | see the table above |
| `PEEK_INTERVAL` | `5` | seconds between polls |
| `PEEK_ROTATE_SECONDS` | `8` | how long each machine stays on screen |
| `PEEK_OFFLINE_AFTER_POLLS` | `5` | missed polls before a machine reads OFFLINE |
| `PEEK_ALERTS` | `1` | show alerts at all |
| `PEEK_OFFLINE_ALERTS` | `1` | alert when a machine goes offline and comes back |
| `PEEK_OFFLINE_SEVERITY` | `warning` | `info`, `warning` or `critical` |
| `PEEK_INFO_SECONDS` / `PEEK_ALERT_SECONDS` / `PEEK_CRITICAL_SECONDS` | `10` / `30` / `600` | how long an alert stays on screen |
| `PEEK_ALERT_MAX_AGE` | `900` | a message older than this when first seen is filed quietly |
| `PEEK_BUZZER_PIN` / `PEEK_BUZZER_ACTIVE_LOW` | — / `0` | an active buzzer, BCM pin |
| `PEEK_ACK_PIN` | — | a button to ground that dismisses alerts |
| `PEEK_QUIET` | — | quiet hours, `22:00-07:00` |
| `PEEK_QUIET_DAYS` / `PEEK_QUIET_CRITICAL` | `all` / `1` | the days a window *begins*; whether critical gets through |
| `PEEK_QUIET_SCREEN` | `none` | `dim` or `off`, on a panel that can |
| `PEEK_EXTRAS` | `1` | GPU, NPU and fans in the footer and the LCD rotation |
| `PEEK_PANEL_*` | — | passed straight to the driver |

**The poll interval is a shared budget.** Cloudflare's free tier is 100,000
requests a day. Each agent at 5 s costs 17,280, each ESP32 display costs 17,280,
and so does this. Two machines and two displays is already ~69k. `PEEK_INTERVAL=15`
divides this one by three.

---

## When something is offline

A machine that stops reporting keeps its last numbers on screen, dimmed and
captioned `OFFLINE`, with how long it has been silent — what it was doing when
it stopped is the most useful thing left to show.

The age counted is the relay's own timestamp **plus** the time since this Pi
last managed a successful fetch. Without that second term a Pi that lost its
network would show every machine as permanently fresh, which is exactly
backwards: at that moment it knows nothing at all.

---

## Alerts

Three things become an alert, and what the display does depends on the severity:

| `severity` | On screen | Buzzer |
|---|---|---|
| `info` | a notice for 10 s | none |
| `warning` | for 30 s | two short beeps, once |
| `critical` | until acknowledged, or 10 minutes; blinks | repeats until acknowledged |

* **A message from a machine.** The agents run the alert rules and carry messages
  from security tools in their readings; this shows them, with the severity the
  sender gave (`red` is critical). The display does not run rules of its own:
  that would only duplicate what the agents and the phone already do.
* **A machine going offline, and coming back.** The display works this out for
  itself from the age the relay reports. It does **not** fire when the display
  has just booted and finds a machine already silent: that is state, not news.
* **This display losing the relay.** After a minute of failed fetches, once.

**This Pi losing its network is not every machine dying.** Offline alerts are only
judged on a reading the relay actually answered. When the Pi cannot reach the
relay it raises the one "No connection" alert and nothing about the machines,
because at that moment it knows nothing about them. A rejected pairing code is not
the link being down either, and says so on screen instead.

A message stays in the agents' readings for hours, so a phone that was off can
catch up. The display shows it **once**, by its id, and a message more than 15
minutes old when the display first sees it is filed quietly (`PEEK_ALERT_MAX_AGE`):
a Pi that has just been powered on must not ring for something from last night.

**Quiet hours** (`PEEK_QUIET=22:00-07:00`) hide everything except critical, and
silence the buzzer. They do not queue what they hide: an alert that arrived at
23:30 is not shown at 07:00. `PEEK_QUIET_DAYS` are the days the window *begins*, so
a Friday night runs into Saturday morning. `PEEK_QUIET_SCREEN` can also dim or
blank the panel, and wakes it for a critical alert:

| Panel | `dim` | `off` |
|---|---|---|
| SSD1306 / SH1106 / SSD1309 OLED | contrast to 15 % | contrast 0 |
| MAX7219 matrix | contrast to 15 % | contrast 0 |
| HD44780 | off — a backlight is a switch | backlight off |
| TFT (ILI9341, ST7789, GC9A01) | off — and only if `PEEK_PANEL_GPIO_LIGHT` is set | backlight off, same condition |
| e-paper, `png` | — | — |

A panel that cannot do it logs one warning and carries on; quiet hours still
silence the buzzer.

**E-paper** keeps its three-minute floor between redraws. A new alert redraws it
once, early, and a cleared one the same; not more than every 30 seconds.

The **critical alert does not need a button.** Without `PEEK_ACK_PIN` it ends after
`PEEK_CRITICAL_SECONDS`, like the phone's ten-minute alarm.

---

## Extra sensors

GPU, NPU, fans and anything else a machine adds to its reading
([`PROTOCOL.md`](../PROTOCOL.md)) appear without configuration, read by the same
rules the agents and the phone use: a negative number means "this machine has no
such sensor" and is left out, and at most 24 are read.

* **Colour and mono panels** with a footer swap it for the extras on alternate
  turns, a page at a time. Pages hold **whole** items: at 240 px a line takes
  `GPU 42.5%   GPU° 61°C   NPU 7%`, and an item that would be cut off starts the
  next page instead, because half a value is a wrong number. With several
  machines each takes its own turn; the pages are counted per machine, not per
  rotation.
* **Character LCDs** add them to the rotation after uptime, up to six, as
  `GPU 42%` and `GPU 61C`. The degree sign is dropped: an HD44780 and the matrix's
  font disagree with Unicode about everything past ASCII, which is also why alert
  text from outside is folded to plain ASCII on those panels (`café` is `cafe`).
* **LED matrices** scroll up to three after the usual fields.
* **Layouts with no footer** — anything under 64 px tall (128×32, 64×48) and round
  panels under 200 px — do not show extras. There is nowhere to put them that
  would not crush the gauges. A 128×64 OLED does have a footer, and does.

`PEEK_EXTRAS=0` turns it off.

**For any of this to arrive, the relay must pass `messages` and `metrics` through
untouched.** If it copies a fixed list of fields, add them there.

---

## E-paper refreshes slowly on purpose

A full refresh takes one to three seconds of visible inversion, and the panel
has a finite number of them in it. Polling every five seconds would flash all
day and wear it out for numbers nobody was watching that closely.

So `epaper` sets a floor of three minutes between redraws, and a frame identical
to the one already on the glass is not drawn at all. Nothing is lost — e-paper
holds its image with the power off, which is the point of it.

```
PEEK_PANEL_MIN_INTERVAL=600      # ten minutes, for a battery build
```

---

## Service

```bash
sudo systemctl status peek-display
journalctl -u peek-display -f
sudo systemctl restart peek-display
```

It runs as root, unlike the telemetry agent — which drops to its own account,
because reading `/proc` needs nothing. This one needs `/dev/spidev`, `/dev/i2c`
and GPIO, and adding a user to the `spi`, `i2c` and `gpio` groups does not cover
the sysfs paths some of these libraries reach for. That is a real trade-off and
it seemed better to say so than to ship a hardened-looking unit that quietly
needs root anyway.

`systemctl stop` sends SIGTERM, which would normally end the process on the
spot. The display turns it into the same orderly shutdown as Ctrl-C, so a buzzer
that was sounding is switched off and released rather than left driven.

Upgrading is the same command as installing: run the installer again. It keeps
your `display.conf`, adds the new settings to it as comments, and asks about a
buzzer and a button (blank skips them). The installer fetches an explicit list of
files, and `alerts.py` is now one of them.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

Pillow is the only requirement, and no display library is needed — the panel
adapters are exercised through the `png` panel, which is the same code path as
a real one right up to the point the bytes go out over SPI. Every geometry in
the table is rendered and checked, in every state: online, offline, empty, and
error.

**What the tests cannot cover is SPI and I²C themselves** — whether a GC9A01 is
on the right pins, whether an OLED is at `0x3C` or `0x3D`. That is what
`--self-test` is for, and it needs the panel in your hand.

`tests/test_display_alerts.py` covers the alert logic and everything around it,
with a fake pin standing in for the buzzer: the buzzer patterns, the quiet-hours
windows, "the Pi lost its network is not every machine offline", a real
subprocess stopped with SIGTERM, and every severity at every geometry in the table
above. **It cannot cover the pin itself, or whether `dim` does anything on your
particular panel** — the brightness calls are made through each library's own
API and have been exercised against stand-ins, not against glass.
`--test-alert critical` is for the pin.
