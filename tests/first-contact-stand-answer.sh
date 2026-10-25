#!/usr/bin/env bash
# Four isolated, synthetic password-change dialogues on loopback Docker ports.
set -euo pipefail
export LC_ALL=C

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE=vpn-fleet-first-contact-stand:local
PREFIX=vpn-fleet-answer-stand
WORK_DIR=""
cases=(A1 A2 B1 B2)
ports=(22131 22132 22133 22134)

down() {
  for case in "${cases[@]}"; do
    docker rm -f "$PREFIX-$case" >/dev/null 2>&1 || true
  done
}

cleanup() {
  if [ "${VPN_FLEET_KEEP_STAND:-0}" = 1 ]; then
    echo 'answer-stand: kept for diagnostics'
    return
  fi
  down
  if [ -n "$WORK_DIR" ]; then
    rm -rf "$WORK_DIR"
  fi
}

up() {
  down
  docker build -q -t "$IMAGE" "$ROOT/tests/first-contact-stand" >/dev/null
  for index in "${!cases[@]}"; do
    options=()
    case "${cases[$index]}" in
      A1) options=(-e SLOW_PAM_SECONDS=11) ;;
      A2) options=(-e SLOW_PAM_SECONDS=11 -e STTY_MODE=-icrnl) ;;
      B1) options=(-e STTY_MODE=inlcr) ;;
      B2) options=(-e STTY_MODE=-icrnl) ;;
    esac
    docker run -d --name "$PREFIX-${cases[$index]}" \
      -e EXPIRE_PASSWORD=0 "${options[@]}" \
      -p "127.0.0.1:${ports[$index]}:22" "$IMAGE" >/dev/null
  done
  for port in "${ports[@]}"; do
    ready=0
    for _ in $(seq 1 40); do
      if ssh-keyscan -T 1 -p "$port" 127.0.0.1 >/dev/null 2>&1; then
        ready=1
        break
      fi
      sleep 0.25
    done
    [ "$ready" -eq 1 ] || { echo 'answer-stand: sshd did not start' >&2; exit 1; }
  done
}

test_stand() {
  WORK_DIR="$(mktemp -d)"
  trap cleanup EXIT
  up
  python3 "$ROOT/tests/first-contact-stand-answer.py" "$WORK_DIR" "$ROOT"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  test) test_stand ;;
  *) echo "Usage: $0 {up|down|test}" >&2; exit 2 ;;
esac
