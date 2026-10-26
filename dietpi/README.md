# The Linux agent

Reads this machine and pushes it to the relay every five seconds. Standard
library only — no `pip`, no `psutil`, nothing to install on a DietPi.

Works on DietPi, Raspberry Pi OS, Debian, Ubuntu, and anything else with
systemd and Python 3.7 or later. (The installer used to say 3.6. The agent serves
with `ThreadingHTTPServer`, which arrived in 3.7, so a 3.6 machine passed the
check and then failed to start.)

## Install

Flash the device first, so it has a pairing code on screen. Then:

```bash
curl -fsSL https://raw.githubusercontent.com/rhshourav/PeekESP/main/dietpi/install.sh | sudo sh
```

It asks for the code and derives everything else locally. Nothing else is typed,
and nothing is configured on the relay.

Non-interactively — the code as an argument:

```bash
curl -fsSL https://raw.githubusercontent.com/rhshourav/PeekESP/main/dietpi/install.sh | sudo sh -s -- K7M2-P4QX-9R
```

Piping a script from the internet into a root shell means trusting whatever is
at that URL. If you would rather read it first — and that is genuinely the
better habit, not a disclaimer:

```bash
curl -fsSLO https://raw.githubusercontent.com/rhshourav/PeekESP/main/dietpi/install.sh
less install.sh && sudo sh install.sh
```

## The `peekesp` command

The installer puts a `peekesp` command on the path. It is the whole interface —
you should not need `systemctl` or `journalctl` for anything routine.

```
peekesp status              is it running, and is it actually pushing
peekesp start|stop|restart  service control
peekesp enable|disable      start at boot, or not
peekesp logs [-f] [-n N]    journal for the agent
peekesp test                one reading, as this machine would report it

sudo peekesp pair CODE      re-pair to a different device
peekesp config              current settings
sudo peekesp set KEY VALUE  interval SECONDS | relay URL | serve on|off
sudo peekesp cmd VERB [N]   tell the display: reboot, standby, wake, refresh,
                            identify, page N, bright 0-3
peekesp check               is a newer release out
sudo peekesp update         fetch the latest agent and restart
sudo peekesp repair         re-apply the service settings, no download
sudo peekesp uninstall      remove the service, the files and the account
peekesp version

peekesp notify ...          leave a message for your phone
peekesp rules | quiet | hook | sensors | alerts
                            alert rules, quiet hours, extra sensors
```

`status` answers the question that actually matters, which is not whether a
service is running. A rejected push leaves the unit perfectly `active` while
nothing reaches the display, so `status` reads the journal and says so:

```
$ peekesp status
service   running
at boot   enabled
machine   dietpi
interval  5s
relay     https://peek-relay.peekesp.workers.dev
stream    e67e3b0d5b07d6ba

pushes are being REJECTED - usually the wrong pairing code.
re-pair with:  sudo peekesp pair NEW-CODE
```

`test` prints one reading and pushes nothing, which separates *is the agent
reading this machine correctly* from *is it reaching the relay* — two problems
that look identical from the device.

`set` writes to the config file and restarts the service if it was running.
The systemd unit carries no flags at all: everything comes from
`/etc/peekesp/agent.conf`, so changing the poll interval rewrites one line
rather than regenerating and reloading a unit.

```bash
sudo peekesp set interval 15      # a third of the requests
sudo peekesp set serve on         # also answer on :6595, for a LAN device
sudo peekesp set relay https://my-worker.workers.dev
```

`update` refuses to install a download that is not valid Python, because a
truncated fetch would otherwise take the service down until the next release.

### Telling the display what to do

The same commands the Windows app sends, from the command line:

```bash
sudo peekesp cmd identify     # flash the screen - which display is this?
sudo peekesp cmd refresh      # poll now instead of waiting out the interval
sudo peekesp cmd standby      # screen off, polling slowed to once a minute
sudo peekesp cmd wake
sudo peekesp cmd page 2       # the machines first, then the clock, then power
sudo peekesp cmd bright 1     # 0-3: 100 / 59 / 27 / 8 %
sudo peekesp cmd reboot
```

