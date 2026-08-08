#!/usr/bin/env bash
#
# One-shot setup for a fresh Ubuntu EC2 box: mount the build volume, install
# dependencies, fetch and pin Chromium, apply the patch, and build it.
#
#   git clone https://github.com/acidbotmaker/env-chromium-patch.git
#   cd env-chromium-patch
#   ./00_setup/setup.sh
#
# Every phase is idempotent, so re-running after a failure picks up where it
# left off rather than starting over. Budget ~3h end to end on the reference
# box: roughly an hour to fetch and 1h48m to build at -j90 on a 250G volume.
# Run it under tmux.
#
# Configuration -- override by exporting before running, e.g.
#   CHROMIUM_TAG=153.0.7995.0 JOBS=90 ./00_setup/setup.sh
#
#   DEVICE        block device for the build volume, "" to skip the disk phase
#   MOUNT         where it gets mounted
#   FORMAT_DISK   "yes" to allow mkfs on an EMPTY device (destructive; see below)
#   CHROMIUM_TAG  milestone to pin, e.g. 153.0.7995.0. Empty = tip of tree
#   JOBS          ninja parallelism
#   BUILD_TARGET  ninja target; empty builds everything (~94k steps)
#   SKIP_BUILD    "yes" to stop after gn gen
#
set -euo pipefail

DEVICE="${DEVICE:-/dev/nvme1n1}"
MOUNT="${MOUNT:-/mnt/chromium-hdd}"
FORMAT_DISK="${FORMAT_DISK:-no}"
CHROMIUM_TAG="${CHROMIUM_TAG:-}"
JOBS="${JOBS:-90}"
BUILD_TARGET="${BUILD_TARGET:-}"
SKIP_BUILD="${SKIP_BUILD:-no}"

