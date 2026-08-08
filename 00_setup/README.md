# Rebuilding the patched Chromium from scratch

Runbook for standing up a build box and producing a patched `chrome` binary.
Written to be followed months later with nothing remembered.

Measured on the current build box (`-j90`):

| | |
|---|---|
| Build volume | **250 GB** |
| First build, from a clean `out/Release` | **~1 h 48 m** |
| Rebuild after a patch change | **20–30 min**, depending on what changed |
| Changing only fingerprint *values* | **no rebuild at all** |

That last row is the one worth remembering. The overrides are read from the
environment (or the `--env-fp-*` switches) at browser startup, so a different
identity is just a different launch. Only editing the patch source costs a
rebuild.

Fetching Chromium on top of that takes roughly another hour, so budget half a
day for a box built from nothing.

---

## Just run the script

On a fresh Ubuntu EC2 box, everything below is automated:

```sh
git clone https://github.com/acidbotmaker/env-chromium-patch.git
cd env-chromium-patch
tmux new -s build                        # it runs for hours
CHROMIUM_TAG=153.0.7995.0 JOBS=90 ./00_setup/setup.sh
```

Every phase is idempotent, so re-running after a failure resumes rather than
starting over. Knobs, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `DEVICE` | `/dev/nvme1n1` | build volume; `""` builds on the root disk |
| `MOUNT` | `/mnt/chromium-hdd` | where it mounts |
| `FORMAT_DISK` | `no` | `yes` permits `mkfs` on an **empty** device |
| `CHROMIUM_TAG` | *(empty)* | milestone to pin; empty means tip of tree |
| `JOBS` | `90` | ninja parallelism |
| `BUILD_TARGET` | *(empty)* | ninja target; empty builds everything |
| `SKIP_BUILD` | `no` | `yes` stops after `gn gen` |

**Formatting is opt-in and cannot happen by accident.** The script refuses to
`mkfs` unless `FORMAT_DISK=yes` *and* `file -s` reports the device carries no
filesystem *and* it is not mounted anywhere. A disk that already has a
filesystem is mounted as-is, never reformatted. That logic is covered by
`./00_setup/test-disk-guard.sh`, which runs the real function against a fake
device and asserts `mkfs` fires in exactly one of four scenarios.

The rest of this document is what the script does, step by step, with the
reasoning — read it when something fails, or when you want to do it by hand.

---

## The whole sequence

Each step is expanded below with the checks and the reasons. This block is the
shape of it, for when you only need reminding.

```sh
# 0  dependencies
./00_setup/00_setup-chromium-repo.sh

# 0.5  mount the build volume (format ONLY if `file -s` says "data")
sudo mount /dev/nvme1n1 /mnt/chromium-hdd
sudo chown -R "$USER:$USER" /mnt/chromium-hdd

# 1  depot_tools
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git \
  "$HOME/depot_tools"
export PATH="$HOME/depot_tools:$PATH"

# 2  clone chromium, then pin a milestone
mkdir -p /mnt/chromium-hdd/chromium && cd /mnt/chromium-hdd/chromium
fetch --nohooks --no-history chromium
cd src
./build/install-build-deps.sh
gclient runhooks
git checkout -b build-153 refs/tags/153.0.7995.0
gclient sync -D --force --reset

# 3  apply the patch
cd ~/env-chromium-patch
python3 apply.py --chromium-src /mnt/chromium-hdd/chromium/src --dry-run
python3 apply.py --chromium-src /mnt/chromium-hdd/chromium/src

# 4  configure and build
cd /mnt/chromium-hdd/chromium/src
gn gen out/Release --args='
is_debug=false
is_component_build=false
symbol_level=0
blink_symbol_level=0
dcheck_always_on=false
proprietary_codecs=true
ffmpeg_branding="Chrome"
enable_library_cdms=true
enable_widevine=true
'
autoninja -C out/Release -j90

# 5  verify before trusting it
./out/Release/chrome --version
```

---

## 0. Pre-flight

