# Cosmos — Android app (Chaquopy + WebView)

A single-activity Android app that runs the `news_hub` Python package on-device
via **Chaquopy** and displays the generated HTML brief in a **WebView**.

The app is branded **Cosmos**: the launcher name is "Cosmos" and the package /
applicationId is `com.cosmos.app`. The `news_hub` Python package name is
internal (never shown to the user) and is deliberately unchanged.

- On launch: a minimal loading screen (spinner, *"Loading, please wait…"*,
  *"Generating your news brief…"*).
- On a background thread: Python starts, `runner.run()` calls `news_hub.cli.main`
  with `--output <app-private filesDir>/reports`, `--timeout 12`, `--jobs 8` and
  `--no-pdf` (the WebView reads HTML; the PDF would only add to the wait).
- On success: the loading screen is hidden and the WebView loads the newest
  `*.html` in the output dir via `file://`, with file access enabled, a wide
  viewport for tablet reading, zoom enabled, and JavaScript + network loads
  disabled (the report is fully self-contained: inline CSS + inline SVG, only
  `<a href>` links).
- On any failure: the spinner is dropped and the loading screen's title/subtitle
  are swapped for the exception text, so the app never sits on a stuck spinner.
  Python's stdout/stderr also land in logcat under the tags `python.stdout` and
  `python.stderr`.

## Version matrix (exact, mutually compatible)

| Component              | Version   | Why it fits                                              |
| ---------------------- | --------- | -------------------------------------------------------- |
| Gradle                 | **8.9**   | AGP 8.5 requires Gradle ≥ 8.7; 8.9 is the Studio Koala default, JDK 17–22 |
| Android Gradle plugin  | **8.5.2** | Chaquopy 15.0 officially supports AGP 7.0–8.5            |
| Chaquopy               | **15.0.1**| Final patch of the 15.0 line: Python 3.8–3.12, min API 21 |
| Python (runtime)       | **3.11**  | `python { version "3.11" }` selects the APK runtime; package needs 3.10+, 3.11 is inside Chaquopy 15.0's 3.8–3.12 range |
| Kotlin                 | **1.9.24**| Standard pairing with AGP 8.5.x / Gradle 8.9             |
| JDK                    | **17**    | Required by AGP 8.x (Studio bundles one)                 |
| compileSdk / targetSdk | **34**    | AGP 8.5 supports API 34 natively (no warnings)           |
| minSdk                 | **24**    | ≥ Chaquopy 15.0's minimum API 21                         |

Compatibility facts verified against the official Chaquopy version table
(`chaquo.com/chaquopy/doc/current/versions.html`): Chaquopy **15.0** →
Python 3.8–3.12, Android Gradle plugin 7.0–8.5, minimum Android API 21.
`Python.getInstance()` is present in the 15.0.1 runtime (`Python.java` in the
tagged source). No third-party Python packages: `news_hub` imports **only the
standard library** (`urllib`, `xml.etree`, `json`, `email.utils`, `concurrent.futures`,
`dataclasses`, …), so there is **no `pip` configuration at all** in the Gradle
files — Chaquopy bundles the full stdlib.

## Project layout

```
android/
├── settings.gradle                 # repos + :app module
├── build.gradle                    # plugin versions, declared once
├── gradle.properties               # JVM heap for the daemon
├── gradlew / gradlew.bat           # wrapper scripts (jar NOT included, see below)
├── gradle/wrapper/gradle-wrapper.properties   # pins Gradle 8.9
├── sync_python.py                  # re-copy the Python package into the app
└── app/
    ├── build.gradle                # Android + Chaquopy config (python { version "3.11" })
    ├── proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml     # INTERNET + REQUEST_INSTALL_PACKAGES only
        ├── java/com/cosmos/app/MainActivity.kt
        ├── python/
        │   ├── runner.py           # SystemExit-safe wrapper around cli.main()
        │   └── news_hub/           # runtime modules only (see below)
        └── res/
            ├── layout/activity_main.xml
            ├── values/{strings,colors,themes}.xml
            └── mipmap-{mdpi,hdpi,xhdpi,xxhdpi,xxxhdpi}/ic_launcher.png
                                                        # full-bleed PNG icon (48/72/96/144/192)
```

The copied `news_hub/` package contains exactly the five runtime modules —
`__init__.py`, `cli.py`, `core.py`, `geometry.py`, `sources.py` — i.e. everything
`cli.main()` imports. Excluded on purpose: `__main__.py` (only used by
`python -m news_hub`), the `news_hub.py` import shim (only helps running from
*inside* the package folder, and would shadow the real package on Android),
`test_*.py`, and `__pycache__`. **If you edit the Python package, re-sync this
copy** (see below) or the app keeps running the old code.

