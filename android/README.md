# PeekESP for Android - widget slice

Package `com.rhshourav.peekesp`. This slice is the pairing screen and the home-screen
widget that draws your device's dashboard on a glass card. The dashboard and machine
screens come after it.

## Build

You need JDK 17 or newer and the Android SDK (`local.properties` with `sdk.dir`).
The Gradle wrapper is not committed yet, because generating it needs a download:

```
gradle wrapper --gradle-version 8.11.1
./gradlew test             # pairing vector, parser, freshness, formatting
./gradlew installDebug
```

Then open PeekESP, enter the code your device shows, and tap "Add widget to home screen".
Tapping the widget shows the next machine.

## Where things are

| File | What it does |
|---|---|
| `data/Pairing.kt` | Code -> stream + read token. Pinned by `PairingTest` to the shared vector. |
| `data/Model.kt` | Reply parsing, with the Pi host's rules (`relay.py`). |
| `data/RelayClient.kt` | One GET, https only, no redirects, own User-Agent. |
| `data/SetupStore.kt` | The code, AES-GCM encrypted with a Keystore key. |
| `widget/WidgetRenderer.kt` | The dashboard and the glass, drawn with Canvas. |
| `widget/PeekWidget.kt` | Glance widget: shows the bitmap, tap = next machine. |
| `widget/RefreshWorker.kt` | One request every 15 minutes, and on tap. |

## Decisions worth knowing

- **The widget is a drawn image.** Glance cannot draw arcs, so the renderer paints the
  whole dashboard into a bitmap at the widget's exact size and Glance shows it. TalkBack
  reads a generated description instead of the pixels.
- **Glass-like, not glass.** A widget cannot sample or blur the wallpaper. The card is
  a tint with light from two corners, a sheen and a specular rim. The bottom-right light
  turns amber or red when a reading is in trouble. Raise `GLASS_*_ALPHA` in
  `WidgetRenderer.kt` for very bright wallpapers.
- **Severity follows the firmware.** An arc turns amber at 75% and red at 90%
  (`load_color()` in `PeekESP.ino`). Your screenshot shows RAM at 78% in magenta, which
  the current firmware would draw amber; change `Palette.WARN_FROM` to match the picture.
- **The widget prints its own age** ("as of 12m ago") in the footer, between the uptime
  and the link state, and drops it if there is no room.
- **Dim text is lighter than on the device** (`#93A3BA`, not `#5C6B82`), because the
  device's colour is 3.7:1 on its own background and glass is brighter than that.
- **Setups use SharedPreferences, not DataStore**, to keep this slice dependency-light.

## Not verified

Nothing here has been compiled or run: the build environment this was written in has no
Android SDK. Expect a first-build error or two; hand them back and they will be fixed.
The widget has not been seen on a launcher, so its proportions at 4x2 are unchecked.
Montserrat (SIL OFL) is bundled from the google/fonts repository; its licence is in
`app/src/main/assets/licenses/`.