The display polls and never listens, so a command is left at the relay and
collected on its next poll — **queued**, not done. That is within about five
seconds, or a minute if the display is in standby. A command still waiting
after five minutes is dropped rather than delivered late, so a `standby` sent
while the display was unplugged does not fire when it comes back.

It needs root because the pairing code is the credential and the file holding
it is readable by root only. The command is authorised with the push token
derived from that code — the one this machine already pushes telemetry with —
so it grants nothing this machine did not already have. Every command is undone
by a button on the display, and none of them changes configuration.

`bright` stops at 3 here even though the relay accepts up to 15: the display has
four backlight levels and silently ignores anything past them, and a command
that is accepted and quietly does nothing is the least useful way to fail.

"Queued" means the relay accepted it for *this machine's* pairing code. If the
display has since been given a new code, it reads a different stream and the
command will never arrive — re-pair first with `sudo peekesp pair NEW-CODE`.

### Is there an update

```
$ peekesp check
installed 1.2.0; up to date (latest is 1.2.0)
```

It asks GitHub for the newest release and compares the versions as numbers, so
1.10.0 is correctly newer than 1.9.0. The exit status is the answer — `0` up to
date, `10` a newer release exists, `1` GitHub could not be reached — so it can
drive a cron job or a monitoring check without parsing the sentence. A failed
check never reports an update: a network error is not news.

## Alerts, extra sensors and messages

The phone has notifications, quiet hours and alert rules of its own. This is what
the **machine** adds. None of it needs a restart: settings live in
`/etc/peekesp/alerts.json`, the agent re-reads it within five seconds of a change,
and a file that fails validation is *not* applied (the previous settings stay, and
the journal says why), so a typo mid-edit cannot turn your rules off.

```
peekesp alerts              summary: rules, sensors, quiet hours, hook
sudo peekesp alerts edit    open the file in $EDITOR, then validate it
```

### Rules

They run here on every sample, whether or not the phone is looking: a spike at
3 a.m. is caught while the phone is asleep and waits in the next reading. The phone
judges its own rules on whatever it last read. **Use one or the other for a given
condition**: both trip, and you are told twice.

```
$ peekesp rules
cpu_high           on   CPU above 90% for 5 min - warning
ram_high           on   Memory above 90% for 5 min - warning
cpu_hot            on   CPU temperature above 85°C for 1 min - critical
storage_full       on   Storage above 92% - warning
battery_low        on   Battery below 20% - warning, on battery only
battery_critical   on   Battery below 8% - critical, on battery only
net_unusual        on   Network unusually high (over 2000 kbps) for 5 min - warning

$ sudo peekesp rules add gpu_percent above 95 --for 120 --severity red --id gpu_hot
$ sudo peekesp rules off cpu_high
$ sudo peekesp rules add disk_temp_c above 60 --severity warning --title "{machine}: disk at {value}"
```

A rule watches any metric the reading contains, including every extra sensor below.
`unusual` is judged against this machine's own last 24 hours (median and median
absolute deviation, so one spike cannot stretch its own yardstick); it needs about
six hours of history before it says anything, and `--min` sets a floor under which
it stays quiet. A rule sends one message when it trips and an all-clear when it
recovers, and editing rules during an incident does not make the phone ring again.
Wording takes `{machine} {label} {value} {limit} {duration} {typical}`.

**Offline and online is the phone's job.** A machine that has stopped cannot report
that it stopped; the phone sees the relay go quiet and says so. What this machine
can tell you is that *it* cannot reach the relay (the journal says
`relay unreachable`, and the hook below is run), and when it can again. Only a
failure to connect counts: a rejected code is reported as that.

### Quiet hours and the hook

A phone has a screen and a speaker; a headless box has neither. The equivalent is a
**hook**: a command you choose, run for every new message. A buzzer on a GPIO pin,
`wall`, a webhook, ntfy, Telegram.

