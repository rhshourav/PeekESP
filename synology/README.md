# PeekESP agent for Synology (DSM 7.x)

Puts a Synology NAS on a PeekESP display and on your phone, and tells you the one
thing only a NAS can tell you: **a RAID array has lost a drive.**

Same JSON contract as the [Windows agent](../windows/README.md) and the DietPi one.
It is a single Python file using only the standard library, because there is no pip
worth trusting on a NAS, and it must never be the reason your disks stay awake.

**Project site:** <https://rhshourav.github.io/PeekESP/> &nbsp;·&nbsp;
**Repo:** <https://github.com/rhshourav/PeekESP> &nbsp;·&nbsp;
**Discord:** `rhshourav`

## Two outputs, two switches

Each one is independent. Turn either off and the other keeps going.

| | **Widget feed** | **Relay push** |
|---|---|---|
| What it is | `GET http://<nas>:8080/telemetry` on your LAN | `POST` to the PeekESP relay every few seconds |
| Who reads it | The Android widget, or anything that can fetch a URL | The ESP32 display and the phone app, from anywhere |
| Needs the internet | No | Yes (outbound only; no port forward, no VPN) |
| Needs pairing | No | Yes |
| Switch | `peekesp serve on\|off` | `peekesp push on\|off` |
| Default | on, **private networks only** | on, once paired |

```
peekesp push off      # stop sending to the relay; the widget feed carries on
peekesp serve off     # stop answering on the LAN; the relay push carries on
```

Both take effect within a second, without restarting anything.

## Quick start

**1. Turn on SSH.** DSM → Control Panel → Terminal & SNMP → Terminal → *Enable SSH
service*. DSM 7 only lets members of the *administrators* group in, and has root
login disabled, so log in as your admin account and become root:

```
ssh youradmin@<nas-ip>
sudo -i
```

**2. Install.** The code is the one on the device's screen (`K7M2-P4QX-9R`). Leave
`--code` off to be asked, or to pair later.

```
curl -fsSL https://raw.githubusercontent.com/rhshourav/PeekESP/main/synology/install.sh | sh -s -- --code K7M2-P4QX-9R
```

(You are already root after `sudo -i`; otherwise put `sudo` before `sh`.) Prefer to
read it first? Download the `synology/` folder, `cd` into it and run
`sh install.sh --code K7M2-P4QX-9R` as root. That copy uses the files beside it and
touches the network only to reach the relay.

It installs to `/usr/local/peekesp/`, links `peekesp` into `/usr/local/bin`, starts
the agent, and then **waits for the running agent to report a successful push**
before it says it worked. That check goes through the same process and user that
will run after a reboot, which a one-off test would not. A sleeping relay can take up
to a minute to answer the first request; the installer waits for it.

**3. Make it survive a reboot.** One minute, once:

> DSM → Control Panel → **Task Scheduler** → Create → **Triggered Task** →
> User-defined script
> - General: Task `PeekESP`, User `root`, Event **Boot-up**, Enabled
> - Task Settings → User-defined script: `/usr/local/peekesp/peekesp start --quiet`

