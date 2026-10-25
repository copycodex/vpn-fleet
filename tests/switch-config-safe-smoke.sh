#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="vpn-fleet-safe-smoke:test"
SINGBOX_TEST_BIN="${SINGBOX_TEST_BIN:-/usr/local/bin/sing-box}"

if [ "${SAFE_SMOKE_IN_CONTAINER:-0}" != 1 ]; then
  command -v docker >/dev/null 2>&1 || {
    echo "FAIL: docker is required for the isolated network namespace" >&2
    exit 1
  }
  [ -x "$SINGBOX_TEST_BIN" ] || {
    echo "FAIL: sing-box test binary is not executable: $SINGBOX_TEST_BIN" >&2
    exit 1
  }

  docker build --quiet \
    --file "$ROOT/tests/switch-config-safe-smoke.Dockerfile" \
    --tag "$IMAGE" "$ROOT/tests" >/dev/null
  docker run --rm \
    --network none \
    --cap-add NET_ADMIN \
    --device /dev/net/tun \
    --security-opt no-new-privileges \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    --env SAFE_SMOKE_IN_CONTAINER=1 \
    --env SINGBOX=/usr/local/bin/sing-box \
    --volume "$ROOT:/workspace:ro" \
    --volume "$SINGBOX_TEST_BIN:/usr/local/bin/sing-box:ro" \
    "$IMAGE" \
    bash /workspace/tests/switch-config-safe-smoke.sh
  exit
fi

tmp="$(mktemp -d)"
smoke_pid=""
cleanup() {
  if [ -n "$smoke_pid" ] && kill -0 "$smoke_pid" 2>/dev/null; then
    kill "$smoke_pid" 2>/dev/null || true
    wait "$smoke_pid" 2>/dev/null || true
  fi
  rm -rf "$tmp"
}
trap cleanup EXIT

# Exercise the repository's exact function without invoking switch-config.sh,
# whose command modes are reserved for a human on the live workstation.
sed -n '/^smoke_run() {$/,/^}$/p' \
  /workspace/client/switch-config.sh > "$tmp/smoke-run.sh"
# shellcheck source=/dev/null
source "$tmp/smoke-run.sh"
die() { echo "ERROR: $*" >&2; exit 1; }

"$SINGBOX" generate wg-keypair > "$tmp/keypair"
private_key="$(sed -n 's/^PrivateKey: //p' "$tmp/keypair")"
public_key="$(sed -n 's/^PublicKey: //p' "$tmp/keypair")"
[ -n "$private_key" ] && [ -n "$public_key" ] || {
  echo "FAIL: could not generate a synthetic WireGuard keypair" >&2
  exit 1
}

jq -n \
  --arg private_key "$private_key" \
  --arg public_key "$public_key" \
  '{
    log: {level: "error"},
    endpoints: [{
      type: "wireguard",
      tag: "system-test",
      system: true,
      name: "sb-smoke-test",
      mtu: 1280,
      address: ["198.51.100.40/32"],
      private_key: $private_key,
      peers: [{
        address: "192.0.2.1",
        port: 51820,
        public_key: $public_key,
        allowed_ips: ["198.51.100.1/32"]
      }]
    }],
    outbounds: [{type: "direct", tag: "direct"}],
    route: {final: "direct"}
  }' > "$tmp/system-endpoint.json"

ip link set lo up
ip link add test-uplink type dummy
ip link set test-uplink up
ip route add default dev test-uplink metric 42760

ip -br link > "$tmp/links.before"
ip route > "$tmp/routes.before"

(smoke_run "$tmp/system-endpoint.json") > "$tmp/smoke.log" 2>&1 &
smoke_pid=$!
changed_while_running=0
system_interface_created=0
for _attempt in $(seq 1 50); do
  ip -br link > "$tmp/links.current"
  ip route > "$tmp/routes.current"
  if ip link show dev sb-smoke-test >/dev/null 2>&1; then
    system_interface_created=1
  fi
  if ! cmp -s "$tmp/links.before" "$tmp/links.current" \
      || ! cmp -s "$tmp/routes.before" "$tmp/routes.current"; then
    changed_while_running=1
    break
  fi
  kill -0 "$smoke_pid" 2>/dev/null || break
  sleep 0.1
done

set +e
wait "$smoke_pid"
smoke_rc=$?
set -e
smoke_pid=""

ip -br link > "$tmp/links.after"
ip route > "$tmp/routes.after"

[ "$smoke_rc" -eq 0 ] || {
  sed -n '1,20p' "$tmp/smoke.log" >&2
  echo "FAIL: smoke_run rejected the synthetic config" >&2
  exit 1
}
[ "$system_interface_created" -eq 0 ] || {
  echo "FAIL: smoke_run created the system endpoint interface" >&2
  exit 1
}
[ "$changed_while_running" -eq 0 ] || {
  echo "FAIL: smoke_run changed links or routes while the probe was active" >&2
  exit 1
}
cmp -s "$tmp/links.before" "$tmp/links.after" || {
  echo "FAIL: ip -br link differs after smoke_run" >&2
  exit 1
}
cmp -s "$tmp/routes.before" "$tmp/routes.after" || {
  echo "FAIL: ip route differs after smoke_run" >&2
  exit 1
}

switch_script=/workspace/client/switch-config.sh
backup_line="$(grep -n 'echo ">> бэкап текущего"' "$switch_script" | cut -d: -f1)"
rollback_line="$(grep -n 'systemd-run --unit=' "$switch_script" | cut -d: -f1)"
smoke_line="$(grep -nF "if ! smoke_run \"\$NEW\"" "$switch_script" | cut -d: -f1)"
if [ -z "$backup_line" ] || [ -z "$rollback_line" ] || [ -z "$smoke_line" ] \
    || [ "$backup_line" -ge "$rollback_line" ] \
    || [ "$rollback_line" -ge "$smoke_line" ]; then
  echo "FAIL: backup and armed rollback must precede smoke_run" >&2
  exit 1
fi

echo "safe smoke test: links and routes unchanged"
