# PeekESP: sensors, messages and notifications

This describes what the Android app understands beyond the standard reading, so that **a machine, its
agent, or any security tool running on or near it** can put more in front of you. Every addition is
optional and ignored by anything that does not know it, so old agents and devices keep working.

> **One thing to check on your side.** The Android app reads each machine's reading from the relay as
> JSON. For the additions below to arrive, the relay (the Worker) must pass `messages`, `metrics` and
> unknown measurement keys through untouched. If it only copies a fixed list of fields, add these.

---

## 1. Extra sensors (GPU, NPU, fans, power, anything)

Put them in the reading. Either form works; use whichever is easier for the sender.

**Plain keys**, if the name ends the way a measurement does:

```json
{ "host": "studio", "gpu_percent": 42.5, "gpu_temp_c": 61, "npu_percent": 7, "fan_rpm": 1200, "power_w": 148 }
```

**A `metrics` object**, for anything that needs its own name or unit:

```json
{ "host": "studio",
  "metrics": {
    "vram_gb": 8.5,
    "gpu": { "value": 42, "label": "Graphics", "unit": "%", "max": 100 }
  } }
```

| Key ends with | Unit | | Key ends with | Unit |
|---|---|---|---|---|
| `_percent`, `_pct` | % | | `_mb`, `_gb` | MB, GB |
| `_c` | °C | | `_kbps`, `_mbps` | kbps, Mbps |
| `_w` | W | | `_seconds`, `_ms` | s, ms |
| `_mhz`, `_ghz` | MHz, GHz | | `_v`, `_a` | V, A |
| `_rpm` | rpm | | | |

Rules, the same as for every other sensor:

- A **negative number means "this machine has no such sensor"**. It is skipped, never shown as zero.
- Keys are lower-case `a-z`, `0-9`, `_`, at most 32 characters. A plain key with no recognised ending is
  ignored (so relay bookkeeping never turns into a sensor); inside `metrics` any key is accepted.
- At most 24 extra sensors per machine.
- The names the app already knows (`cpu_percent`, `ram_percent`, `cpu_temp_c`, ...) are not repeated here.

Each extra sensor gets its own history chart on the Dashboard and can be watched by an alert rule.
`gpu_percent` is shown as "GPU", `gpu_temp_c` as "GPU temperature", `npu_percent` as "NPU"; anything else
is named from its key (`custom_sensor_w` becomes "Custom Sensor"), or from the `label` you give it.

---

## 2. Messages from a machine

A machine (or a tool on it) can leave messages in its reading:

```json
{ "host": "studio",
  "messages": [
    { "id": "fw-2291", "severity": "critical", "title": "Intrusion detected",
      "body": "New login from 203.0.113.9", "source": "Defender", "ts": 1760000000 } ] }
```

| Field | Notes |
|---|---|
| `id` | Identifies the message. **Send the same id every time you re-send it** and it counts once. Optional; if missing, one is made from the content. |
| `severity` | `info`, `warning` or `critical`. Also understood: `red`, `high`, `alert`, `emergency`, `fatal`, `error` (critical); `yellow`, `amber`, `orange`, `warn`, `medium` (warning). Anything else is `info`. |
| `title` | Up to 80 characters. If missing, the start of `body` is used. |
| `body` (or `message`) | Up to 400 characters. |
| `source` | Who is speaking, up to 40 characters. Shown on the notification. Default `machine`. |
| `ts` | When it happened, in seconds or milliseconds since 1970. Default: when the phone first saw it. |

Up to 10 messages per reading are read.

**A reading is "the latest state", not a queue.** The phone only looks every few seconds with the app
open, and about every 15 minutes with it closed, so a message sent once and then dropped from the next
reading can be missed. Keep each message in `messages` for **at least an hour** (six is better, to cover
a phone that was offline). Re-sending is safe: the `id` stops it becoming a second alert. A message
more than six hours old when the phone first sees it is filed in the inbox quietly, so a backlog
never rings the phone.

### What the phone does with a message

| `severity` | In the app | Notification |
|---|---|---|
| `info` | Alerts tab | Quiet: no sound, no popup |
| `warning` | Alerts tab | The phone's normal sound, and a popup |
| `critical` | Alerts tab, in red, with a Silence button | **Rings like an alarm**, on the alarm volume (so on silent too), until silenced or ten minutes pass; vibrates; shows on the lock screen |