Why this is a manual step is in [Autostart](#autostart-and-why-there-is-a-supervisor).

## Everyday commands

You do not need to type `sudo`; `peekesp` asks for it when it needs it. (If you type
`sudo peekesp …` and DSM says *command not found*, DSM's sudo is leaving
`/usr/local/bin` off its PATH. Drop the `sudo`, or use the full path.)

```
peekesp status                 is it running, what it is sending, what it found
peekesp logs [-f]              recent log (-f follows)
peekesp test                   one reading and one real push; does not touch the running agent
peekesp doctor                 what this NAS can report, and from where
peekesp show                   settings, with secrets hidden

peekesp pair CODE              pair with the code on the device's screen
peekesp push  on|off           the relay push
peekesp serve on|off           the widget feed
peekesp set NAME VALUE         interval  port  bind  allow  name  relay  volumes
                               drive-temp  drive-interval  ups  messages  ttl-hours
                               raid-system  tls-verify

peekesp start | stop | restart
peekesp autostart [on|off]     the rc.d hook, and what to add to Task Scheduler
peekesp update                 latest agent + installer, keeping your settings
peekesp uninstall [--purge]    --purge also removes your pairing
```

`peekesp status` looks like this (a healthy NAS, then the same NAS mid-rebuild):

```
PeekESP agent 2.0.1   host: DiskStation   up 3 d 4 h
  relay push    ON   ok - last push 2 s ago - stream 4b907ba136d0a7f2
  widget feed   ON   listening on 0.0.0.0:8080
  reading       CPU 4%  RAM 38%  storage 61% (4.2 TB free)  temp 47°  RX 56K TX 4K
  RAID          OK   (md2 raid5 OK 4/4)
  drives        4 found, 2 asleep (left alone), hottest 41°

  RAID          REBUILDING   (md2 raid5 REBUILDING 3/4)
  messages      1 in the readings (phone alerts)
```

## The widget feed, and who can reach it

The endpoint has no password, like the Windows agent's serve mode. A NAS is exactly
the kind of box that ends up port-forwarded by accident, so it does two things to be
a safe neighbour:

* **It answers private networks only** by default: `10/8`, `172.16/12`, `192.168/16`,
  link-local, loopback, and `100.64/10` (that is where Tailscale lives, so a NAS
  reached over a tailnet works). Anyone else gets `403`.
* **It never carries messages.** "RAID degraded" is for your phone, not for whoever
  is on the guest Wi-Fi. Numbers only.

```
peekesp set allow 192.168.1.0/24          only that subnet
peekesp set allow private,203.0.113.7     private plus one address (comma separated)
peekesp set allow any                     everyone - say so on purpose, never by accident
peekesp set bind 192.168.1.10             listen on one address instead of all of them
peekesp set port 8090                     if something else already owns 8080
```

If DSM's firewall is on, allow TCP 8080 from your LAN (Control Panel → Security →
Firewall). Pushing to the relay needs only outbound traffic.

A port that is already taken does **not** take the push down with it. The widget feed
reports `error - Address already in use` in `peekesp status`, retries every 30
seconds, and the relay push carries on regardless.

## What it reports, and why it is read that way

Everything comes from `/proc`, `/sys` or one small subprocess. These are the choices
that make the numbers mean what a NAS owner thinks they mean:

