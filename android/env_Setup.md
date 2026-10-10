# PeekESP for Android

Monitor the machines behind your PeekESP pairing code from a phone, and keep one
of them on the home screen as a glass widget that looks like the device's own
screen.

Package `com.rhshourav.peekesp` · minimum Android 8.0 (API 26) · dark only ·
sideloaded (no Play listing).

**Status.** The project builds and its unit tests pass on Windows (JDK 21,
Gradle 8.11.1, Android Gradle Plugin 8.7.3). It has not yet been run on a phone,
so the widget's proportions and glass strength are unchecked. See
[What is and is not verified](#what-is-and-is-not-verified).

- [How the pieces fit](#how-the-pieces-fit)
- [Part 1: build toolchain on Windows](#part-1-build-toolchain-on-windows)
- [Part 2: build and test](#part-2-build-and-test)
- [Part 3: install on a phone](#part-3-install-on-a-phone)
- [Part 4: pair and add the widget](#part-4-pair-and-add-the-widget)
- [Part 5: the rest of the system](#part-5-the-rest-of-the-system)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Design decisions worth knowing](#design-decisions-worth-knowing)
- [What is and is not verified](#what-is-and-is-not-verified)
- [Roadmap](#roadmap)

---

## How the pieces fit

```
  agents (PCs, Pi, servers) ── POST /ingest/<stream> ───▶ ┌────────────────────┐
                               push token                 │  Cloudflare Worker │
                                                          │  + Durable Object  │
  ESP32 / Pi displays ─────── GET /telemetry/<stream> ──▶ │  one per stream    │
                               read token                 │                    │
  this app and its widget ─── GET /telemetry/<stream> ──▶ └────────────────────┘
                               read token
```

Everything dials out; nothing listens. A **pairing code** (ten characters, shown
on the device as `K7M2-P4QX-9R`) is the only thing you type anywhere. Every
party derives the same stream id and tokens from it locally, so the code itself
never leaves your devices:

```
stream = SHA-256("peek-stream:" + CODE)  first 16 hex
read   = SHA-256("peek-read:"   + CODE)  first 48 hex
```

This app derives the stream and the **read** token only, never the push token.
A read-only app cannot forge telemetry or queue commands.

---

## Part 1: build toolchain on Windows

You need three things: JDK 21, the Android SDK, and Gradle (once, to create the
wrapper). Everything below is PowerShell.

> **Keep the toolchain out of `AppData`.** The Microsoft Store build of Python
> silently redirects anything it writes under `AppData\Local` into a private
> sandbox that Gradle and the JDK cannot see. This guide uses
> `%USERPROFILE%\.peekesp-toolchain` for that reason.

### 1.1 JDK 21

Use 21 specifically. Gradle 8.11.1 cannot read the version string of JDK 25 and
fails with an error that is just `25.0.4.1`.

```powershell
winget install EclipseAdoptium.Temurin.21.JDK
Get-ChildItem "C:\Program Files\Eclipse Adoptium"      # note the folder name
```

Make it permanent (then open a **new** PowerShell window):

```powershell
$jdk = (Get-ChildItem "C:\Program Files\Eclipse Adoptium" -Directory |
        Where-Object Name -like "jdk-21*" | Select-Object -First 1).FullName
[Environment]::SetEnvironmentVariable("JAVA_HOME", $jdk, "User")
```

`JAVA_HOME` is what Gradle uses. `java -version` may still print a newer JDK if
one is earlier on your `PATH`; that does not matter. You will check the JDK
Gradle actually uses in Part 2.

### 1.2 Android SDK (command-line tools)

You do not need Android Studio.

1. Open <https://developer.android.com/studio>, scroll to **Command line tools
   only**, and download the Windows zip. Leave it in `Downloads`.
2. Unpack it into a folder named exactly `latest`; the SDK tools refuse to work
   from any other name.

```powershell
$sdk = "$env:USERPROFILE\.peekesp-toolchain\android-sdk"
New-Item -ItemType Directory -Force "$sdk\cmdline-tools" | Out-Null

$zip = Get-ChildItem "$env:USERPROFILE\Downloads" -Filter "commandlinetools-win-*.zip" |
       Select-Object -First 1
if (-not $zip) { throw "Download the 'Command line tools only' zip first." }

Expand-Archive $zip.FullName -DestinationPath "$sdk\cmdline-tools" -Force
Rename-Item "$sdk\cmdline-tools\cmdline-tools" latest

Test-Path "$sdk\cmdline-tools\latest\bin\sdkmanager.bat"      # must print True
```

If you already unzipped it somewhere (say `C:\cmdline-tools`) and `bin` sits
directly inside that folder, copy it into place instead of the unzip and rename:

```powershell
Copy-Item C:\cmdline-tools "$sdk\cmdline-tools\latest" -Recurse
```

Accept the licences and install the pieces the build needs:

```powershell
& "$sdk\cmdline-tools\latest\bin\sdkmanager.bat" --licenses
& "$sdk\cmdline-tools\latest\bin\sdkmanager.bat" "platform-tools" "platforms;android-35" "build-tools;34.0.0"
```

Answer `y` to every licence prompt.

### 1.3 Tell the project where the SDK is

From the `android` folder (the one containing `settings.gradle.kts`):

```powershell
"sdk.dir=$($sdk -replace '\\','/')" | Set-Content local.properties -Encoding ascii
```

`local.properties` is git-ignored. It holds a path on your machine and must not
be committed.

### 1.4 Gradle, once, to create the wrapper

The project ships without the Gradle wrapper, because generating it needs a
download. You need any Gradle 8.x on your `PATH` for this one step: unzip
`gradle-8.11.1-bin.zip` from <https://gradle.org/releases/> and add its `bin`
folder to `PATH`. Then, from the `android` folder:

```powershell
gradle --stop
gradle wrapper --gradle-version 8.11.1
```

This creates `gradlew`, `gradlew.bat` and `gradle\wrapper\`. **Commit them.**
From then on everyone, and CI, uses `.\gradlew.bat` and never needs a system
Gradle.

---

## Part 2: build and test

From the `android` folder:

```powershell
.\gradlew.bat --version        # "Launcher JVM" must say 21, not 25
.\gradlew.bat test
```

The first run downloads the Android Gradle Plugin, Compose, Glance and
WorkManager, so it takes several minutes. A successful run ends with
`BUILD SUCCESSFUL`.

A warning like *"SDK XML version 4 … only understands up to 3"* is a harmless
mismatch between the command-line tools and the Gradle plugin. Ignore it.

What `test` covers (JVM, no device needed):

| Test | Pins |
|---|---|
| `PairingTest` | The shared derivation vector `K7M2P4QX9R` → stream `4b907ba136d0a7f2`, read `ec3cb369…ce84`; dashes and case ignored; alphabet and length checks |
| `ParserTest` | `devices[]` sorted by name; flat legacy shape; negative = no sensor; booleans, nulls and numeric strings; missing is unknown, never zero; duplicate hosts |
| `FreshnessAndFmtTest` | Age = relay age + time since fetch; offline floor of 60 s; number formats match the device screen |

Useful variants:

```powershell
.\gradlew.bat assembleDebug      # build app-debug.apk
.\gradlew.bat installDebug       # build and install on the connected phone
.\gradlew.bat clean              # when a build is behaving strangely
```

---

## Part 3: install on a phone

### 3.1 Enable USB debugging

1. Settings → About phone → tap **Build number** seven times.
2. Settings → System → **Developer options** → enable **USB debugging**.
3. Plug the phone in and accept the "Allow USB debugging?" prompt on its screen.

### 3.2 Check the phone is visible

```powershell
& "$env:USERPROFILE\.peekesp-toolchain\android-sdk\platform-tools\adb.exe" devices
```

The phone must be listed as `device`. `unauthorized` means you have not accepted
the prompt on the phone; `List of devices attached` with nothing under it means
no cable, a charge-only cable, or no driver.

### 3.3 Install

```powershell
.\gradlew.bat installDebug
```

No cable? Build the APK and copy it across:

```powershell
.\gradlew.bat assembleDebug
# app\build\outputs\apk\debug\app-debug.apk  → copy to the phone and open it
```

You will have to allow "install unknown apps" for whichever app opens the file.

> **Debug and release builds cannot update each other.** They are signed with
> different keys. When you later move to a release-signed build, uninstall the
> debug one first, or Android reports a signature mismatch.

---

## Part 4: pair and add the widget

1. **Have something pushing to your code.** The widget only has data once an
   agent on at least one machine is pushing to the same pairing code. Until then
   the relay answers "no telemetry received yet" and the app says so.
2. **Open PeekESP** and type the code your device shows (`K7M2-P4QX-9R`; dashes
   and case don't matter; there is no I, O, 0 or 1 in a code).
3. **Tap Pair.** The app checks the relay and reports honestly:
   - *Found N machines*: you are done.
   - *Nothing is pushing to this code yet*: expected if no agent is installed,
     and **also exactly what a typo looks like**. The relay claims a token on
     first use, so it cannot tell the two apart. Re-check the code if you expect
     machines.
   - *Could not reach the relay*: no network, or the relay address is wrong.
4. **Tap "Add widget to home screen"** and place it. If your launcher doesn't
   support that, long-press the home screen → Widgets → PeekESP. Resize to about
   4×2.

### What the widget shows

It draws the device's own dashboard: hostname and `1/2` machine marker, latency
of the last fetch, a status dot, CPU and RAM arcs, temperature and RX/TX,
storage free with a bar, uptime, and the link state.

- **Tap** shows the next machine and requests fresh numbers.
- **Updates** every 15 minutes (the platform's floor for background work) and on
  tap. That is 96 requests a day.
- **It shows its own age**, `as of 12m ago`, in the footer. A widget is stale by
  design, and a stale number shown as current is the one failure a monitor must
  not have. The label is dropped if the footer has no room.
- **Colours follow the device.** An arc turns amber at 75% and red at 90%. The
  glass glow in the bottom-right corner warms the same way when any reading is
  in trouble.
- **Link state.** `LINK OK` (green), `STALE` (amber: the machine stopped
  reporting, or the widget's data is over 35 minutes old), `NO LINK` (red: the
  last fetch failed; it keeps showing the last good reading, ageing).
- **Messages** replace the dashboard when there is nothing to draw: not paired,
  waiting for the first reading, code rejected, nothing pushing yet.

### Reset

In the app: **Forget this setup** clears the stored code, the cached reading and
the scheduled refresh. From a terminal: `adb uninstall com.rhshourav.peekesp`.

---

## Part 5: the rest of the system

The app is one end of a larger setup. Facts below are from the repository files;
for steps I have not seen, the folder's own README is the reference.

### The relay (Cloudflare Worker)

The default relay is `https://peek-relay.peekesp.workers.dev`. The app uses it
unless you change `RelayClient.DEFAULT_BASE` and rebuild (the pairing screen has
no relay field yet).

To run your own, from `cloudflare/`:

```powershell
npm test                  # node test/worker.test.mjs
npx wrangler login
npx wrangler deploy
```

Paired streams need **no secrets** on the Worker; each stream's Durable Object
claims its tokens on first use. Set `RELAY_BASE_URL` in the firmware's
`secrets.h`, and `RelayClient.DEFAULT_BASE` here, to your own `workers.dev` name.

`live_pair.py` runs an end-to-end check against the deployed Worker. Edit its two
hard-coded `D:\GITHUB\PeekESP\...` paths to your checkout first.

> **Free-tier ceiling.** The Workers free plan allows 100,000 requests a day per
> account. One display polling every 5 s is 17,280 a day, so about six pollers
> saturate it, before agent pushes are counted. The widget adds 96 a day. Check
> Cloudflare's current limits before relying on this.

### The device (ESP32 + display)

One firmware image drives exactly one panel, so there is one image per panel.
With only `esptool` (no PlatformIO):

```powershell
python tools/flash.py --panel ttgo-t-display      # the default; see PANELS.md for the rest
```

On first boot the device raises an access point, shows a QR code and serves a
settings form; what you save there survives reflashing. It then shows its
pairing code on screen, which is the code you type into this app. To build the
images yourself: `pip install platformio` then `python tools/build_panels.py`.
Only the T-Display has ever run on hardware.

### The agents and the Pi display host

Each machine you want to monitor runs an agent that pushes to the same pairing
code (the Windows agent under `windows/`, the Linux agent `dietpi/peek-agent.py`).
The Raspberry Pi display host is under `pi/`; see `pi/README.md`. Follow those
folders' instructions for installing them.

---

## Troubleshooting

Every row below happened during the first real setup.

| What you see | Cause | Fix |
|---|---|---|
| `What went wrong: 25.0.4.1` | Gradle 8.11.1 on JDK 25 | Install JDK 21, set `JAVA_HOME` to it (1.1), `gradle --stop`, retry |
| `JAVA_HOME is set to an invalid directory` | The path was a placeholder (`jdk-21.0.x-hotspot`) or the JDK moved | `Get-ChildItem "C:\Program Files\Eclipse Adoptium"` and use the real folder name |
| `Task 'run' not found` / nothing useful from `gradle run` | `run` is not a task in an Android project | Use `.\gradlew.bat test` or `installDebug` |
| `SDK location not found` | No `local.properties`, or no SDK | Part 1.2 and 1.3 |
| `Expand-Archive : Cannot validate argument on parameter 'Path'` | `$zip` was empty: no `commandlinetools-win-*.zip` in `Downloads` | Download the zip first; check with `Get-ChildItem "$env:USERPROFILE\Downloads" -Filter "*commandlinetools*"` |
| `sdkmanager.bat is not recognized` | The tools are not in a folder named `latest`, or the unpack step failed | `Test-Path "$sdk\cmdline-tools\latest\bin\sdkmanager.bat"` must be `True` |
| `Rename-Item: … cmdline-tools\cmdline-tools does not exist` | Nothing was unpacked, or the zip had no nested folder | List `$sdk\cmdline-tools`; copy the folder that contains `bin` and `lib` to `latest` |
| *SDK XML version 4 … understands up to 3* | Tools and plugin released at different times | Harmless; ignore |
| `adb devices` shows `unauthorized` | USB-debugging prompt not accepted | Unplug, replug, accept on the phone |
| `adb devices` shows nothing | Charge-only cable, or no driver | Try another cable; check the phone's USB mode |
| Install fails with a signature mismatch | A differently-signed build is already installed | `adb uninstall com.rhshourav.peekesp`, then install |
| Widget says *Not paired* | No setup stored | Open the app and pair |
| Widget says *Waiting for the first reading...* | Paired, but no refresh has completed yet | Tap the widget, or "Refresh widget now" in the app |
| Widget says *Nothing is pushing to this code yet* | No agent is pushing, or the code has a typo | Install an agent with the same code; re-check the code |
| Widget says *Code rejected* | The stream's read token was claimed by something else | Pair again; if it persists the code is in use elsewhere |
| Widget shows `NO LINK` | The last fetch failed (offline, relay down) | It keeps the last good reading; it recovers on the next refresh |
| Widget text is hard to read on a bright wallpaper | A widget cannot blur the wallpaper; the tint is fixed | Raise `GLASS_TOP_ALPHA` / `GLASS_BOTTOM_ALPHA` in `WidgetRenderer.kt` |
| A build error you can't read | A real compile error in the source | Run `.\gradlew.bat test` and send the first `e: …` line |

Still stuck? Run with `--stacktrace`, and for the widget use
`adb logcat -s AndroidRuntime` while adding it.

---

## Project layout

```
android/
├─ settings.gradle.kts        project + repositories
├─ build.gradle.kts           plugin versions (applied in app/)
├─ gradle/libs.versions.toml  every dependency version, in one place
├─ gradle.properties
├─ local.properties           YOUR sdk.dir. Git-ignored; never commit
└─ app/
   ├─ build.gradle.kts        applicationId, minSdk 26, targetSdk 35
   └─ src/
      ├─ main/
      │  ├─ AndroidManifest.xml     INTERNET only; backups off; HTTPS only
      │  ├─ assets/fonts/           Montserrat (variable), licence in assets/licenses/
      │  ├─ res/                    adaptive icon, widget metadata, backup rules
      │  └─ java/com/rhshourav/peekesp/
      │     ├─ MainActivity.kt      pairing screen
      │     ├─ data/
      │     │  ├─ Pairing.kt        code → stream + read token
      │     │  ├─ Model.kt          reply parsing (the Pi host's rules)
      │     │  ├─ RelayClient.kt    one GET; https only; no redirects
      │     │  ├─ SetupStore.kt     the code, AES-GCM encrypted via the Keystore
      │     │  ├─ WidgetCache.kt    what the widget last heard
      │     │  ├─ Freshness.kt      age and offline rules
      │     │  └─ Fmt.kt            number formats matching the device
      │     └─ widget/
      │        ├─ WidgetRenderer.kt the dashboard and the glass, drawn with Canvas
      │        ├─ PeekWidget.kt     Glance widget; tap = next machine
      │        ├─ RefreshWorker.kt  15-minute refresh and refresh-on-tap
      │        ├─ Frame.kt          palette, link state, accessibility text
      │        └─ Fonts.kt
      └─ test/                      JVM unit tests
```

---

## Design decisions worth knowing

- **The widget is a drawn image.** Glance renders through RemoteViews, which
  cannot draw arcs. `WidgetRenderer` paints the whole dashboard into a bitmap at
  the widget's exact size; Glance shows it. TalkBack reads a generated
  description (`Frame.describe()`) instead of the pixels.
- **Glass-like, not glass.** A widget cannot sample or blur the wallpaper. The
  card is a translucent tint, light from two corners, a sheen and a specular
  rim. The tint is strong enough for text to hold contrast over most wallpapers,
  not guaranteed over pure white.
- **Layout is the firmware's arithmetic.** Every position is a coordinate on the
  T-Display's 240×135 screen, scaled by width, height, or whichever axis has
  less room for round things, as `layout_metrics.h` does. At 16:9 it lands where
  the device puts it; at other shapes it is proportional, not tuned.
- **Dim text is lighter than the device's** (`#93A3BA`, not `#5C6B82`). The
  device's colour is 3.7:1 on its own background, below the 4.5:1 the design
  asks for.
- **Severity follows the firmware, not the screenshot.** `load_color()` turns an
  arc amber at 75% and red at 90%. A screenshot of RAM at 78% in magenta predates
  that or is from another host. Change `Palette.WARN_FROM` to match it.
- **Freshness is honest.** A reading's age is the relay's own timestamp plus the
  time since the app last fetched it. Without the second term, a phone that lost
  its connection would show every machine as permanently fresh.
- **The code is the credential.** Stored encrypted with an AES-GCM key held in the
  Android Keystore; never logged, never sent (only the derived stream and read
  token travel); shown masked after entry. Backups and device transfer are
  disabled, because Keystore keys do not migrate and a restored ciphertext would
  be unreadable.
- **Security surface.** HTTPS only, cleartext disabled, redirects refused so a
  token is never replayed elsewhere. The manifest declares `INTERNET` only, but
  WorkManager merges a few permissions of its own into the final manifest; read
  them from the merged manifest before a release.
- **Setups use `SharedPreferences`** (holding an encrypted blob), not DataStore,
  to keep this slice dependency-light.
- **Versions are pinned in `gradle/libs.versions.toml`** to ones that exist; bump
  them freely.

---

## What is and is not verified

**Verified on your machine:** the project compiles and `.\gradlew.bat test` passes
(pairing vector, parser rules, freshness, formats).

**Not yet verified:**

- Anything on a real phone: installation, pairing against a live relay, the
  widget on a launcher, its proportions at 4×2, text legibility and glass
  strength over real wallpapers.
- The relay-side and firmware-side changes in the roadmap below.
- The release-signed build and CI.

When you first run it, check: pairing succeeds against a live stream, the widget
draws and its numbers match the device, tapping cycles machines, the footer age
advances after 15 minutes, TalkBack reads the description, and a very large font
size still looks sensible.

---

## Roadmap

| Phase | Work | State |
|---|---|---|
| Widget slice | Pairing screen, glass widget, refresh | Built; compiled and unit-tested; not yet run on a phone |
| A1 | Worker: record display identity, `?displays=1` list; deliver `cmd` only to ESP32 displays (today any reader consumes it) | Planned |
| A2 | Firmware: send `X-Peek-Display`, `X-Peek-Fw`, `X-Peek-Panel`; rebuild all seven panel images | Planned |
| A3 | Pi display host: send the same headers | Planned |
| B | Dashboard and machine screens, settings, visual system with real glass on Android 13+, second widget size | Planned |
| Release | CI (`android.yml`), signing with four GitHub secrets (`ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`), release tags `android-vX.Y.Z` published with *latest* off so they never trigger the firmware update banner | Planned |

The full plan is in the PeekESP for Android Build Plan doc.

## Licences

Montserrat is SIL Open Font License 1.1 (`app/src/main/assets/licenses/Montserrat-OFL.txt`).
Your own project licence goes here.
