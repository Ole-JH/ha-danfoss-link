#!/usr/bin/env bash
# Copy the Danfoss Link app identity off a rooted phone or an Android emulator.
#
#   ./export_credentials.sh [output_dir]
#
# Needs adb and a device where root is available: an emulator with a
# "Google APIs" system image (Google *Play* images refuse `adb root`), or a
# rooted phone with `su`.
set -euo pipefail

PKG="com.danfoss.linkapp"
OUT="${1:-./danfoss_link_creds}"

command -v adb >/dev/null || { echo "adb not found (install Android platform-tools)"; exit 1; }

if [[ "$(adb devices | awk 'NR>1 && $2=="device"' | wc -l)" -ne 1 ]]; then
  echo "Connect exactly one device/emulator (adb devices shows:)"; adb devices; exit 1
fi

adb root >/dev/null 2>&1 || true
adb wait-for-device

# Prefer root shell; fall back to su on rooted phones.
run() {
  if adb shell id | grep -q "uid=0"; then adb shell "$1"; else adb shell "su -c '$1'"; fi
}

DIR=""
for d in "/data/data/${PKG}/files/luadata" "/data/data/${PKG}/files"; do
  if run "test -f $d/app_cert.pem" >/dev/null 2>&1; then DIR="$d"; break; fi
done
if [[ -z "$DIR" ]]; then
  echo "app_cert.pem not found. Is the app installed, paired, and is this device rooted?"
  exit 1
fi

mkdir -p "$OUT"
chmod 700 "$OUT"
for f in app_cert.pem app_key.pem; do
  run "cat $DIR/$f" | tr -d '\r' > "$OUT/$f"
done
chmod 600 "$OUT"/*.pem

PEERS="$(run "cat $DIR/pairings/* 2>/dev/null" | tr -d '\r' \
  | grep -oE '"peer_id"\]="[0-9a-fA-F:]+"' | sed -E 's/.*="([^"]+)"/\1/' | sort -u || true)"
if [[ -z "$PEERS" ]]; then
  # Pairing files are named after the peer ID with '_' instead of ':'.
  PEERS="$(run "ls $DIR/pairings 2>/dev/null" | tr -d '\r' | tr '_' ':' || true)"
fi
echo "$PEERS" > "$OUT/peer_id.txt"

echo
echo "Saved to $OUT:"
echo "  app_cert.pem   -> 'App certificate' in Home Assistant"
echo "  app_key.pem    -> 'App private key' (keep this secret)"
echo "  CC peer ID     -> $(echo "$PEERS" | head -1)"
[[ "$(echo "$PEERS" | grep -c .)" -gt 1 ]] && echo "  (several CCs paired; one entry per CC: $PEERS)"
echo
echo "Now close the Danfoss Link app on this device: only one client per identity."