The box is Ubuntu (this repo's setup script is `apt-get`-based). Two packages
are assumed present and are *not* in the script because most images ship them —
check before starting, because both fail late and confusingly:

```sh
python3 --version     # depot_tools is python3; without it `fetch` dies
lsb_release -a        # install-build-deps.sh reads this
```

Then run the dependency script (as root, or prefix with sudo):

```sh
./00_setup/00_setup-chromium-repo.sh
```

That installs the bootstrap toolchain and creates the `/mnt/chromium-hdd`
directory. It does **not** mount anything — do that next, before fetching, or
the checkout lands on the root filesystem and fills it.

---

## 0.5 Mount the build volume at /mnt/chromium-hdd

The checkout plus one release build lives on a separate 250 GB disk
(`/dev/nvme1n1` on this box) rather than on the root volume.

### Identify the device first

```sh
lsblk -f
```

Confirm `nvme1n1` is the size you expect and is **not** the root volume — on
EC2 the root is normally `nvme0n1`. Device numbering is not guaranteed stable
across reboots, which is why the fstab entry below uses a UUID rather than the
device name.

Also work out which kind of volume it is, because it changes what happens when
the instance stops:

```sh
sudo nvme id-ctrl -v /dev/nvme1n1 | grep -i '^mn'
```

- **Amazon Elastic Block Store** — persists across stop/start. Format once.
- **Amazon EC2 NVMe Instance Storage** — *ephemeral*. Every stop/start gives you
  a blank disk: the filesystem, the Chromium checkout and the build output are
  all gone and step 2 onward has to be redone. Fine for a scratch build box,
  but do not keep anything you care about there.

### Format — only if it is empty

```sh
sudo file -s /dev/nvme1n1
```

Output of exactly `/dev/nvme1n1: data` means there is no filesystem. Anything
mentioning a filesystem (e.g. `ext4 filesystem data`) means it is **already
formatted — skip this step**, or you will destroy the contents.

```sh
sudo mkfs.ext4 -m 0 -L chromium /dev/nvme1n1
```

`-m 0` drops the 5% root reserve, which is pointless on a data volume and worth
~10 GB here.

### Mount and take ownership

```sh
sudo mkdir -p /mnt/chromium-hdd
sudo mount /dev/nvme1n1 /mnt/chromium-hdd
sudo chown -R "$USER:$USER" /mnt/chromium-hdd
df -h /mnt/chromium-hdd
```

The `chown` matters: `fetch` and `gclient` run as your user, not root.

### Persist across reboot

Mount by UUID, never by device name:

```sh
sudo blkid /dev/nvme1n1        # note the UUID
echo "UUID=<uuid>  /mnt/chromium-hdd  ext4  defaults,nofail,discard  0  2" \
  | sudo tee -a /etc/fstab
sudo mount -a                  # must succeed with no output before you reboot
```

`nofail` is not optional. Without it, a missing or reformatted volume — which is
the normal state after an instance-store stop/start — leaves the machine hung at
boot waiting for a disk that will never appear.

Run `sudo mount -a` and confirm it is silent before rebooting. A typo in fstab
that you only discover on reboot means console recovery.

---

## 1. depot_tools

Safe to re-run: clones if missing, updates if already there.

```sh
if [ -d "$HOME/depot_tools/.git" ]; then
  git -C "$HOME/depot_tools" pull --ff-only
else
  git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git \
    "$HOME/depot_tools"
fi

# This shell, plus persist it once rather than appending on every re-run.
export PATH="$HOME/depot_tools:$PATH"
grep -qs 'depot_tools' "$HOME/.bashrc" \
  || echo 'export PATH="$HOME/depot_tools:$PATH"' >> "$HOME/.bashrc"

which gclient gn ninja      # all three should resolve under ~/depot_tools
```

depot_tools must be **prepended** to `PATH`, not appended: it ships wrappers for
`python3`, `ninja` and `gn` that have to win over any system copies. The final
`which` is the check that they do.

It also self-updates whenever you invoke one of its tools, so the explicit
`pull` is belt-and-braces — useful mainly when it has been sitting unused for
months, or when `DEPOT_TOOLS_UPDATE=0` is set in the environment.

---

## 2. Fetch Chromium

Also safe to re-run — `fetch` refuses to run in a directory that already has a
checkout, so guard it:

```sh
mkdir -p /mnt/chromium-hdd/chromium && cd /mnt/chromium-hdd/chromium

if [ -d src/.git ]; then
  echo "checkout already present, syncing instead"
  cd src && git rebase-update && gclient sync -D
else
  fetch --nohooks --no-history chromium
  cd src
fi

./build/install-build-deps.sh
gclient runhooks
```

`--no-history` saves a lot of time and disk and is fine here — we never need to
walk Chromium's history. (Watch the spelling: `--no-hisotry` is a typo that
`fetch` rejects.)