```bash
sudo peekesp hook set --min warning /usr/local/bin/my-alert
sudo peekesp quiet 22:00 07:00 --days weekdays      # the days the window BEGINS
```

The message arrives in the **environment**, never the command line, so nothing in
a message can be read as an argument or as shell: `PEEK_SEVERITY`, `PEEK_TITLE`,
`PEEK_BODY`, `PEEK_SOURCE`, `PEEK_ID`, `PEEK_HOST`, `PEEK_TS`.

```sh
#!/bin/sh
# /usr/local/bin/my-alert
curl -s -H "Title: $PEEK_TITLE" -d "$PEEK_BODY" https://ntfy.sh/my-topic
```

**Quiet hours silence the hook, not the phone.** The phone has quiet hours of its
own, and a message dropped here would never reach its Alerts tab at all. Critical
gets through unless you pass `--block-critical`. A window that crosses midnight
belongs to the day it began, so a Friday 22:00 to 07:00 window covers Saturday at
03:00.

The hook runs inside the service's sandbox: no capabilities, a read-only
filesystem, `PrivateTmp`. That is the right default for something that reacts to
network input, and it means a hook that needs `/dev/gpiochip0` needs
`SupplementaryGroups=gpio` in `sudo systemctl edit peek-agent`. Its environment
has **the pairing code removed**: `PEEK_PAIR_CODE` is in the service's
environment (systemd loads `agent.conf`), and a hook or sensor command would
otherwise inherit the one value that lets anything push to your display. It is
stopped after 15 seconds and at most four run at once.

### Extra sensors

Read in this order; the first source to answer a reading wins it.

| Reading | Key | Source |
|---|---|---|
| GPU load, temperature | `gpu_percent`, `gpu_temp_c` | `nvidia-smi`, else the `amdgpu` driver's own files |
| GPU power, memory, fan | `gpu_power_w`, `gpu_mem_percent`, `vram_used_gb`, `gpu_fan_percent` / `gpu_fan_rpm` | the same |
| NPU load | `npu_percent` | Intel's `intel_vpu` driver (kernel 6.11 and later) |
| Fastest case fan | `fan_rpm` | `/sys/class/hwmon` |
| NVMe / SATA temperature | `disk_temp_c` | `/sys/class/hwmon` (`nvme`, `drivetemp`) |

```
$ peekesp sensors
GPU                  42 %   (nvidia)
GPU temperature      61 °C   (nvidia)
Fan                  1500 rpm   (hwmon)
Disk temperature     42 °C   (hwmon)
NPU                  3.2 %   (npu)
```

**NPU load is a rate, not a reading.** The driver publishes `npu_busy_time_us`,
microseconds spent executing jobs, so utilisation is the change in that counter
over the change in time, and the first read only sets a baseline. Other vendors'
NPUs publish nothing comparable that an unprivileged service can read (Rockchip's
is in debugfs, root only), and are not guessed at: add one as a custom sensor if
yours exposes a file. Intel GPUs have no load figure without `perf`, so they show
nothing here rather than something invented.

A source that finds nothing is asked less and less often, up to every five
minutes, so a machine with no NVIDIA card is not launching `nvidia-smi` forever. A
reading whose source goes quiet is dropped after a few intervals rather than left
frozen on the dashboard.

**Your own** are a command, a file or a URL that yields a number. A negative number
is left out, because the phone reads it as "this machine has no such sensor".

```bash
# any sysfs file: millidegrees to degrees
sudo peekesp sensors add tank_c file /sys/class/hwmon/hwmon4/temp1_input \
     --scale 0.001 --unit °C --label "Coolant"

# a command, with the number picked out by a regex
sudo peekesp sensors add ping_ms command "ping -c 1 -W 1 1.1.1.1" \
     --regex 'time=([0-9.]+)' --unit ms --label Ping

# a local service's JSON, by path
sudo peekesp sensors add inverter_w http http://127.0.0.1:8080/status \
     --json-path data.power_w --unit W --label Solar

peekesp sensors test ping_ms
```