## Custom icon

The launcher icon is the user's own **full-bleed PNG** (`ic_launcher.png`),
installed at all five densities: `mipmap-mdpi` (48×48), `mipmap-hdpi` (72×72),
`mipmap-xhdpi` (96×96), `mipmap-xxhdpi` (144×144) and `mipmap-xxxhdpi`
(192×192). It is **not** an adaptive icon — there are no `mipmap-anydpi*`
XMLs, so the same PNG is used on every supported Android version (the manifest
still points at `@mipmap/ic_launcher`, which now resolves to the density PNGs).

To change the icon:

- **Drop a new square PNG** named `ic_launcher.png` over the five density
  files, each sized to match (48/72/96/144/192).
- Or keep a single master image and re-run the resize step (`sync` script or
  an image-resize tool — e.g. Pillow with Lanczos) to regenerate the five
  files from it. Non-square sources must be centred on a transparent square
  before resizing; nothing here crops.

## How to build

### Build without Android Studio (cloud)

No Android Studio or Android SDK needed locally — just git (or GitHub's web
upload) and this folder:

1. Push this `android/` folder to a GitHub repo **as the repo root** (on the
   GitHub website: **Add file → Upload files**).
2. The workflow (`.github/workflows/build-apk.yml`) builds the APK
   automatically on push to `main`/`master`; you can also trigger it by hand:
   **Actions** tab → **Build Cosmos APK** → **Run workflow**.
3. When the run finishes, open the **Actions** tab → the finished run →
   **Summary** → **Artifacts**, and download `cosmos-debug-apk` — or take
   `cosmos-debug.apk` from the newest entry under **Releases**, which is the
   copy the in-app updater reads.

`gradle-wrapper.jar` is intentionally not needed: the workflow installs
Gradle 8.9 itself (`gradle/actions/setup-gradle@v4`), sets up JDK 17 and the
Android SDK, and uploads `app/build/outputs/apk/debug/app-debug.apk` as the
artifact.

### Android Studio (easiest)

1. **File → Open** the `android/` folder (not the parent workspace).
2. Studio will use the Gradle wrapper pinned by `gradle-wrapper.properties`.
   If it complains that `gradle-wrapper.jar` is missing, choose its option to
   fix/regenerate the wrapper, or see "Regenerating the wrapper jar" below.
3. Let the first sync finish — it downloads Gradle 8.9, AGP/Kotlin/Chaquopy
   plugins, and (once, per ABI) the Python 3.11 builds Chaquopy ships. Needs
   network access. JDK 17 is bundled with Studio.
4. **Build → Build APK(s)**. Output:
   `app/build/outputs/apk/debug/app-debug.apk`.
5. Install on a device/emulator with `adb install -r app-debug.apk` or via the
   Run button.

The debug APK contains all four ABIs (armeabi-v7a, arm64-v8a, x86, x86_64), so
it works on both emulators and physical devices. A `release` build needs your
own signing config (not included).

### Command line

With the wrapper in place:

```bat
cd android
gradlew assembleDebug        :: Windows
./gradlew assembleDebug      # macOS / Linux
```

Release: `gradlew assembleRelease` (requires a signing config; debug-signed by
default only in `debug` variants).

### Regenerating the wrapper jar

The binary `gradle/wrapper/gradle-wrapper.jar` **cannot be committed to this
scaffold**, so it is not included — only `gradle-wrapper.properties` (pinning
Gradle 8.9) and the `gradlew` scripts (taken verbatim from the Gradle 8.9.0
tag) are. To generate the jar, any of:

- **Android Studio**: on open it offers to regenerate the wrapper, or you can
  run "Fix Gradle wrapper and re-import project".
- **A local Gradle install** (any recent version, e.g. via
  `choco install gradle` / `scoop install gradle` / `sdk install gradle`), then:

  ```bat
  cd android
  gradle wrapper --gradle-version 8.9
  ```

  This creates `gradle-wrapper.jar` and refreshes `gradlew`/`gradlew.bat`; the
  `distributionUrl` in `gradle-wrapper.properties` will match what you passed.
- **Copy the jar** from another Gradle 8.x project's `gradle/wrapper/` folder.

## How the app works

1. `MainActivity.onCreate` shows `activity_main.xml`: the WebView is `gone`,
   the loading panel (spinner + two TextViews) is visible.
2. A background thread (`cosmos-pipeline`) runs `runPipeline()`:
   `Python.getInstance().getModule("runner").callAttr("run", filesDir/reports, 12, 8)`.