`--nohooks` is what makes the ordering above correct: it defers the hooks so
`install-build-deps.sh` can install system packages *first*, and only then does
`gclient runhooks` download the toolchains and run the generators. Running hooks
before the deps are in place is the usual cause of a confusing first-fetch
failure.

If you are re-running against an existing checkout that already has the patch
applied, **revert it first** (see step 7); syncing over a patched tree either
conflicts or silently discards the edits.

### Pin a milestone, and write it down

**This is the step that decides how much pain a rebuild costs.** The patch finds
its edit sites by matching verbatim substrings of Chromium source. Those anchors
drift as upstream moves. Building the same milestone as last time means the
anchors resolve unchanged; building tip-of-tree may mean relocating a few.

```sh
git checkout -b build-153 refs/tags/153.0.7995.0
gclient sync -D --force --reset
```

Record the tag you used next to the binary. The UA string in your launch flags
should claim a version that plausibly matches it — see step 6.

---

## 3. Apply the patch

```sh
cd ~/env-chromium-patch
git pull

python3 apply.py --chromium-src /mnt/chromium-hdd/chromium/src --dry-run
python3 apply.py --chromium-src /mnt/chromium-hdd/chromium/src \
                 --emit-patch env-fp-$(date +%Y%m%d).patch
```

Always `--dry-run` first. The applier is two-phase: if any anchor fails to
resolve it writes **nothing** and names the file plus the `edits/*.py` module to
fix. That is the expected failure when you have moved milestones; see
Troubleshooting.

It prints the `base/` API flavours it detected, e.g.

```
Detected APIs: GetVar=optional, values=base::DictValue,
               JSONReader::ReadDict=yes, utf8=String::FromUtf8
```

Those are sniffed out of the tree, not assumed. If a line looks wrong for the
milestone you pinned, stop and investigate before building.

---

## 4. Configure and build

```sh
cd /mnt/chromium-hdd/chromium/src
gn gen out/Release --args='
is_debug=false
is_component_build=false
symbol_level=0
blink_symbol_level=0
dcheck_always_on=false
proprietary_codecs=true
ffmpeg_branding="Chrome"
enable_library_cdms=true
enable_widevine=true
'
autoninja -C out/Release -j90
```

`-j90` is a manual override for this box; `autoninja` otherwise picks a job
count itself. Note that with **no target named**, ninja builds everything GN
generated — around 94,000 steps including every test binary. For just the
browser:

```sh
autoninja -C out/Release -j90 chrome
```

`proprietary_codecs` + `ffmpeg_branding="Chrome"` give H.264/AAC.
`enable_library_cdms` + `enable_widevine` add Widevine key-system support —
both are real `declare_args`, and Widevine's own comment confirms it "can be
optionally enabled in Chromium on non-Android platforms".

Set `is_component_build = true` instead while iterating on the patch — relinking
is far faster, at the cost of many `.so` files rather than one binary.

### Why a rebuild costs 20–30 min rather than seconds

The patch appends to `third_party/blink/public/common/switches.h`. That header
is included across Blink, `content/` and `chrome/`, so touching it invalidates a
large fraction of the tree — which is the price of putting the config reader
somewhere both the browser and the renderer can already see. The refresh clock
edits `components/viz/common/frame_sinks/delay_based_time_source.h`, which is
narrower but still widely included.

So batch patch changes rather than applying them one at a time. And remember
that changing fingerprint *values* needs no rebuild at all — only changing the
patch source does.

### Two args to check before pasting

