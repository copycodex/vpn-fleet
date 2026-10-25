#!/usr/bin/env bash
# Atomically append one client to encrypted SOPS data. Plaintext flows only
# through pipes; the sole temporary file is already SOPS-encrypted.
set -euo pipefail

NODE="${1:?Usage: add-client.sh <node> <client-name>}"
CLIENT_NAME="${2:?Usage: add-client.sh <node> <client-name>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS="${VPN_FLEET_SECRETS_FILE:-$ROOT/private/inventories/group_vars/all/secrets.sops.yml}"
SOPS_FILENAME="${VPN_FLEET_SOPS_FILENAME_OVERRIDE:-$ROOT/private/inventories/group_vars/all/secrets.sops.yml}"
SOPS_CONFIG="${VPN_FLEET_SOPS_CONFIG:-$ROOT/private/.sops.yaml}"
INVENTORY="${VPN_FLEET_INVENTORY_FILE:-$ROOT/private/inventories/nodes/$NODE.yml}"

[[ "$NODE" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || {
  echo "ERROR: unsafe node name" >&2
  exit 2
}
[[ "$CLIENT_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || {
  echo "ERROR: unsafe client name" >&2
  exit 2
}
[[ -f "$SECRETS" && ! -L "$SECRETS" ]] || {
  echo "ERROR: SOPS secrets file is missing or is a symlink" >&2
  exit 2
}
[[ -f "$INVENTORY" && ! -L "$INVENTORY" ]] || {
  echo "ERROR: node inventory is missing or is a symlink" >&2
  exit 2
}

for command_name in flock jq python3 sops uuidgen; do
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

node_key="node_${NODE//-/_}_clients"
encrypted_tmp_dir="$(mktemp -d)"
encrypted_tmp="$encrypted_tmp_dir/secrets.sops.yml"
encrypted_backup="$encrypted_tmp_dir/secrets.backup.sops.yml"
inventory_tmp="$encrypted_tmp_dir/inventory.yml"
uuid_tmp="$encrypted_tmp_dir/client.uuid"
trap 'rm -rf "$encrypted_tmp_dir"' EXIT
uuidgen | tr -d '\r\n' | tr '[:upper:]' '[:lower:]' >"$uuid_tmp"

# Prepare the non-secret inventory update without rewriting comments/formatting.
python3 - "$INVENTORY" "$inventory_tmp" "$CLIENT_NAME" <<'PY'
import pathlib
import re
import sys

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
client_name = sys.argv[3]
lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
markers = [i for i, line in enumerate(lines) if line == "      singbox_client_names:\n"]
if len(markers) != 1:
    raise SystemExit("ERROR: inventory must contain exactly one singbox_client_names list")

insert_at = markers[0] + 1
names = []
while insert_at < len(lines):
    match = re.fullmatch(r"        - ([A-Za-z0-9][A-Za-z0-9_-]{0,63})\n", lines[insert_at])
    if not match:
        break
    names.append(match.group(1))
    insert_at += 1

if client_name in names:
    raise SystemExit("ERROR: client name already exists in inventory")
lines.insert(insert_at, f"        - {client_name}\n")
destination.write_text("".join(lines), encoding="utf-8")
PY

sops --decrypt --output-type json "$SECRETS" |
  jq --arg key "$node_key" --arg name "$CLIENT_NAME" --rawfile uuid "$uuid_tmp" '
    if (has($key) | not) then
      error("client list for node does not exist")
    elif (.[$key] | type) != "array" then
      error("client list is not an array")
    elif any(.[$key][]; .name == $name) then
      error("client name already exists")
    elif ([to_entries[] |
           select(.key | test("^node_.*_clients$")) |
           .value[]? | .uuid] | index($uuid)) != null then
      error("generated UUID collision")
    else
      .[$key] += [{"name": $name, "uuid": $uuid}]
    end
  ' |
  sops --encrypt --input-type json --output-type yaml \
    --config "$SOPS_CONFIG" --filename-override "$SOPS_FILENAME" \
    /dev/stdin >"$encrypted_tmp"

# Verify MAC/decryptability before the atomic replacement. Plaintext is discarded.
sops --decrypt "$encrypted_tmp" >/dev/null
chmod --reference="$SECRETS" "$encrypted_tmp"
chmod --reference="$INVENTORY" "$inventory_tmp"
cp --preserve=mode "$SECRETS" "$encrypted_backup"

# Each rename is atomic. If the second rename fails, restore encrypted SOPS so
# the exact-equality invariant from the singbox role is not left half-updated.
mv "$encrypted_tmp" "$SECRETS"
if ! mv "$inventory_tmp" "$INVENTORY"; then
  mv "$encrypted_backup" "$SECRETS"
  echo "ERROR: inventory update failed; SOPS rolled back" >&2
  exit 1
fi

printf 'Client %s added to %s (credential hidden)\n' "$CLIENT_NAME" "$NODE"
