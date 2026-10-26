# Sending a message to your phone from another program

Any program on this machine can put a message in front of your phone through the
PeekESP agent: a security tool, a backup script, a cron job, `fail2ban`. The agent adds
it to the reading it pushes to the relay, and the phone shows it in its Alerts tab and
notifies. What the phone does with it depends on the **severity**:

| `severity` | On the phone |
|---|---|
| `info` | Alerts tab. Quiet: no sound, no popup. |
| `warning` | Alerts tab. The phone's normal sound, and a popup. |
| `critical` | Alerts tab in red. **Rings like an alarm** on the alarm volume (so on silent too) until silenced or ten minutes pass, vibrates, shows on the lock screen. |

`red`, `high`, `alert`, `emergency`, `fatal` and `error` all mean `critical`. `yellow`,
`amber`, `orange`, `warn` and `medium` mean `warning`. Anything else is `info`, so a
typo fails quiet rather than loud.

## The fields

| Field | |
|---|---|
| `title` | Up to 80 characters. If missing, the start of `message` is used. |
| `message` (or `body`) | Up to 400 characters. At least one of `title` / `message` is needed. |
| `severity` | As above. Default `info`. |
| `source` | Who is speaking, up to 40 characters, shown on the notification. |
| `id` | Optional. **The same id twice counts once**, so a sender that retries cannot ring the phone twice. Without one, every call is a new message. Letters, digits and `. _ : @ -`, 44 at most. |
| `ts` | When it happened, seconds or milliseconds since 1970. Default: now. |

The agent puts this machine's name in front of your `id` before it reaches the phone,
so two machines running the same tool cannot overwrite each other.

## Three ways in

### 1. The command line

```bash
peekesp notify --severity red --title "Intrusion" --message "New login from 203.0.113.9"
peekesp notify --severity warning --title "Backup failed" --id backup-2026-10-24
```

Needs no key and no `sudo`, for root or for any member of the `peekesp` group:

```bash
sudo usermod -aG peekesp alice        # then alice logs in again
```

It writes a file into the drop folder (route 3), so it works **even if the agent is
not running yet**: the message waits and goes out when the agent starts. Add `--http`
to post to the running agent instead, which reads the key from
`/var/lib/peekesp/notify.key`.

### 2. HTTP, for a program that would rather POST

```bash
curl -s -X POST http://127.0.0.1:6598/notify \
  -H "Authorization: Bearer $(cat /var/lib/peekesp/notify.key)" \
  -H "Content-Type: application/json" \
  -d '{"title":"Intrusion","message":"New login","severity":"red","id":"fw-2291"}'
```

```python
import json, urllib.request
key = open("/var/lib/peekesp/notify.key").read().strip()
req = urllib.request.Request(
    "http://127.0.0.1:6598/notify", method="POST",
    data=json.dumps({"title": "Intrusion", "severity": "red", "id": "fw-2291"}).encode(),
    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=5).read())
```

The body is one message, a list of up to ten, or `{"messages": [...]}`.
`X-Peek-Key: <key>` works in place of the `Authorization` header. The key is made the
first time the agent starts and kept: root and members of the `peekesp` group can read
it, nobody else. To replace it, delete the file and `sudo peekesp restart`.

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

Drop a `.json` file in `/var/lib/peekesp/outbox/`. It is picked up within a couple of
seconds and deleted. The content is any of the shapes above. Write it under another
name and `mv` it into place if you can; a file still being written is given a few
seconds before it is called broken. Bad files move to `outbox/rejected/`.

The folder is `2770`, group `peekesp`: a member can write to it, and the file arrives in
the right group for the service to read. This is the route for a tool that can only
write files. **Membership of the group is as good as the key**: either lets a program
put a message in front of your phone, and neither lets it read anything.

## From the usual suspects (sketches: adapt them, they have not been run here)

```bash
# fail2ban: /etc/fail2ban/action.d/peekesp.conf
[Definition]
actionban = peekesp notify --severity warning --title "fail2ban: banned <ip>" --id f2b-<ip>

# cron: tell me when a job fails, not when it succeeds
0 3 * * * /usr/local/bin/backup.sh || peekesp notify --severity red --title "backup failed on $(hostname)"

# systemd: OnFailure=peekesp-notify@%n.service, with
#   ExecStart=/usr/local/bin/peekesp notify --severity red --title "%i failed"
```

## What keeps this safe

* **It listens on 127.0.0.1 only.** Nothing off this machine can reach it. The key keeps
  other programs on this machine out.
* **A web page cannot use it.** A browser will POST to `localhost` for any page, but
  cannot add an `Authorization` header without a preflight the agent never answers, and a
  page that rebinds a DNS name to 127.0.0.1 arrives with the wrong `Host`. Both are
  refused before the body is read, as is any request carrying an `Origin`.
* **There is no way to flood the phone.** 20 outside messages a minute, ten in a reading.
* **A key only lets a program post a message.** It cannot read anything. Treat it as a
  secret anyway.

## What to know before relying on it

* **The agent has to be running** for a message to leave this machine. One queued while it
  is stopped goes out on the next start. A machine that is off cannot ring anything, and
  this is not a substitute for the phone noticing it went quiet.
* **The service needs a state directory** (`/var/lib/peekesp`). Without one, messages live
  in memory only and this endpoint is off. `peekesp status` says so, and `sudo peekesp
  repair` fixes it.
* **Messages travel through your relay**, inside the reading, and the relay operator can
  read them. Do not send a password or a one-time code. They are *not* served on the LAN
  endpoint (`peekesp set serve on`).
* **Each message is re-sent for six hours** (`sudo peekesp set ttl 12`), because the phone
  only looks every fifteen minutes with the app closed. The `id` stops the repeats counting
  twice. A message more than six hours old when the phone first sees it is filed quietly,
  so a backlog never rings it.
* **At most ten messages ride in one reading**, the most severe first, then the newest. The
  rest wait and are never lost, unless they age out before there is room.
* **The relay has to pass `messages` and `metrics` through untouched.** If it copies a fixed
  list of fields, add them there, or nothing here reaches the phone. A fast check: send a
  message and read `/telemetry/<stream>` with the read token.