GN **hard-errors** on an argument it does not recognise ("Build argument has no
effect"), so a stale name fails the whole `gn gen`. Two from earlier versions of
this command did not appear anywhere I looked in current Chromium:

- **`remove_webcore_debug_symbols`** — its historical home was
  `third_party/blink/renderer/config.gni`, which now declares
  `blink_symbol_level` instead, and it is absent from
  `build/config/compiler/{BUILD.gn,compiler.gni}`. It was superseded by
  `blink_symbol_level`, which the command already sets to `0`. Dropped above.
- **`enable_linux_installer`** — not found in `chrome/installer/linux/BUILD.gn`,
  `chrome/BUILD.gn`, `build/config/features.gni` or
  `build/config/chrome_build.gni`. The Linux packaging targets are generated
  unconditionally; you build them by name rather than enabling them:
  `autoninja -C out/Release "chrome/installer/linux:unstable_deb"`. Dropped
  above.

Confirm against your own tree rather than taking my word — this is authoritative
and takes a second:

```sh
gn args out/Release --list --short | grep -E 'remove_webcore_debug_symbols|enable_linux_installer|blink_symbol_level'
```

If a name prints, it exists on that milestone and you can add it back.

To run the refresh clock's unit tests as well:

```sh
autoninja -C out/Release viz_unittests
./out/Release/viz_unittests --gtest_filter='RefreshClock*'
```

Record the built version for step 6:

```sh
./out/Release/chrome --version
```

---

## 5. Sanity check before trusting it

Launch with **no** overrides set and confirm the browser reports stock values
everywhere. The patch must be inert by default; if an unconfigured build already
looks spoofed, something is wrong.

```sh
Xvfb :99 -screen 0 1920x1080x24 &
python3 ~/env-chromium-patch/test/serve.py &        # http://localhost:8000
```

Load `http://localhost:8000/fingerprint.html` and check the consistency panel.
`serve.py` also prints the outgoing `User-Agent` and `Sec-CH-UA*` headers, which
is the half that JavaScript cannot show you.

---

## 6. Run it

The full launch command lives in the main README. The parts that matter here:

- Point `--env-fp-ua` at a Chrome version consistent with what you built
  (step 4). The patch rewrites `Sec-CH-UA` to match whatever the UA claims, so
  those stay in agreement automatically — but a version no channel ever shipped
  is its own signal.
- Keep the identity flags coherent as a set. A Windows UA with a Linux
  `navigator.platform`, or an NVIDIA D3D11 renderer string on a Linux profile,
  is more identifying than not spoofing at all.
- The refresh clock is **off** unless `CHROME_ENV_REFRESH_CLOCK=1` (or
  `--enable-human-refresh-clock`). Read `docs/refresh-clock.md` before enabling
  it; it documents where the model is known to break.

---

## 7. Updating Chromium later

**Revert before syncing.** The applier keeps `.env-fp.orig` backups beside every
file it touched. `gclient sync` over a patched tree either conflicts or silently
discards the edits.

```sh
NEW_TAG=154.0.8000.0        # the milestone you are moving to

python3 ~/env-chromium-patch/apply.py \
  --chromium-src /mnt/chromium-hdd/chromium/src --revert

cd /mnt/chromium-hdd/chromium/src
git fetch --tags
git checkout -b "build-${NEW_TAG}" "refs/tags/${NEW_TAG}"
gclient sync -D --force --reset
# then step 3 again
```

---

## Troubleshooting

**"N anchor(s) did not resolve. Nothing was written."**
Upstream moved the code an anchor was quoting. The message names the file, the
purpose of the edit, and the first line of the search string. Open that file in
the checkout, find the equivalent code, and update the anchor in the named
`edits/*.py`. Nothing was written, so the tree is still clean.

**A `base/` or WTF API changed shape.**
Two of these have already bitten us:

- `base::Environment::GetVar` now takes `base::cstring_view`, which has no
  `const char*` conversion.
- `WTF::String::FromUTF8` was renamed to `FromUtf8`, with a byte-span overload.

Both are handled by probes in `detect_api_flavors()` plus rewriters in
`edits/__init__.py`. If a third one appears, add a probe and a rewriter the same
way rather than hardcoding a spelling — and remember to rewrite the idempotency
**marker** as well as the replacement, or reruns will double-apply.

**Build succeeds but the overrides do nothing.**
Check which process the surface lives in. WebGL, WebGPU and the navigator
properties are evaluated in the **renderer**; the browser forwards the
`--env-fp-*` switches to child processes. Environment variables need no such
plumbing, which is why they are the more reliable channel. For the refresh
clock, look for the `Human Refresh Clock` banner in stderr — no banner means no
`DelayBasedTimeSource` was constructed and nothing is being modelled (headless
with `--enable-begin-frame-control`, or `--disable-frame-rate-limit`, select a
different BeginFrameSource entirely).

**Re-running the applier says "already applied" but the code looks wrong.**
Delete `edits/__pycache__`. A stale bytecode cache can emit the previous
version's generated code.
