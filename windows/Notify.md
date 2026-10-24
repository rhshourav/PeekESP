# Sending a message to your phone from another program

Any program on this PC can put a message in front of your phone through the PeekESP
agent: a security tool, an antivirus hook, a backup script, a scheduled task. The agent
adds it to the reading it pushes to the relay, and the phone shows it in its Alerts tab
and notifies. What the phone does with it depends on the **severity**:

| `severity` | On the phone |
|---|---|
| `info` | Alerts tab. Quiet: no sound, no popup. |
| `warning` | Alerts tab. The phone's normal sound, and a popup. |
| `critical` | Alerts tab in red. **Rings like an alarm** on the alarm volume (so on silent too) until silenced or ten minutes pass, vibrates, shows on the lock screen. |

`red`, `high`, `alert`, `emergency`, `fatal` and `error` all mean `critical`. `yellow`,
`amber`, `orange`, `warn` and `medium` mean `warning`. Anything else is `info`, so a typo
fails quiet rather than loud.

## The fields

| Field | |
|---|---|
| `title` | Up to 80 characters. If missing, the start of `message` is used. |
| `message` (or `body`) | Up to 400 characters. At least one of `title` / `message` is needed. |
| `severity` | As above. Default `info`. |
| `source` | Who is speaking, up to 40 characters, shown on the notification. |
| `id` | Optional. **The same id twice counts once**, so a sender that retries cannot ring the phone twice. Without one, every call is a new message. Letters, digits and `. _ : @ -`, 44 at most. |
| `ts` | When it happened, seconds or milliseconds since 1970. Default: now. |

The agent puts this PC's name in front of your `id` before it reaches the phone, so two
PCs running the same tool cannot overwrite each other.

## Three ways in

### 1. The command line

```
peek-agent.exe notify --severity red --title "Intrusion" --message "New login from 203.0.113.9"
peek-agent.exe notify --severity warning --title "Backup failed" --id backup-2026-10-24
```

Needs no key. It writes a file into the outbox folder (route 3), so it works **even if the
agent is not running yet**: the message waits and goes out when the agent starts. Add
`--http` to post to the running agent instead, which uses the key from `config.json`.

### 2. HTTP, for a program that would rather POST

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:6598/notify `
  -Headers @{ Authorization = "Bearer <key>" } -ContentType application/json `
  -Body '{"title":"Intrusion","message":"New login","severity":"red","id":"fw-2291"}'
```

```
curl.exe -X POST http://127.0.0.1:6598/notify -H "Authorization: Bearer <key>" ^
  -H "Content-Type: application/json" ^
  -d "{\"title\":\"Intrusion\",\"severity\":\"red\"}"
```

```python
import json, urllib.request
req = urllib.request.Request(
    "http://127.0.0.1:6598/notify", method="POST",
    data=json.dumps({"title": "Intrusion", "severity": "red", "id": "fw-2291"}).encode(),
    headers={"Authorization": "Bearer <key>", "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=5).read())
```

The body is one message, a list of up to ten, or `{"messages": [...]}`. The key is under
**Settings → Alerts, sensors and messages → Messages**, where **New key** replaces it and
**Copy PowerShell** copies a ready command. `X-Peek-Key: <key>` works in place of the
`Authorization` header.

| Reply | Meaning |
|---|---|
| `200` `{"ok":true,"accepted":1,"ids":[...]}` | Queued. |
| `400` | Not JSON, or there was no title or message to send. |
| `401` | Wrong or missing key. Nothing else is revealed. |
| `403` | Wrong `Host`, or an `Origin` header (see below). |
| `404` | Any path but `/notify`. |
| `413` | Over 16 KB. |
| `429` | More than 20 messages in a minute, across every program. |

### 3. A watched folder

Drop a `.json` file in `%APPDATA%\PeekESP\outbox\` (the **Open outbox folder** button). It
is picked up within a couple of seconds and deleted. The content is any of the shapes
above. Write it under another name and rename it into place if you can; a file still being
written is given a few seconds before it is called broken. Bad files move to `outbox\rejected\`.

This is the route for a tool that can only write files, and the only one that needs no key.

## What keeps this safe

* **It listens on 127.0.0.1 only.** Nothing off this PC can reach it. The key keeps other
  programs on this PC out, and the key sits in `%APPDATA%`, readable by your account.
* **A web page cannot use it.** A browser will POST to `localhost` for any page, but cannot
  add an `Authorization` header without a preflight the agent never answers, and a page that
  rebinds a DNS name to 127.0.0.1 arrives with the wrong `Host`. Both are refused before the
  body is read, as is any request carrying an `Origin`.
* **There is no way to flood the phone.** 20 outside messages a minute, ten in a reading.
* **A key only lets a program post a message.** It cannot read anything. Treat it as a
  secret anyway, and use **New key** if one leaks.

## What to know before relying on it

* **The agent has to be running.** The tray app or `peek-agent.exe --config`. A message
  queued while it is stopped goes out on the next start, but a PC that is off cannot ring
  anything, and this is not a substitute for the phone noticing the PC went quiet.
* **A service running as SYSTEM has a different `%APPDATA%`** from yours, so its outbox
  folder and `config.json` are not the ones your `notify` call sees. Use `--http` (the
  endpoint is on the PC, whoever runs it) or `--outbox <that folder>`.
* **Messages travel through your relay**, inside the reading, and the relay operator can
  read them. Do not send a password or a one-time code. They are *not* served on the LAN
  endpoint of `serve` mode.
* **Each message is re-sent for six hours** (**Alerts → Keep each message**), because the
  phone only looks every fifteen minutes with the app closed. The `id` stops the repeats
  counting twice. A message more than six hours old when the phone first sees it is filed
  quietly, so a backlog never rings it.
* **At most ten messages ride in one reading**, the most severe first, then the newest.
  The rest wait and are never lost, unless they age out before there is room.
* **The relay has to pass `messages` and `metrics` through untouched.** If it copies a fixed
  list of fields, add them there, or nothing here reaches the phone. A fast check: send a
  message and read `/telemetry/<stream>` with the read token.