3. `runner.run()` (in `src/main/python/runner.py`) calls
   `news_hub.cli.main(["--output", <abs dir>, "--timeout", "12", "--jobs", "8", "--no-pdf"])`.
   `main(argv: list[str] | None = None) -> int` returns `0` on success and
   raises `SystemExit` via argparse on bad arguments; the wrapper catches
   `SystemExit` and every other exception and converts them to a status string
   (`ok:0`, `exit:<code>`, `error:<ExcName>:<msg>`) so nothing ever escapes
   across the Python/Kotlin boundary.
4. `onPipelineFinished()` runs on the UI thread. For `ok:` it picks the newest
   `*.html` under the output dir (reports are named `YYYY-MM-DD.html`) and
   loads it with `file://`; otherwise (or if no HTML exists) `showError()`
   hides the spinner and shows the failure text.

Notes:

- The CLI's `--output` default is the *relative* dir `reports/`, and cwd on
  Android is not a stable place to write, so the app always passes the absolute
  app-private path (`context.filesDir/reports`). No storage permission needed.
- The package reads **no config file from cwd** (sources come from code
  defaults unless `--sources` is given), so nothing else is path-sensitive.
- `android:configChanges` on the activity prevents rotation from restarting
  the pipeline; a fresh launch simply regenerates today's report.

## Auto-update

Every push to `main` builds an APK and publishes it as a **GitHub release**
(tagged `build-<versionCode>`) in the last step of the workflow. On launch the
app asks GitHub's public releases API for the newest release carrying its APK
asset and, when that build is newer than the installed one, offers it in a
banner along the top of the screen.

- **Why releases and not Actions artifacts?** The artifact zip endpoint answers
  `401 Requires authentication` to an unauthenticated client *even for a public
  repository*, so an app without a token can list artifacts but can never
  download one. To confirm:

  ```bash
  curl -sLi -H 'User-Agent: x' \
    https://api.github.com/repos/Plrlr/cosmos/actions/artifacts/<artifact-id>/zip
  ```

  Artifacts also expire (90 days by default), while a release asset downloads
  anonymously and permanently.
- **Version numbers.** The workflow passes `COSMOS_VERSION_CODE=<run number>`
  to Gradle, so the APK's `versionCode` *is* the CI run number: strictly larger
  on every push, which is what lets each build install over the previous one.
  The release tag carries the same number (`build-21`), so the app decides
  "newer?" by comparing numbers rather than trusting the device clock. A local
  build falls back to the checked-in `versionCode` (8) and so cannot install
  over a CI build — expected, and only affects development.
- **Installing.** Android 8+ requires `REQUEST_INSTALL_PACKAGES` (declared in
  the manifest) *and* the user's permission under **Settings → Apps → Cosmos →
  Install unknown apps**. When that is still off, the app opens that screen for
  you and finishes the install when you come back.
- **Before the installer runs**, the downloaded file is checked: it must be a
  zip containing `AndroidManifest.xml`, it must be `com.cosmos.app`, and it must
  not be older than the installed copy. Each failure names the step that broke
  (download, validation, or installer) instead of one catch-all message.
- **One manual install is needed** for any copy built before this channel
  existed — an old APK keeps asking the artifact endpoint and reports
  "Couldn't open the installer". Install the newest APK from **Releases** once;
  after that the in-app updater takes over.

## Re-syncing the Python package

After editing anything under `news_hub/` (the package at the workspace root),
copy the runtime modules into the app again:

```bat
cd android
python sync_python.py
```

or manually: copy `__init__.py`, `cli.py`, `core.py`, `geometry.py`,
`sources.py` from the workspace root into
`android/app/src/main/python/news_hub/` (skip `__pycache__`, tests,
`__main__.py`, `news_hub.py`).

## Troubleshooting

- **First sync slow**: Chaquopy downloads Python 3.11 builds per ABI; be online.
- **`error:UnsatisfiedLinkError...` on screen**: Python native lib missing for
  the device ABI — make sure the APK contains the right ABI (`unzip -l app.apk
  | grep libpython`) or trim `abiFilters`.
- **Feeds fail but the report still appears**: expected — per-source failures
  are recorded in the report's collection notes and never abort the brief.
- **Debug the pipeline**: `adb logcat -s python.stdout python.stderr` shows the
  CLI's own progress prints.
- **"Update download failed: HTTP 401"**: the installed build predates the
  release channel and is still pointed at Actions artifacts, which reject
  unauthenticated downloads. Install the newest APK from **Releases** once.
- **"The downloaded update is not installable: …"**: the download finished (the
  banner showed a percentage) but the file failed validation — the message says
  which check, e.g. `not a zip archive` when an error page was saved instead of
  an APK.
- **"Couldn't open the installer."**: no activity answered the install intent,
  which in practice means no package installer is visible to the app. The
  manifest's `<queries>` block and `REQUEST_INSTALL_PACKAGES` are what make one
  visible and legal to call.
