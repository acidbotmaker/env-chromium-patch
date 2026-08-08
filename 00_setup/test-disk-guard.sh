#!/usr/bin/env bash
#
# Exercises the destructive path in setup.sh's disk phase without touching a
# real disk. mkfs is the one command in that script that can lose data, so the
# conditions under which it runs are worth testing rather than eyeballing.
#
#   ./00_setup/test-disk-guard.sh
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
FAIL=0

# Runs phase_disk against a fake device. Echoes a line for each privileged
# command it would have run, so the assertions can look for "mkfs".
run_case() {
  # Prefixed because bash scoping is dynamic: phase_disk declares its own
  # `local probe`, which would shadow a same-named fixture and leave it unset
  # under `set -u` by the time the sudo stub reads it.
  local FAKE_PROBE="$1" FAKE_FORMAT="$2" FAKE_MOUNTED="$3"
  (
    export SETUP_SH_SOURCE_ONLY=1
    # shellcheck disable=SC1091
    source "$HERE/setup.sh"

    DEVICE=/dev/fake0
    MOUNT=/tmp/fake-mount
    FORMAT_DISK="$FAKE_FORMAT"

    is_block_device() { return 0; }
    findmnt() {
      # --target form is the "already mounted at MOUNT" probe; the bare
      # --source form is the "mounted anywhere" safety check.
      if [ "$FAKE_MOUNTED" = "yes" ]; then return 0; fi
      return 1
    }
    sudo() {
      case "$1" in
        file)   echo "/dev/fake0: $FAKE_PROBE" ;;
        mkfs.ext4) echo "WOULD-MKFS $*" ;;
        mount)  echo "WOULD-MOUNT $*" ;;
        mkdir|chown|tee) : ;;
        blkid)  echo "" ;;         # no UUID -> skips the fstab edit
        nvme)   return 1 ;;
        df)     echo "500G" ;;
        *)      : ;;
      esac
    }
    df() { echo "Avail"; echo "500G"; }
    grep() { command grep "$@"; }
    sleep() { : ; }

    phase_disk 2>&1
  )
}

assert() {
  local label="$1" haystack="$2" needle="$3" want="$4"  # want = yes|no
  local found=no
  [[ "$haystack" == *"$needle"* ]] && found=yes
  if [ "$found" = "$want" ]; then
    printf '  \033[32mPASS\033[0m  %s\n' "$label"; PASS=$((PASS + 1))
  else
    printf '  \033[31mFAIL\033[0m  %s (expected %s to be %s)\n' \
      "$label" "$needle" "$want"; FAIL=$((FAIL + 1))
    printf '        output: %s\n' "${haystack//$'\n'/ | }"
  fi
}

echo "disk guard"

out="$(run_case "data" "no" "no")"
assert "blank disk + FORMAT_DISK=no  -> refuses"      "$out" "opt-in" yes
assert "blank disk + FORMAT_DISK=no  -> no mkfs"      "$out" "WOULD-MKFS" no

out="$(run_case "data" "yes" "no")"
assert "blank disk + FORMAT_DISK=yes -> formats"      "$out" "WOULD-MKFS" yes

out="$(run_case "Linux rev 1.0 ext4 filesystem data" "yes" "no")"
assert "existing fs + FORMAT_DISK=yes -> NO mkfs"     "$out" "WOULD-MKFS" no
assert "existing fs -> mounts as-is"                  "$out" "WOULD-MOUNT" yes

out="$(run_case "data" "yes" "yes")"
assert "already mounted -> no mkfs"                   "$out" "WOULD-MKFS" no
assert "already mounted -> no remount"                "$out" "WOULD-MOUNT" no

echo
if [ "$FAIL" -eq 0 ]; then
  echo "all $PASS checks passed"
else
  echo "$FAIL of $((PASS + FAIL)) checks failed"
  exit 1
fi
