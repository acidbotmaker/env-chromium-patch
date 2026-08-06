#!/usr/bin/env bash
# Launch the patched Chromium with a fingerprint profile.
#
#   ./run.sh profiles/win-nvidia.env
#   ./run.sh profiles/mac-m1.env https://example.com
#
# Values are read by Chrome at startup, so switching profiles just means
# relaunching with a different file -- no rebuild. Each profile gets its own
# --user-data-dir so two profiles can run side by side without sharing
# cookies, storage or permission grants.
#
# Override the binary with CHROME_BIN=... if it is not where this expects.

set -euo pipefail

profile="${1:-}"
if [[ -z "$profile" ]]; then
  echo "usage: $0 <profile.env> [chrome args...]" >&2
  echo >&2
  echo "available profiles:" >&2
  for candidate in "$(dirname "$0")"/profiles/*.env "$(dirname "$0")"/env.example; do
    [[ -e "$candidate" ]] && echo "  $candidate" >&2
  done
  exit 2
fi
shift

if [[ ! -f "$profile" ]]; then
  echo "error: no such profile: $profile" >&2
  exit 1
fi

default_bin="$HOME/chromium/src/out/Release/chrome"
case "$(uname -s)" in
  Darwin) default_bin="$HOME/chromium/src/out/Release/Chromium.app/Contents/MacOS/Chromium" ;;
esac
chrome_bin="${CHROME_BIN:-$default_bin}"

if [[ ! -x "$chrome_bin" ]]; then
  echo "error: chrome binary not found or not executable: $chrome_bin" >&2
  echo "       set CHROME_BIN to override" >&2
  exit 1
fi

# Keep each profile's browsing state separate. Permission grants matter here:
# media device labels stay hidden until the profile has been granted camera and
# microphone access, so sharing a data dir would leak state between profiles.
profile_name="$(basename "$profile" .env)"
tmp_base="${TMPDIR:-/tmp}"
user_data_dir="${CHROME_USER_DATA_DIR:-${tmp_base%/}/env-fp-$profile_name}"

set -a
# shellcheck disable=SC1090
source "$profile"
set +a

echo "profile:   $profile"
echo "binary:    $chrome_bin"
echo "data dir:  $user_data_dir"
echo "overrides:"
while IFS= read -r name; do
  value="${!name}"
  if [[ ${#value} -gt 70 ]]; then
    printf '  %-42s %s...\n' "$name" "${value:0:70}"
  else
    printf '  %-42s %s\n' "$name" "$value"
  fi
done < <(compgen -v | grep '^CHROME_ENV_' | sort)
echo

exec "$chrome_bin" --user-data-dir="$user_data_dir" "$@"
