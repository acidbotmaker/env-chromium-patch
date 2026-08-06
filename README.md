# env-chromium-patch

Patches a Chromium checkout so that the JS-visible device identity — user agent,
`navigator.platform`, `navigator.hardwareConcurrency`, the WebGL renderer/vendor
strings, WebGPU adapter info, and the `enumerateDevices()` result — can be set
from environment variables at launch time. Anything you don't set keeps its
stock value, so an unconfigured build behaves exactly like upstream Chromium.

## Applying

The patch is applied by a script rather than shipped as a unified diff. It was
authored without access to your checkout, so it finds each edit site by a
verbatim anchor quoted from Chromium source instead of by line number. Every
anchor must match exactly once; if any fails to resolve, nothing is written and
you get the file and search string to fix by hand.

```sh
python3 apply.py --chromium-src ~/chromium/src --dry-run    # inspect the diff
python3 apply.py --chromium-src ~/chromium/src              # apply
python3 apply.py --chromium-src ~/chromium/src --revert     # undo
```

Add `--emit-patch env-fp.patch` to also write a real `git diff` of the result,
which is what you want for archiving or code review.

The applier sniffs two `base/` APIs that were renamed recently
(`Environment::GetVar`'s signature and `base::Value::Dict` vs `base::DictValue`)
and generates whichever spelling your tree uses, so it works across a range of
milestones.

Then build normally:

```sh
autoninja -C out/Release chrome
```

## Configuration

| Variable | Effect |
|---|---|
| `CHROME_ENV_UA` | `navigator.userAgent` and the outgoing `User-Agent` header |
| `CHROME_ENV_UA_PLATFORM` | `navigator.userAgentData.platform`, `Sec-CH-UA-Platform` |
| `CHROME_ENV_UA_PLATFORM_VERSION` | `Sec-CH-UA-Platform-Version` |
| `CHROME_ENV_PLATFORM` | `navigator.platform` |
| `CHROME_ENV_VENDOR` | `navigator.vendor` |
| `CHROME_ENV_HARDWARE_CONCURRENCY` | `navigator.hardwareConcurrency` (clamped to 1–1024) |
| `CHROME_ENV_WEBGL_RENDERER` | `UNMASKED_RENDERER_WEBGL` |
| `CHROME_ENV_WEBGL_VENDOR` | `UNMASKED_VENDOR_WEBGL` |
| `CHROME_ENV_WEBGL_VERSION` | driver string inside `GL_VERSION` |
| `CHROME_ENV_WEBGL_SHADING_LANGUAGE_VERSION` | driver string inside `GL_SHADING_LANGUAGE_VERSION` |
| `CHROME_ENV_WEBGPU_VENDOR` | `GPUAdapterInfo.vendor` |
| `CHROME_ENV_WEBGPU_ARCHITECTURE` | `GPUAdapterInfo.architecture` |
| `CHROME_ENV_WEBGPU_DESCRIPTION` | `GPUAdapterInfo.description` (developer features only) |
| `CHROME_ENV_MEDIA_DEVICES` | `enumerateDevices()` result, as JSON |

Each also has a `--env-fp-*` command-line switch (`--env-fp-ua`,
`--env-fp-webgl-renderer`, …). The environment wins when both are set. The
switches exist because the browser re-emits whatever it resolved to its child
processes, so the overrides survive launchers that scrub the child environment
block. Setting them by hand works too.

An empty value counts as unset, so `CHROME_ENV_UA= chrome` is the same as not
setting it.

See `env.example` for a complete, coherent profile.

### When the values are read

At browser startup, not at build time. Only the variable *names* are compiled
in; each value is read with `getenv` the first time it is needed. Build once,
then change the values as often as you like — a different profile is just a
different launch.

Each browser process latches its values on startup and caches them, so changing
a variable does not affect an already-running Chrome. Relaunch to pick up a
change. The flip side is that two Chrome instances launched with different
variables run side by side quite happily, each with its own identity.

```sh
./run.sh profiles/win-nvidia.env            # one identity
./run.sh profiles/mac-m3.env                # another, at the same time
```

`run.sh` sources the profile, gives each one its own `--user-data-dir` (so
permission grants and cookies do not leak between them), prints what it set, and
execs Chrome. Set `CHROME_BIN` if your binary is not at
`~/chromium/src/out/Release/chrome`. Extra arguments are passed through:

```sh
./run.sh profiles/win-nvidia.env https://example.com
CHROME_BIN=/opt/chromium/chrome ./run.sh profiles/mac-m3.env --incognito
```

Nothing requires the launcher — it is a convenience over setting the variables
yourself:

```sh
CHROME_ENV_PLATFORM=Win32 CHROME_ENV_HARDWARE_CONCURRENCY=8 \
  out/Release/chrome --user-data-dir=/tmp/p1
```

Copy `env.example` to `profiles/<name>.env` to add your own.

### Derived values

`navigator.platform` and the UA client hints use different vocabularies —
`"Win32"` versus `"Windows"`. If you set `CHROME_ENV_PLATFORM` but not
`CHROME_ENV_UA_PLATFORM`, the client-hint value is derived from it
(`Win32`→`Windows`, `MacIntel`→`macOS`, `Linux x86_64`→`Linux`,
`Linux armv81`→`Android`, `iPhone`/`iPad`→`iOS`). Set both explicitly if you
need something outside that mapping.

`navigator.appVersion` needs no variable of its own: Chromium defines it as
everything past the first `/` of the UA string, so it follows `CHROME_ENV_UA`
automatically.

When `CHROME_ENV_UA` contains a `Chrome/<version>` token, the brand lists in
`Sec-CH-UA` and `Sec-CH-UA-Full-Version-List` are rewritten to that version.
Without this the headers would advertise the real build number while the UA
string claimed another, which is worse than not spoofing at all.

### Media devices JSON

```json
{
  "audioinput": [
    {"deviceId": "default", "label": "Default - Microphone (Realtek Audio)", "groupId": "grp-onboard"}
  ],
  "videoinput": [
    {"deviceId": "cam0", "label": "HD Pro Webcam C920", "groupId": "grp-usb", "facingMode": "user"}
  ],
  "audiooutput": [
    {"deviceId": "default", "label": "Default - Speakers (Realtek Audio)", "groupId": "grp-onboard"}
  ]
}
```

`deviceId` and `label` are required per entry; `groupId` and `facingMode`
(`user`, `environment`, `left`, `right`) are optional. Each of the three
top-level keys is optional — omit one and that device type enumerates for real.
An explicit empty array means "no devices of this type", which is a useful thing
to be able to say.

Two consequences worth knowing about:

1. **The `deviceId` your JS sees is not the one you wrote.** Chromium HMACs
   device and group IDs per origin, and the patch deliberately sits upstream of
   that so the synthetic devices are hashed exactly like real ones. JS receives
   a 64-character lowercase hex string. Only `""`, `"default"` and
   `"communications"` pass through verbatim.
2. **`getUserMedia` will not open a synthetic device.** Nothing backs it. If you
   need capture to succeed, pair this with
   `--use-fake-device-for-media-stream`.

Labels follow the normal permission model: a page without camera/microphone
permission sees one blank entry per device type, exactly as in stock Chrome.
That is intentional — always-visible labels would be a behaviour no real browser
exhibits, and therefore a giveaway.

## Verifying

Open `test/fingerprint.html` in the patched build. It prints every overridden
value, re-runs the same checks inside a Worker, and flags the inconsistencies
that are easy to introduce:

- UA string version vs. `Sec-CH-UA` brand version
- `navigator.platform` vs. `navigator.userAgentData.platform`
- main thread vs. worker (catches overriding `Navigator` instead of
  `NavigatorBase`, which would leave workers reporting the truth)
- `navigator.appVersion` still containing the real UA

The first thing to check is that an **unconfigured** run reports stock values
everywhere. The patch must be inert by default.

## Design notes

Reading happens once per process, cached in a `base::NoDestructor`, because
`getenv` is not safe against a concurrent `setenv` and these values are read
from hot JS entry points on several threads.

The shared config reader is appended to `third_party/blink/public/common/switches.h`
and `third_party/blink/common/switches.cc` rather than living in its own
component. `blink_common` is already linked into the browser, renderer and GPU
processes, and both `components/embedder_support` and `content/browser` already
depend on it, so this adds no new source files and touches no `BUILD.gn` — which
is what makes the patch survive rebases. An upstream CL would grow its own
component instead; this is a downstream convenience, not a style to copy.

It also avoids `base/environment.h` inside Blink, which is not on Blink's DEPS
allowlist and would fail `checkdeps`. Blink reads the values through
`blink::env_fingerprint`, which lives on the allowed side of that boundary.

`navigator.hardwareConcurrency` is overridden in Blink, not in
`base::SysInfo::NumberOfProcessors()`. Patching the latter would resize the real
renderer thread pool.

The masked `GL_RENDERER` (`"WebKit WebGL"`) and `GL_VENDOR` (`"WebKit"`) values
are left alone, because those are the constants stock Chrome returns. The
`WEBGL_debug_renderer_info` extension gate is also left intact — answering when
the extension is disabled would itself be a detectable difference.

### Not covered

- The GPU process's own `GPUInfo`, visible at `chrome://gpu`. Only the
  JS-visible WebGL and WebGPU strings change.
- Per-profile, per-tab or per-origin variation. These are per-browser-process
  globals.
- `checkdeps` is not run by `autoninja`. The design should stay clean, but run
  `gn check` if you plan to upstream anything.

## Layout

```
apply.py              anchored applier
run.sh                launch a build with a profile
profiles/             ready-made profiles (win-nvidia, mac-m3/m4/m5)
edits/shared.py       the config reader appended to blink's switches.{h,cc}
edits/ua.py           user agent + client hints
edits/navigator.py    platform, vendor, hardwareConcurrency
edits/webgl.py        WebGL renderer/vendor/version
edits/webgpu.py       GPUAdapterInfo
edits/media.py        enumerateDevices
edits/propagate.py    browser to child-process switch forwarding
edits/refresh_clock.py  human-like BeginFrame timing (see docs/refresh-clock.md)
src/                  whole files copied into the tree
tools/                trace analysis
docs/                 design notes and measured limitations
env.example           a coherent Windows/NVIDIA profile
test/fingerprint.html verification page
test/capture-profile.html  build a profile from a real machine
test/refresh-clock.html    adversarial harness for the refresh clock
```

## Refresh clock

A second, independent feature lives in the same applier: a seed-driven timing
model that replaces Chromium's perfectly periodic BeginFrame schedule with one
whose statistics resemble a real compositor delivery path. It is off unless
`CHROME_ENV_REFRESH_CLOCK=1` is set, and it changes no web-facing API directly —
only the compositor's frame scheduling.

See **[docs/refresh-clock.md](docs/refresh-clock.md)** for the design, the
measured validation results, and — importantly — the checks that break it.
Two findings worth knowing before using it:

* The rAF timestamp Blink exposes is quantised to 100 µs unless the page is
  cross-origin isolated, which is *larger* than the injected jitter. Measure
  cross-origin isolated or you are measuring the quantiser.
* Cadence variance does not respond to render load, only the drop rate does.
  A load-coupling detector separates this model from real hardware.

## Building an accurate profile

A profile is only useful if it describes a machine that actually exists. A core
count or GPU string that matches no real configuration is *more* identifying
than not spoofing at all, so prefer captured values over plausible ones.

Open `test/capture-profile.html` in **stock Chrome on the machine you want to
imitate**, grant camera and microphone access, and click Capture. It writes a
ready-to-save profile with that machine's real UA, platform, core count, WebGL
and WebGPU strings, and device list.

Two things it handles that hand-editing gets wrong: it unwraps the driver string
from inside `WebGL 1.0 (…)`, since the patch sets the inner part only; and it
replaces the captured `deviceId` values, which are per-origin HMACs and
meaningless to copy, with stable readable ids while preserving which devices
share a group.

The bundled `mac-m4.env` and `mac-m5.env` carry `[VERIFY]` markers on fields
that are reconstruction rather than observed output — chiefly the macOS
version, the ANGLE build hash, and the WebGPU architecture token. Replace them
with captured values before using those profiles in anger.

To adjust an anchor after a rebase, edit the relevant `edits/*.py`; the applier
tells you which one when a match fails.
