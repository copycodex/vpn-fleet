#!/usr/bin/env bash
# Atomically append one management-WireGuard peer to encrypted SOPS data.
# Server keypair is created on first call. Plaintext flows only through pipes;
# the sole temporary file is already SOPS-encrypted.
set -euo pipefail

NODE="${1:?Usage: add-wg-peer.sh <node> <peer-name>}"
PEER_NAME="${2:?Usage: add-wg-peer.sh <node> <peer-name>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS="${VPN_FLEET_SECRETS_FILE:-$ROOT/private/inventories/group_vars/all/secrets.sops.yml}"
SOPS_FILENAME="${VPN_FLEET_SOPS_FILENAME_OVERRIDE:-$ROOT/private/inventories/group_vars/all/secrets.sops.yml}"
SOPS_CONFIG="${VPN_FLEET_SOPS_CONFIG:-$ROOT/private/.sops.yaml}"

[[ "$NODE" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || {
  echo "ERROR: unsafe node name" >&2
  exit 2
}
[[ "$PEER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || {
  echo "ERROR: unsafe peer name" >&2
  exit 2
}
[[ -f "$SECRETS" && ! -L "$SECRETS" ]] || {
  echo "ERROR: SOPS secrets file is missing or is a symlink" >&2
  exit 2
}

for command_name in flock jq sops wg; do
  command -v "$command_name" >/dev/null || {
    echo "ERROR: required command not found: $command_name" >&2
    exit 2
  }
done

umask 077
if [[ "${VPN_FLEET_SECRETS_LOCK_HELD:-}" != 1 ]]; then
  export VPN_FLEET_SECRETS_LOCK_HELD=1
  exec python3 "$ROOT/scripts/secrets_lock.py" "$SECRETS" -- "$0" "$@"
fi

node_key="node_${NODE//-/_}_wg"

server_private_key="$(wg genkey)"
server_public_key="$(printf '%s' "$server_private_key" | wg pubkey)"
peer_private_key="$(wg genkey)"
peer_public_key="$(printf '%s' "$peer_private_key" | wg pubkey)"

encrypted_tmp_dir="$(mktemp -d)"
encrypted_tmp="$encrypted_tmp_dir/secrets.sops.yml"
trap 'rm -rf "$encrypted_tmp_dir"' EXIT

# Server keys are consumed only when the node has none yet; jq keeps the
# existing pair otherwise, so re-running never rotates the server identity.
sops --decrypt --output-type json "$SECRETS" |
  jq \
    --arg key "$node_key" \
    --arg name "$PEER_NAME" \
    --arg server_private "$server_private_key" \
    --arg server_public "$server_public_key" \
    --arg peer_private "$peer_private_key" \
    --arg peer_public "$peer_public_key" '
    def node_wg:
      (.[$key] // {"server_private_key": $server_private,
                   "server_public_key": $server_public,
                   "peers": []});
    node_wg as $wg |
    if ($wg.peers | type) != "array" then
      error("wg peer list is not an array")
    elif any($wg.peers[]; .name == $name) then
      error("wg peer name already exists")
    else
      ([$wg.peers[].address | split(".")[3] | tonumber] | max // 1) as $last |
      if $last >= 254 then error("wg subnet exhausted") else . end |
      .[$key] = ($wg | .peers += [{
        "name": $name,
        "private_key": $peer_private,
        "public_key": $peer_public,
        "address": ("10.88.0." + (($last + 1) | tostring))
      }])
    end
  ' |
  sops --encrypt --input-type json --output-type yaml \
    --config "$SOPS_CONFIG" --filename-override "$SOPS_FILENAME" \
    /dev/stdin >"$encrypted_tmp"

# Verify MAC/decryptability before the atomic replacement. Plaintext is discarded.
sops --decrypt "$encrypted_tmp" >/dev/null
chmod --reference="$SECRETS" "$encrypted_tmp"
mv "$encrypted_tmp" "$SECRETS"

printf 'WireGuard peer %s added to %s (keys hidden)\n' "$PEER_NAME" "$NODE"
