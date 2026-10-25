#!/usr/bin/env bash
# Assemble a ready-to-copy Windows bundle for one issued client.
# Scripts come from client/windows (tracked), credentials from clients/
# (gitignored), binaries from a local cache that is never committed.
set -euo pipefail

NODE="${1:?Usage: build-win-bundle.sh <node> <client-name>}"
CLIENT="${2:?Usage: build-win-bundle.sh <node> <client-name>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$ROOT/client/windows"
BINARIES="${VPN_WIN_BINARIES:-$ROOT/clients/.win-binaries}"
CONFIG="$ROOT/clients/$NODE-$CLIENT.json"
DEST="$ROOT/clients/windows-bundle-$CLIENT"

[[ "$NODE" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || {
  echo "ERROR: unsafe node name" >&2
  exit 2
}
# The Windows scripts locate the config by the *-win.json glob, so a bundle
# built for a client without that suffix would never start.
[[ "$CLIENT" =~ ^[a-z0-9][a-z0-9-]{0,62}-win$ ]] || {
  echo "ERROR: client name must match <name>-win (e.g. laptop-win)" >&2
  exit 2
}
[[ -f "$CONFIG" ]] || {
  echo "ERROR: client config not found: $CONFIG (run: make export NODE=$NODE)" >&2
  exit 2
}

for binary in sing-box.exe libcronet.dll; do
  [[ -f "$BINARIES/$binary" ]] || {
    echo "ERROR: missing $binary in $BINARIES" >&2
    echo "Put the pinned Windows sing-box release there (see client/windows/README.txt)." >&2
    exit 2
  }
done

# Validate what will actually run on Windows: run-vpn.ps1 rewrites the selector
# and the urltest group before starting sing-box, so check the rewritten form.
if command -v sing-box >/dev/null; then
  patched="$(mktemp "${TMPDIR:-/tmp}/win-bundle.XXXXXX.json")"
  trap 'rm -f "$patched"' EXIT
  python3 - "$CONFIG" "$patched" <<'PY'
import json
import sys

source, target = sys.argv[1], sys.argv[2]
with open(source) as handle:
    config = json.load(handle)

for outbound in config["outbounds"]:
    if outbound.get("tag") == "proxy":
        outbound["default"] = "auto"
    if outbound.get("type") == "urltest":
        outbound["tolerance"] = 3000
        outbound["interval"] = "1m"
        outbound["idle_timeout"] = "30m"
        outbound["interrupt_exist_connections"] = True

with open(target, "w") as handle:
    json.dump(config, handle, indent=2)
PY
  sing-box check -c "$patched"
  rm -f "$patched"
  trap - EXIT
else
  echo "WARN: sing-box not found locally, skipping config check" >&2
fi

mkdir -p "$DEST"
chmod 700 "$DEST"
find "$DEST" -maxdepth 1 -name '*-win.json' -delete
cp "$SOURCE"/* "$DEST/"
cp "$BINARIES"/sing-box.exe "$BINARIES"/libcronet.dll "$DEST/"
install -m 600 "$CONFIG" "$DEST/$NODE-$CLIENT.json"

echo "Bundle ready: $DEST"
echo "Copy the whole folder to the Windows machine and run INSTALL-VPN.cmd."
