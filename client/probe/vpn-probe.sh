#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="${VPN_PROBE_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/vpn-fleet/probe}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/vpn-fleet"
LOG_FILE="$STATE_DIR/probe.log"
SING_BOX_BIN="${SING_BOX_BIN:-/usr/local/bin/sing-box}"
IP_CHECK_URL="${VPN_PROBE_IP_URL:-https://api.ipify.org}"
FAILED=0
ACTIVE_PID=""
RUNTIME_FILES=()

cleanup() {
  if [[ -n "$ACTIVE_PID" ]]; then
    kill "$ACTIVE_PID" 2>/dev/null || true
    wait "$ACTIVE_PID" 2>/dev/null || true
  fi
  if (( ${#RUNTIME_FILES[@]} > 0 )); then
    rm -f "${RUNTIME_FILES[@]}"
  fi
}

trap cleanup EXIT INT TERM

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
touch "$LOG_FILE"
chmod 600 "$LOG_FILE"

log() {
  printf '%s %s\n' "$(date --iso-8601=seconds)" "$*" >> "$LOG_FILE"
}

alert() {
  log "FAIL $*"
  if command -v notify-send >/dev/null 2>&1 && [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    notify-send --urgency=critical "VPN probe failed" "$*" || true
  fi
}

[[ -x "$SING_BOX_BIN" ]] || { alert "sing-box binary not found"; exit 1; }
[[ -d "$CONFIG_DIR" ]] || { alert "config directory not found: $CONFIG_DIR"; exit 1; }

shopt -s nullglob
manifests=("$CONFIG_DIR"/*.manifest)
(( ${#manifests[@]} > 0 )) || { alert "no probe manifests in $CONFIG_DIR"; exit 1; }

for manifest in "${manifests[@]}"; do
  IFS=$'\t' read -r node expected_ip ssh_target ssh_port ssh_identity < "$manifest"
  [[ -n "$node" && -n "$expected_ip" && -n "$ssh_target" &&
    "$ssh_port" =~ ^[0-9]+$ && -n "$ssh_identity" ]] || {
    alert "invalid manifest: $(basename "$manifest")"
    FAILED=1
    continue
  }

  for transport in vless hy2; do
    config="$CONFIG_DIR/$node-$transport.json"
    [[ -f "$config" ]] || continue
    port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
    runtime="$(mktemp "${TMPDIR:-/tmp}/vpn-probe.XXXXXX.json")"
    log_tmp="$(mktemp "${TMPDIR:-/tmp}/vpn-probe.XXXXXX.log")"
    RUNTIME_FILES=("$runtime" "$runtime.ip" "$log_tmp")
    chmod 600 "$runtime" "$log_tmp"
    sed "s/\"listen_port\": 19080/\"listen_port\": $port/" "$config" > "$runtime"

    # sing-box 1.13.x uTLS "randomized" fingerprint is seeded per process:
    # ~50% of processes emit an ML-KEM ClientHello that the Reality server
    # rejects (vless: EOF). Restart the process (re-roll) instead of retrying
    # curl inside one doomed process.
    ready=0
    reason=""
    for _attempt in 1 2 3 4; do
      "$SING_BOX_BIN" run -c "$runtime" > "$log_tmp" 2>&1 &
      pid=$!
      ACTIVE_PID="$pid"
      sleep 1
      for _ in 1 2; do
        if curl --silent --fail --max-time 5 --socks5-hostname "127.0.0.1:$port" \
          "$IP_CHECK_URL" > "$runtime.ip"; then
          ready=1
          break
        fi
        sleep 1
      done
      if (( ready )); then
        break
      fi
      case "$(tail -n 5 "$log_tmp")" in
        *EOF*) reason="eof-reality-reject" ;;
        *timeout*) reason="timeout" ;;
        *refused*) reason="refused" ;;
        *) reason="unknown" ;;
      esac
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      ACTIVE_PID=""
      # only the fingerprint dice is worth re-rolling; hard network
      # failures will not change on process restart
      [[ "$reason" == "eof-reality-reject" ]] || break
    done

    if (( ready )); then
      actual_ip="$(tr -d '[:space:]' < "$runtime.ip")"
      if [[ "$actual_ip" == "$expected_ip" ]]; then
        log "OK node=$node transport=$transport egress=$actual_ip"
      else
        alert "node=$node transport=$transport unexpected-egress=$actual_ip"
        FAILED=1
      fi
    else
      alert "node=$node transport=$transport connection failed reason=${reason:-unknown}"
      FAILED=1
    fi

    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    rm -f "$runtime" "$runtime.ip" "$log_tmp"
    ACTIVE_PID=""
    RUNTIME_FILES=()
  done

  ssh_identity="${ssh_identity/#\~/$HOME}"
  # ConnectTimeout only bounds the TCP handshake. A wedged ssh-agent hangs the
  # signing step instead, which pins this oneshot in "activating" forever — and
  # while it runs, the timer never computes its next elapse (Trigger: n/a), so
  # monitoring goes silently blind. Bound the whole exchange, not just connect.
  #
  # The key is passphrase-protected and this runs unattended from a systemd user
  # timer, where no ssh-agent is reachable — BatchMode then fails instantly with
  # rc=255 on every tick and the repeated auth failures also feed the node's
  # fail2ban. ~/bin/ssh-key-unlock already doubles as an SSH_ASKPASS helper: it
  # prints the passphrase from the sops store whose age key comes from the TPM,
  # so no human and no agent are involved. BatchMode must stay off (it disables
  # askpass). NumberOfPasswordPrompts must be >= 1: it bounds the private-key
  # passphrase loop too, so 0 makes ssh try only the empty passphrase and drop
  # the method without ever calling askpass ("we did not send a packet"). One
  # attempt is enough, and PreferredAuthentications=publickey keeps the exchange
  # non-interactive, so a wrong or missing passphrase still fails fast.
  ssh_rc=0
  ssh_askpass="$HOME/bin/ssh-key-unlock"
  if [[ -x "$ssh_askpass" ]]; then
    # ssh-key-unlock resolves sops-age-key through PATH, and the systemd user
    # manager's PATH is whatever the login session happened to export.
    SSH_KEY_UNLOCK_ASKPASS=1 SSH_KEY_UNLOCK_NAME="$(basename "$ssh_identity")" \
    SSH_ASKPASS="$ssh_askpass" SSH_ASKPASS_REQUIRE=force PATH="$HOME/bin:$PATH" \
    timeout 20 ssh -i "$ssh_identity" -o IdentitiesOnly=yes \
      -o PreferredAuthentications=publickey -o NumberOfPasswordPrompts=1 \
      -o ConnectTimeout=8 -o UserKnownHostsFile="$CONFIG_DIR/known_hosts" \
      -p "$ssh_port" "$ssh_target" true </dev/null || ssh_rc=$?
  else
    timeout 20 ssh -i "$ssh_identity" -o IdentitiesOnly=yes -o BatchMode=yes \
      -o ConnectTimeout=8 -o UserKnownHostsFile="$CONFIG_DIR/known_hosts" \
      -p "$ssh_port" "$ssh_target" true || ssh_rc=$?
  fi
  if (( ssh_rc == 0 )); then
    log "OK node=$node transport=ssh"
  elif (( ssh_rc == 124 )); then
    alert "node=$node transport=ssh timed out after 20s (agent wedged?)"
    FAILED=1
  else
    alert "node=$node transport=ssh connection failed rc=$ssh_rc"
    FAILED=1
  fi
done

cleanup
trap - EXIT INT TERM
exit "$FAILED"