`command` runs **without a shell**, so nothing in it is interpreted. Each sensor is
read once when added and tells you straight away whether it works.

### Messages from other programs

Anything on this machine can put a message in front of your phone: a security tool,
a backup script, a cron job. **[NOTIFY.md](NOTIFY.md)** has the details and examples.

```bash
peekesp notify --severity red --title "Intrusion" --message "New login from 203.0.113.9"
```

The **severity** decides what the phone does: `info` is quiet, `warning` uses the
normal sound and a popup, `critical` (or `red`) rings like an alarm on the alarm
volume, so on silent too, until silenced or ten minutes pass. Each message stays in
every reading for six hours (`sudo peekesp set ttl 12`), because a reading is the
latest state, not a queue, and a phone looking every fifteen minutes with the app
closed would otherwise miss one. The `id` makes the repeats count once.

**Two things to know about where messages travel:**

* They go to your **relay** inside the reading, like everything else, and the relay
  operator can read them. Do not put a password or a one-time code in one.
* They are **left out of the LAN endpoint** (`serve on`), which listens on every
  interface with no password. Numbers are served there; messages are not.

### Where it keeps things

| | |
|---|---|
| `/etc/peekesp/agent.conf` | the pairing code and scalar settings. Root only, `0600` |
| `/etc/peekesp/alerts.json` | rules, quiet hours, hook, your sensors. `0640`, group `peekesp` |
| `/var/lib/peekesp/` | the outbox, history, the endpoint's key, the drop folder. Owned by the service |

`/var/lib/peekesp` comes from `StateDirectory=` in the unit. A machine installed
before this existed has a unit without it, and `sudo peekesp update` (or `sudo
peekesp repair`, which downloads nothing) adds it as a drop-in. `peekesp status`
says so if it is missing: the agent keeps running, but messages live in memory only
and the local endpoint is off, rather than failing. Settings in `agent.conf`:
`sudo peekesp set alerts|sensors|notify on|off`, `notify-port N`, `ttl HOURS`,
`extras-interval SECONDS`.

A **package** that ships its own unit gets the same drop-in from `peekesp repair`;
its unit file is not touched.

## Several machines, one code

Run the same command with the **same pairing code** on every machine. They all
appear on the one display, and the left button swipes between them. Six per
code; a machine silent for 24 hours drops off the list on its own.

Nothing extra to configure — each agent already reports its own hostname, and
the relay keeps a slot per host rather than letting them overwrite each other.

The cost is per *agent*, not per screen: the device polls once and gets all of
them, so it makes the same number of requests whether it shows one machine or
six. At the default five-second interval each agent is 17,280 requests/day
against Cloudflare's 100,000/day free allowance, so one display and two machines
fits comfortably and four is getting close. `--interval 15` divides it by three.

## What it reports

| Field | Source |
|---|---|
| `cpu_percent` | `/proc/stat`, idle vs total delta |
| `ram_percent` | `/proc/meminfo`, using `MemAvailable` |
| `storage_percent`, `storage_total_gb`, `storage_free_gb` | every real filesystem in `/proc/mounts` |
| `cpu_temp_c` | `/sys/class/thermal`, preferring a zone whose type names a CPU |
| `battery_*` | `/sys/class/power_supply` |
| `uptime_seconds` | `/proc/uptime` |
| `net_rx_kbps`, `net_tx_kbps` | `/proc/net/dev` |
| GPU, NPU, fan, disk temperature, your own | see *Extra sensors*. Plain keys or a `metrics` object, whichever PROTOCOL.md says the phone reads |
| `messages` | the outbox; see *Messages from other programs* |

**Storage is every disk, not just `/`.** A DietPi with a USB drive would
otherwise show a nearly full 4 GB card and never mention the 2 TB attached to
it. Only filesystems backed by a block device are counted, so tmpfs, overlay
and kernel views are excluded, and each device is counted once so a bind mount
is not billed twice. The numbers follow `df`: root's reserved 5 % is excluded
from both the total and the free figure, which keeps the percentage, the total
and the free space agreeing with each other and with the tool you would check
them against.