| Field | Source | The decision |
|---|---|---|
| `cpu_percent` | `/proc/stat`, delta between samples | **iowait counts as idle.** A NAS waits on disks a lot, and a CPU that is waiting is not working: counting it busy makes a RAID rebuild read as 90% CPU on a box doing almost nothing. Guest time is not added again (the kernel already folded it into user). |
| `ram_percent` | `/proc/meminfo` | **`MemAvailable`, not `MemFree`.** DSM's Btrfs cache is greedy; "total − free" reads 90% on an idle NAS. |
| `storage_percent`, `_total_gb`, `_free_gb` | `statvfs` on `/volume1`, `/volume2`, … | **Only `/volumeN`, and nothing under it.** That one pattern keeps out `/` (DSM's small system partition, always nearly full), Docker's overlays, Btrfs subvolumes, `@sharesnap` snapshots, USB shares and network mounts. df-style: free is what a user can still write. |
| `cpu_temp_c` | hwmon (`coretemp`, `k10temp`, …), then thermal zones | Package sensor first, hottest core next, an ACPI zone last (it is wherever the board maker put it, and on some boards a constant). A drive, chipset or Wi-Fi sensor is **never** reported as the CPU. `-1` if the model exposes none. |
| `net_rx_kbps`, `_tx_kbps` | `/proc/net/dev` | **`eth*` and `bond*` only, with a bond's slaves dropped** — they carry the same bytes as the bond, so summing both doubles the number. `ovs_eth0` (Virtual Machine Manager) is excluded for the same reason. KiB/s, the unit the Windows agent calls "kbps". |
| `battery_*` | `upsc`, if a UPS is enabled in DSM | `-1` (no battery) on a NAS without one. With one, the phone's existing "battery low while unplugged" rule works on your NAS for free. |
| `uptime_seconds` | `/proc/uptime` | |
| `raid_degraded`, `raid_status` | `/proc/mdstat` | See [RAID](#raid-and-the-alarm). |
| `metrics.drive_temp_c` | `smartctl -n standby` | See [Hibernation](#drive-temperatures-without-waking-your-drives). |
| `metrics.volumeN_percent` | per volume, **only when you have two or more** | Aggregated, a nearly full SSD volume hides behind a roomy HDD one. |
| `messages` | RAID and SMART faults | Relay only. See [RAID](#raid-and-the-alarm). |

`peekesp doctor` prints, for *your* NAS, what it found and which sensor it used for
each number. It is the thing to paste into an issue.

## Drive temperatures without waking your drives

Drive hibernation only works if nothing touches the disks, so this is the part that
had to be right.

* Drive temperature is read with `smartctl -n standby -H -A /dev/<drive>`. `-n
  standby` makes smartctl ask the drive its power mode first, which does not spin it
  up, and stop if the drive is asleep. **A sleeping drive is skipped, never woken.**
  An awake drive answers a SMART read in milliseconds and neither spins down nor up
  because of it.
* The exit code cannot tell "asleep" from "could not open the device" (both are
  status 2), so the agent reads the message, `Device is in STANDBY mode`, instead.
* It runs in its **own thread, one drive at a time, a second apart, every five
  minutes** — never in the path of a reading. A wedged drive can make that one number
  stale; it cannot stop the push or the widget feed.
* A sleeping drive's last temperature is kept, then **expires** (about 16 minutes at
  the default) rather than being reported forever. A box whose disks have all been
  asleep for an hour reports no drive temperature, not a stale one.
* Volume usage uses `statvfs`, which is answered from memory. CPU, RAM, network and
  RAID read `/proc`, which is RAM.
* **Nothing is written to a volume.** The status file and log are in `/var/run`
  (RAM). Settings live on DSM's system partition and are written only when you change
  one. The log is capped at 128 KB and records transitions, not readings.
* `smartctl` needs root. As a normal user the drive temperature is simply unavailable
  and `peekesp doctor` says why.
* `peekesp set drive-temp off` removes every `smartctl` call.

What this does **not** prove: that your particular drives, controller and DSM release
all honour `-n standby`. SATA does. SAS behaves differently across vendors. Check
yours once — see *Verifying hibernation* below.

**Verifying hibernation.** Let the NAS idle past your hibernation timeout, run
`peekesp doctor`, and look for `asleep - left alone` against each drive. Then watch
DSM's disk-state indicator while the agent runs normally. If a drive that should be
asleep keeps waking, `peekesp set drive-temp off` and see whether it stops; if it
does, please open an issue with your model and `peekesp doctor` output.

The drive listing reads `/sys/block`. It skips USB-attached drives, empty bays and the
DSM boot device. `peekesp doctor` also lists whatever is in `/run/synostorage/disks/`
without reading it — that layout differs between DSM releases and I would rather
show you than guess.

## RAID and the alarm

`/proc/mdstat` is parsed per array into one of: `OK`, `SYNCING` (a scheduled check on
a healthy array), `REBUILDING`, `DEGRADED`, `FAILED` (the array is not running).

* `raid_degraded` is **true** for `DEGRADED`, `REBUILDING` and `FAILED`. A rebuild
  still leaves you unprotected until it finishes. A scheduled consistency check is
  **not** an alarm.
* **`md0` and `md1` do not trip the alarm.** DSM builds those (the DSM system
  partition and swap) across *every* drive in the box, including one that belongs to
  no storage pool. Pull a drive from a spare bay and they go `[4/3]` while your data
  is untouched. A critical alarm for that teaches you to ignore the alarm. A drive
  that is really failing takes its slice of the *data* array with it, and that array
  does trip it. They are still shown by `peekesp doctor`;
  `peekesp set raid-system on` counts them anyway.
* This is md-level, which is what DSM means by *Degraded* for RAID and SHR. It does
  not see Btrfs scrub errors or a drive's SMART *warnings* (it does catch SMART
  *FAILED*, below).

### On the phone

The Android app already reads `messages` (PROTOCOL.md §2), so **no app change is
needed**. While a problem lasts, the reading carries one critical message with a
stable id, so the phone counts it once and **rings like an alarm**. When it clears,
an info message says so and stays for `ttl-hours` (default 6), so a phone that was
off can still see how it ended.

Three things keep that from waking you for nothing:

1. A problem must **hold for 60 seconds** before it is raised, and be gone for 60
   before it is cleared. Arrays are still assembling for a moment at boot; that is
   not an incident.
2. The message id includes **when the problem began**, so a second failure next month
   is a new alert, while a restart of the agent in the middle of one is not. Open
   incidents are kept in `state.json`, written only when one starts or ends.
3. A failing drive (SMART overall health `FAILED`) is raised the same way.

CPU, RAM and temperature thresholds are rules you set on the phone (Configure →
Alerts). This agent raises only what the phone cannot know.

Relay only: messages travel through your relay, inside the reading, and the relay
operator can read them. Nothing sensitive is in them — array names and drive names.

### On the device

`raid_degraded` is an ordinary key in the JSON. To make the ESP32 show it, apply
[`firmware-raid-warning.patch`](firmware-raid-warning.patch) (from the repo root:
`git apply synology/firmware-raid-warning.patch`) and reflash. It is 29 added lines.
When a NAS reports `raid_degraded: true`, the storage bar turns solid red and its
caption reads `RAID DEGRADED`. Every other host omits the key, which reads as false,
so nothing changes for them.

The Android *widget* will ignore `raid_*` until the app learns them; it gets the
alarm through the phone alert above in the meantime.

## Autostart, and why there is a supervisor

You asked why this does not install a service. DSM 6 used upstart; DSM 7 moved to a
systemd-based init internally. But it is Synology's private one: rebuilt by every
DSM update, and a unit dropped into it by hand is neither supported nor reliably
still there after the next update. The supported ways to run your own code are
**Task Scheduler** and packaging an SPK.

Task Scheduler only *starts* things; nothing restarts a process that dies. So
`peekesp start` launches a small supervisor that runs the agent and restarts it with
a growing delay (2 s up to 60 s) if it ever exits. Task Scheduler then only has to
start the supervisor at boot.

* `start` is **safe to run any number of times.** A second one finds the first and
  leaves. Two started at the same instant produce exactly one supervisor, and the
  agent takes a kernel lock of its own as a second layer, so there can never be two
  agents.
* If the supervisor itself is killed, the next `peekesp start` finds the orphaned
  agent and replaces it.
* The installer also writes an rc.d hook (`/usr/local/etc/rc.d/S99peekesp.sh`).
  **I cannot tell you that DSM 7 runs it** — it is not a documented hook there.
  It is harmless alongside Task Scheduler, because `start` is idempotent. Treat the
  Task Scheduler entry as the real mechanism.
* **A DSM update can wipe `/usr/local`.** If the agent is gone after an update, the
  Task Scheduler entry will fail silently. Re-run the installer; it keeps your
  pairing if the settings survived.
* Optional self-heal: add a second, *Scheduled* task every 10 minutes running the
  same `start --quiet`. It is idempotent, so it costs nothing, and it covers a
  killed supervisor.
* `peekesp status` says whether it can find a Task Scheduler entry. The way it looks
  (`synoschedtask --get`, grepped for `peekesp`) depends on an undocumented output
  format, so *"cannot tell from here"* is a possible and honest answer.

I did not automate creating the Task Scheduler entry. DSM has no supported command
for it, and the alternative is editing its internal database, which is how you end
up with a NAS that no longer boots cleanly.

## Container Manager path

For people who would rather install nothing on the NAS itself, or whose DSM has no
usable Python 3. [`Dockerfile`](Dockerfile) and
[`docker-compose.yml`](docker-compose.yml): `python:3.12-alpine`, the same agent file,
byte for byte.

Container Manager → Project → Create → point it at this folder, put your pairing code
in `PEEK_PAIR`, start. Or: `sudo docker compose up -d --build`.

* Host networking, so the NAS's own interfaces are visible and the widget feed is on
  the NAS's own address. There is no `ports:` section; the port is the real port.
* Each volume is mounted **read-only at its own path** (`/volume1:/volume1:ro`). That
  is how the agent finds it — it looks for `/volumeN` in its own mount table, and a
  bind mount puts one there.
* **`/proc` and `/sys` are deliberately not mounted**, though you might expect them
  to be. The container's own `/proc` already shows the host's CPU, memory, uptime and
  RAID, and with host networking its interfaces; its `/sys` shows the host's
  sensors. A second copy of `/proc` adds nothing and breaks `/proc/self`.
* Runs as `nobody`, read-only root, all capabilities dropped, 96 MB limit, status in
  a tmpfs, log size capped. Configuration is by `PEEK_*` environment variables;
  settings that need to persist go in the `/data` volume.
* The container's `HEALTHCHECK` runs `--healthcheck`, so Container Manager shows
  *healthy* only when the agent is alive and both enabled outputs are working.
* **Limits:** it cannot see DSM's UPS service, and drive temperatures need
  `WITH_SMART: "1"`, `user: root`, `SYS_RAWIO` and each drive mapped in — all in the
  commented block of the compose file. The same `-n standby` guarantee applies.

## Settings

`/usr/local/etc/peekesp/config.json`, mode `0600`. Editing it by hand is supported:
the agent notices within a few seconds. A mistake is reported by name, and that one
value falls back to its default — it does not stop the agent or its neighbours.
Anything you can `peekesp set`, you can edit here.

| Key | Default | Meaning |
|---|---|---|
| `push` | `true` | The relay push switch |
| `serve` | `true` | The widget feed switch |
| `pair_code` | `""` | Set by `peekesp pair`; the stream and token are derived from it |
| `relay_base` | the PeekESP relay | Change only to use your own relay (`set relay URL`) |
| `relay_url`, `token` | `""` | A named or private stream by hand; wins over pairing |
| `interval` | `5` | Seconds between pushes (1–600) |
| `name` | `""` | Name on the display; blank is the DSM hostname. ≤ 19 characters |
| `serve_port` / `serve_bind` | `8080` / `0.0.0.0` | Where the widget feed listens |
| `serve_allow` | `["private"]` | `private`, `any`, or networks |
| `volumes` | `[]` | Empty = every `/volumeN`. Set it if yours are mounted elsewhere |
| `drive_temp` / `drive_interval` | `true` / `300` | Drive temperatures and how often (30–3600 s) |
| `ups` | `auto` | `auto`, `on` or `off` |
| `messages` | `true` | Raise RAID/SMART faults as phone messages |
| `message_ttl_hours` | `6` | How long an all-clear stays in the readings |
| `raid_system` | `false` | Count `md0`/`md1` toward the alarm |
| `tls_verify` | `true` | See troubleshooting before turning this off |

Environment variables (`PEEK_PAIR`, `PEEK_PUSH`, `PEEK_SERVE`, `PEEK_PORT`,
`PEEK_ALLOW`, `PEEK_INTERVAL`, `PEEK_NAME`, `PEEK_RELAY`, `PEEK_VOLUMES`,
`PEEK_DRIVE_TEMP`, `PEEK_UPS`, `PEEK_MESSAGES`, `PEEK_TLS_VERIFY`) override the file
and are never written back to it. That is how the container is configured.

## Troubleshooting

| You see | It means | Do this |
|---|---|---|
| `peekesp: command not found` | `/usr/local/bin` is not on your PATH | `/usr/local/bin/peekesp status` |
| `sudo: peekesp: command not found` | DSM's sudo has its own PATH | Run `peekesp …` without `sudo` |
| Installer: *No usable Python 3.7+* | DSM's Python is missing or has no `ssl` | Install **Python 3** from Package Center, or use the container path |
| `relay push … HTTP 401 token rejected` | Wrong pairing code | `peekesp pair CODE`, reading the code off the device again. There is no `0`, `1`, `I` or `O` |
| `relay push … HTTP 404` | Wrong URL or stream | You set `relay_url` by hand; `peekesp set relay …` or re-pair |
| First push times out | A sleeping relay can take a minute to wake | Wait; the agent retries on its own |
| `error - TLS: …` | This Python cannot verify the relay's certificate | The agent already borrows `/etc/ssl/certs/ca-certificates.crt` if the default store is missing. As a last resort `peekesp set tls-verify off` — but then the relay's identity is **not checked** and the token travels in that connection |
| `widget feed … Address already in use` | Another package owns the port | `peekesp set port 8090` |
| Widget works from the NAS, not from the phone | DSM firewall, or your client is outside `allow` | Firewall rule for the port; a `403` means the allow-list |
| Storage `0%`, or `none found` in doctor | Volumes not mounted yet, or mounted somewhere unusual | `peekesp doctor`; `peekesp set volumes /your/mount` |
| Temperature `--` | The model exposes no CPU sensor | `peekesp doctor` lists what it looked for |
| `smartctl needs root` / `not found` | Run as a normal user, or no smartctl | `peekesp doctor` as root; otherwise drive temps are unavailable |
| `RAID DEGRADED` but DSM looks healthy | An array is genuinely short a member, or `raid-system` is on | `peekesp doctor` shows each array and `[n/m]`. `SYNCING` never alarms |
| No alert on the phone for a RAID fault | The 60 s hold, an old message, or `messages` off | A message over 6 hours old when the phone first sees it is filed quietly (PROTOCOL.md) |
| Agent gone after a reboot | No Task Scheduler entry | See *Make it survive a reboot* |
| Agent gone after a DSM update | The update cleared `/usr/local` | Re-run the installer |
| Disks stopped hibernating | Something touches the volumes, maybe not us | `peekesp stop`, wait, see; then `peekesp set drive-temp off` |

## What has and has not been tested

I would rather you read this than discover it.

**Verified**, on a generic Linux box with a fake `/proc` and `/sys`, a fake relay and
BusyBox as the shell:

* 84 unit tests: every parser against fixtures (mdstat (healthy, degraded,
  rebuilding, syncing, inactive, raid0, a failed member, a drive pulled from a spare
  bay); thermal; SMART for SATA, NVMe and
  SAS; standby detection); config validation; the allow-list; the RAID message
  lifecycle (blip, confirm, restart mid-incident, recover, next month); the pairing
  vector from the firmware (`K7M2P4QX9R` → `4b907ba136d0a7f2`); a contract test
  against the fields `parse_one()` reads; and an end-to-end run that flips both
  switches while the agent is live.
* The installer and `peekesp` under BusyBox `ash`: install, pair, upgrade over a
  running install, both switches, a `403` from the allow-list, `kill -9` on the agent
  and on the supervisor, eight supervisors started at once (exactly one survives),
  `stop`, uninstall with and without `--purge`, reinstall keeping the pairing.
* The agent as an unprivileged user (UID 65534) with environment-only configuration,
  and the container's own healthcheck command against it.
* `shellcheck`, and `vermin` (minimum Python 3.7; DSM 7 ships 3.8).
* The firmware patch applies cleanly with `patch` and `git apply`.

**Not verified, because I have no Synology here:**

* Anything on a real DiskStation. DSM's own Python and BusyBox may differ from what I
  tested against in ways I could not see.
* That `smartctl -n standby` leaves *your* drives asleep. It is the documented
  mechanism and works on SATA; I cannot show it on your controller.
* Whether DSM 7 runs the rc.d hook; what `synoschedtask --get` prints on your release;
  the contents of `/run/synostorage/disks/`.
* The Docker image build (there is no Docker in my environment). The compose file is
  checked against the agent's own parsers; the image itself has not been built.
* The firmware patch **compiles** — it uses only calls already used elsewhere in the
  sketch, but I have no ESP32 toolchain.
* How the Android widget consumes the LAN endpoint. It is the same contract as the
  Windows agent's serve mode.

If any of these go wrong on your NAS, `peekesp doctor` plus your DSM version and model
is exactly what I need.

## Files

| | |
|---|---|
| `peek-agent-synology.py` | The agent. One file, standard library only |
| `install.sh` | Installer, and (installed as `peekesp`) the command line |
| `Dockerfile`, `docker-compose.yml` | The Container Manager path |
| `firmware-raid-warning.patch` | ESP32: red bar and `RAID DEGRADED` |
| `test_synology.py` | `python3 -m unittest test_synology -v` |

---

PeekESP by **rhshourav** · Discord: `rhshourav` ·
[github.com/rhshourav/PeekESP](https://github.com/rhshourav/PeekESP) ·
[rhshourav.github.io/PeekESP](https://rhshourav.github.io/PeekESP/)