Quiet hours hold back everything except critical (if the user allows it). Muting a machine keeps its
messages in the Alerts tab and never notifies.

### For a security tool on the machine

The cleanest way for another program on a machine to reach the phone is through the machine's PeekESP
agent: the agent keeps an outbox, and every push carries the outbox's recent entries as `messages`.

**The Windows and Linux agents provide this** (`peek-agent.exe notify` / `peekesp notify`, a watched
folder, and a loopback HTTP endpoint; see `windows/NOTIFY.md` and `dietpi/NOTIFY.md`). It re-sends each message in every reading for six hours by default, so
a phone that was offline can still catch up, and it prefixes each `id` with the machine name so two machines
running the same tool cannot collide. Other agents are free to do the same; this file only fixes what the
phone understands once the message arrives.

---

## 3. Other apps on the phone

A security or automation app *on the phone* (Tasker, a script, another tool) can post a message through
PeekESP with a broadcast. The user turns this on under **Configure > Alerts > Other apps**, which
makes a key. Without the key, nothing is accepted.

| Extra | |
|---|---|
| `token` | **Required.** The key from the app. |
| `title`, `message` | At least one. 80 and 400 characters. |
| `severity` | As above. |
| `source` | Shown as who sent it. Default "External app". |
| `machine` | Optional: ties it to a machine, so muting that machine mutes it. |
| `id` | Optional. The same id twice counts once, so a sender can retry safely. |

```
adb shell am broadcast \
  -a com.rhshourav.peekesp.action.SEND_MESSAGE \
  -p com.rhshourav.peekesp \
  --es token <KEY> --es severity critical \
  --es title "Intrusion" --es message "New login"
```

```kotlin
// From another Android app
context.sendBroadcast(
    Intent("com.rhshourav.peekesp.action.SEND_MESSAGE")
        .setPackage("com.rhshourav.peekesp")      // required: Android will not deliver an unaddressed broadcast
        .putExtra("token", key)
        .putExtra("severity", "critical")
        .putExtra("title", "Intrusion")
        .putExtra("message", "New login")
)
```

- The receiver gives **no reply on any failure**, so a refused sender learns nothing about why.
- At most 20 messages a minute across all apps.
- The key only lets an app *post a message on this phone*. It cannot read anything. Treat it as a secret anyway,
  and use **New key** if one leaks.

---

## 4. What the app alerts on by itself

- **A machine goes offline / comes back.** Offline means the relay answered and reported the machine
  silent for the chosen time (default 2 minutes).
- **A rule trips.** CPU, memory, temperature, storage, battery while unplugged, network, and any extra
  sensor, with a limit, a duration, a severity, optional wording of your own, and optionally one machine only.
- **Unusually high.** Judged against that machine's own last 24 hours (median and median absolute
  deviation, so one spike cannot stretch its own yardstick). Needs history logging on and about six hours of data.

**Phone offline is not machine offline.** Alerts are only evaluated on a reading the relay actually
answered with. When the phone has no internet, nothing is evaluated, so a dead phone signal can never
be reported as a machine going down. The same distinction is drawn on every chart.

### How quickly

| App state | Alert arrives |
|---|---|
| Open | Within seconds (the app asks every 5 seconds) |
| Closed | Within about 15 minutes: the fastest Android allows background work |

Faster than that needs the relay to push to the phone (for example through Firebase Cloud Messaging),
which is outside what the app can do alone.

---

## 5. A complete reading

```json
{
  "host": "studio",
  "age_s": 3,
  "cpu_percent": 31.2, "ram_percent": 58, "cpu_temp_c": 54, "storage_percent": 61,
  "net_rx_kbps": 880, "net_tx_kbps": 112, "uptime_seconds": 86400,
  "battery_percent": 64, "battery_charging": true, "battery_ac": true,
  "gpu_percent": 42.5, "gpu_temp_c": 61, "npu_percent": 7,
  "metrics": { "vram_gb": 8.5 },
  "messages": [
    { "id": "fw-2291", "severity": "warning", "title": "Disk is noisy", "source": "SMART", "ts": 1760000000 }
  ]
}
```