DEPOT_TOOLS="$HOME/depot_tools"
CHROMIUM_ROOT="$MOUNT/chromium"
SRC="$CHROMIUM_ROOT/src"
OUT="out/Release"
PATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Wrapped so the disk guard can be exercised by 00_setup/test-disk-guard.sh.
is_block_device() { [ -b "$1" ]; }

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    warning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m!!  %s\033[0m\n' "$*" >&2; exit 1; }

trap 'die "failed at line $LINENO. Nothing after this point ran; fix and re-run -- earlier phases will be skipped."' ERR

# ---------------------------------------------------------------- preflight

phase_preflight() {
  log "Preflight"

  [ "$(id -u)" -ne 0 ] || die "run as a normal user, not root. The checkout must \
be owned by the build user, and gclient misbehaves under root. sudo is used \
only where it is actually needed."

  sudo -v || die "passwordless sudo (or a sudo password) is required"

  command -v python3 >/dev/null || die "python3 is missing; depot_tools needs it"

  if [ ! -t 0 ]; then
    info "non-interactive"
  elif [ -z "${TMUX:-}" ] && [ -z "${STY:-}" ]; then
    warn "not inside tmux/screen. This runs for hours; an SSH drop will kill it."
    warn "Ctrl-C now and re-run under: tmux new -s build"
    sleep 5
  fi

  info "patch repo:   $PATCH_DIR"
  info "chromium:     $SRC"
  info "milestone:    ${CHROMIUM_TAG:-<tip of tree>}"
  info "jobs:         $JOBS"
  [ -n "$CHROMIUM_TAG" ] || warn "no CHROMIUM_TAG set. Tip of tree moves, and the \
patch matches Chromium source verbatim, so anchors may need relocating. Pinning \
a tag makes rebuilds reproducible."
}

# --------------------------------------------------------------- packages

phase_packages() {
  log "Installing bootstrap packages"
  sudo bash "$PATCH_DIR/00_setup/00_setup-chromium-repo.sh"

  # Not in that script, and each fails late and confusingly if absent.
  sudo apt-get install -y --no-install-recommends \
    python3 python3-venv lsb-release nvme-cli e2fsprogs xvfb
}

# ------------------------------------------------------------------- disk

phase_disk() {
  if [ -z "$DEVICE" ]; then
    log "Disk phase skipped (DEVICE empty); building on the root filesystem"
    sudo mkdir -p "$MOUNT"
    sudo chown -R "$USER:$USER" "$MOUNT"
    return
  fi

  log "Preparing build volume $DEVICE -> $MOUNT"

  is_block_device "$DEVICE" || die "$DEVICE is not a block device. Run 'lsblk -f' \
and set DEVICE=, or DEVICE='' to build on the root disk."

  if findmnt -rn --target "$MOUNT" --source "$DEVICE" >/dev/null 2>&1; then
    info "already mounted"
  else
    # A device with no filesystem reads exactly "<dev>: data".
    local probe
    probe="$(sudo file -s "$DEVICE")"
    info "$probe"

    if [[ "$probe" == *": data" ]]; then
      if [ "$FORMAT_DISK" != "yes" ]; then
        die "$DEVICE has no filesystem. Formatting ERASES IT, so it is opt-in:
    confirm the device is the right one with 'lsblk -f', then re-run with
    FORMAT_DISK=yes ./00_setup/setup.sh"
      fi
      # Refuse if it is carrying the root filesystem, whatever the probe said.
      if findmnt -rn --source "$DEVICE" >/dev/null 2>&1; then
        die "$DEVICE is mounted somewhere; refusing to format"
      fi
      warn "formatting $DEVICE as ext4 in 5s -- Ctrl-C to abort"
      sleep 5
      sudo mkfs.ext4 -m 0 -L chromium "$DEVICE"
    else
      info "existing filesystem found; mounting as-is (not formatting)"
    fi

    sudo mkdir -p "$MOUNT"
    sudo mount "$DEVICE" "$MOUNT"
  fi

  sudo chown -R "$USER:$USER" "$MOUNT"

  # Persist by UUID: NVMe device numbering is not stable across reboots.
  local uuid
  uuid="$(sudo blkid -s UUID -o value "$DEVICE")"
  if [ -n "$uuid" ] && ! grep -q "$uuid" /etc/fstab; then
    # nofail matters: an instance-store volume is blank after every stop/start,
    # and without it the box hangs at boot waiting for a disk that never comes.
    echo "UUID=$uuid  $MOUNT  ext4  defaults,nofail,discard  0  2" \
      | sudo tee -a /etc/fstab >/dev/null
    sudo mount -a
    info "added to /etc/fstab (UUID=$uuid, nofail)"
  fi

  # The build box uses a 250G volume, which fits the checkout plus one release
  # build with room to spare. A fresh 250G ext4 reports ~246G available, so the
  # threshold sits just under that rather than at 250.
  local avail
  avail="$(df -BG --output=avail "$MOUNT" | tail -1 | tr -dc '0-9')"
  info "free space: ${avail}G"
  [ "${avail:-0}" -ge 240 ] || warn "under 240G free. The reference box uses a \
250G volume for the checkout plus one release build; less than that risks \
running out partway through a ~2h build."

  if sudo nvme id-ctrl "$DEVICE" 2>/dev/null | grep -qi 'Instance Storage'; then
    warn "$DEVICE is EC2 Instance Storage: EPHEMERAL. Everything here is lost on \
instance stop/start and this script must be re-run from scratch."
  fi
}

# ------------------------------------------------------------ depot_tools

phase_depot_tools() {
  log "depot_tools"

  if [ -d "$DEPOT_TOOLS/.git" ]; then
    info "updating"
    git -C "$DEPOT_TOOLS" pull --ff-only
  else
    git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git \
      "$DEPOT_TOOLS"
  fi

  # Must be PREPENDED: depot_tools ships python3/ninja/gn wrappers that have to
  # win over the system copies.
  export PATH="$DEPOT_TOOLS:$PATH"
  grep -qs 'depot_tools' "$HOME/.bashrc" \
    || echo 'export PATH="$HOME/depot_tools:$PATH"' >> "$HOME/.bashrc"

  command -v gclient >/dev/null || die "gclient not on PATH after install"
  info "gclient: $(command -v gclient)"
}

# --------------------------------------------------------------- chromium

phase_fetch() {
  log "Fetching Chromium"
  mkdir -p "$CHROMIUM_ROOT"
  cd "$CHROMIUM_ROOT"

  if [ -d "$SRC/.git" ]; then
    info "checkout already present, skipping fetch"
  else
    # --nohooks defers the hooks so install-build-deps can put the system
    # packages in place first; running hooks before that is the classic
    # first-fetch failure.
    fetch --nohooks --no-history chromium
  fi

  cd "$SRC"
  ./build/install-build-deps.sh --no-prompt

  if [ -n "$CHROMIUM_TAG" ]; then
    local branch="build-$CHROMIUM_TAG"
    if git rev-parse --verify --quiet "$branch" >/dev/null; then
      info "already on a $branch branch"
      git checkout "$branch"
    else
      git fetch --tags --depth 1 origin "refs/tags/$CHROMIUM_TAG" 2>/dev/null || true
      git checkout -b "$branch" "refs/tags/$CHROMIUM_TAG"
    fi
    gclient sync -D --force --reset
  else
    gclient runhooks
  fi
}

# ------------------------------------------------------------------ patch

phase_patch() {
  log "Applying the patch"
  cd "$PATCH_DIR"

  # Two-phase: if any anchor fails to resolve it writes nothing and names the
  # file plus the edits/*.py module to fix.
  python3 apply.py --chromium-src "$SRC" \
    --emit-patch "env-fp-$(date +%Y%m%d-%H%M%S).patch"
}

# ------------------------------------------------------------------ build

phase_build() {
  log "Configuring"
  cd "$SRC"

  gn gen "$OUT" --args='
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

  if [ "$SKIP_BUILD" = "yes" ]; then
    info "SKIP_BUILD=yes, stopping before ninja"
    return
  fi

  log "Building (this is the long part)"
  # No target named = everything GN generated, ~94k steps. Set BUILD_TARGET
  # to 'chrome' for just the browser.
  if [ -n "$BUILD_TARGET" ]; then
    autoninja -C "$OUT" -j"$JOBS" "$BUILD_TARGET"
  else
    autoninja -C "$OUT" -j"$JOBS"
  fi
}

# ----------------------------------------------------------------- verify

phase_verify() {
  log "Done"
  local bin="$SRC/$OUT/chrome"
  if [ -x "$bin" ]; then
    info "binary:  $bin"
    info "version: $("$bin" --version 2>/dev/null || echo '(could not run)')"
  else
    warn "no chrome binary at $bin"
    return
  fi

  cat <<EOF

    Next:
      1. Sanity check with NO overrides set -- the build must be inert by
         default. Anything already looking spoofed means something is wrong.

           Xvfb :99 -screen 0 1920x1080x24 &
           python3 $PATCH_DIR/test/serve.py &
           $bin --headless=new --no-sandbox --screenshot=/tmp/t.png \\
             http://localhost:8000/fingerprint.html

      2. Point the UA at a version consistent with the build above, then use
         the launch command in $PATCH_DIR/README.md.

      3. The refresh clock is OFF unless CHROME_ENV_REFRESH_CLOCK=1. Read
         $PATCH_DIR/docs/refresh-clock.md first -- it records where it breaks.

EOF
}

main() {
  phase_preflight
  phase_packages
  phase_disk
  phase_depot_tools
  phase_fetch
  phase_patch
  phase_build
  phase_verify
}

# Sourced by the guard test, which needs the functions without running them.
if [ "${SETUP_SH_SOURCE_ONLY:-}" != "1" ]; then
  main "$@"
fi