**Temperature picks a CPU zone.** It used to take whichever thermal zone came
first and read above zero, which on a laptop can be the battery bay or the wifi
card. Linux needs nothing installed for this; Windows is the awkward one.

**Battery is empty on a Pi**, which has neither a cell nor a mains supply that
announces itself, so it reports `-1` and the display leaves it out rather than
drawing a flat battery. On a laptop it is all there. Charging is the charging
state itself, not "plugged in": on mains at 100 % it reports on-AC and not
charging, because calling that *charging* is how a battery readout stops being
believed.

**Network counters skip the tunnels.** `lo`, `wg*`, `tailscale*`, `docker*` and
`veth*` are excluded, or the device's own polling traffic would appear in the
numbers it is displaying.

## Running it by hand

```bash
python3 peek-agent.py --pair-code K7M2-P4QX-9R
```

See what this machine reports, without pushing anything anywhere:

```bash
python3 peek-agent.py --once
```

Check a code without pushing anything:

```bash
python3 peek-agent.py --pair-code K7M2-P4QX-9R --verify
# e67e3b0d5b07d6ba
```

That is the stream id, and it is what to compare against the device's own
`[pair] code X -> stream Y` line on serial when the two will not meet.

Serve the JSON on `:6595` instead of pushing — for a device on the same LAN, or
just to look at it:

```bash
python3 peek-agent.py
curl http://localhost:6595/telemetry
```

**Pushing no longer opens a port as a side effect.** Once there is somewhere to
push, listening is opt-in via `--serve` (or `PEEK_SERVE=1`). An agent that
dials out has no reason to also accept connections from the whole LAN, and an
open port nobody asked for is the kind of default that is only noticed later.

Every setting has an environment variable, because the systemd unit passes no
flags: `PEEK_PAIR_CODE`, `PEEK_RELAY_BASE`, `PEEK_INTERVAL`, `PEEK_SERVE`, and for
the newer features `PEEK_ALERTS`, `PEEK_SENSORS`, `PEEK_NOTIFY`,
`PEEK_NOTIFY_PORT`, `PEEK_MESSAGE_TTL_HOURS`, `PEEK_EXTRAS_INTERVAL`,
`PEEK_ALERTS_FILE` and `PEEK_STATE_DIR`. `--once` also reports the extra sensors,
and creates nothing on disk.
`--push URL --token TOKEN` still drives the private and named-stream modes that
do not use pairing at all.

## What the installer does

1. Checks for root, systemd, Python 3.6+, and curl or wget — each failure names
   the fix rather than just the problem.
2. Downloads `peek-agent.py` to `/opt/peekesp/`, replacing the existing copy
   only once the download has succeeded, so a failed upgrade leaves the working
   one in place instead of a truncated file.
3. Validates the code by asking the agent, so the alphabet lives in exactly one
   place and cannot drift out of step with the firmware.
4. Installs the `peekesp` command to `/usr/local/bin`, after checking it
   parses. Creates a system account `peekesp` with no home and no shell.
5. Writes the code to `/etc/peekesp/agent.conf`, mode `0600`, root-owned.
   **The code is the credential** — the stream id and push token are derived
   from it, so anything that can read that file can push telemetry to your
   device. systemd reads it as root before dropping to the service account.
6. Installs the unit and starts it.
7. Waits, then reports whether the machine is *actually pushing* — not merely
   that a service was installed. A rejected push says so, and says that the
   usual cause is the wrong code.

8. Gives the service a state directory, `/var/lib/peekesp`, the one place it may
   write, and makes `/etc/peekesp` readable by the service group so it can read
   `alerts.json`. `agent.conf` stays `0600` root.

The service gives up everything it can: no capabilities, no new privileges,
read-only filesystem apart from that state directory, private `/tmp`. `ProtectHome` is deliberately **not** set,
which is worth knowing — it replaces `/home` with an empty mount, and since this
agent adds up every real filesystem, a machine with `/home` on its own partition
would silently stop counting it and report less disk than it has.