#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$CONFIG_HOME/systemd/user"
PROBE_DIR="$CONFIG_HOME/vpn-fleet/probe"
GENERATED_DIR="${1:-$SOURCE_DIR/../../clients/probe}"

[[ -d "$GENERATED_DIR" ]] || {
  echo "ERROR: $GENERATED_DIR отсутствует; сначала запусти make export NODE=<node>"
  exit 1
}

install -d -m0700 "$BIN_DIR" "$UNIT_DIR" "$PROBE_DIR"
install -m0700 "$SOURCE_DIR/vpn-probe.sh" "$BIN_DIR/vpn-probe"
install -m0600 "$GENERATED_DIR"/*.json "$GENERATED_DIR"/*.manifest "$PROBE_DIR/"
install -m0600 "${VPN_FLEET_KNOWN_HOSTS:-$SOURCE_DIR/../../private/inventories/known_hosts}" "$PROBE_DIR/known_hosts"
install -m0644 "$SOURCE_DIR/vpn-probe.service" "$SOURCE_DIR/vpn-probe.timer" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now vpn-probe.timer
systemctl --user start vpn-probe.service
systemctl --user --no-pager status vpn-probe.timer
