#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="vpn-fleet-xray-recovery:test"

if [ "${XRAY_RECOVERY_IN_CONTAINER:-0}" != 1 ]; then
  command -v docker >/dev/null 2>&1 || {
    echo "FAIL: docker is required for the isolated deployment test" >&2
    exit 1
  }

  docker build --quiet \
    --file "$ROOT/tests/xray-recoverable-deploy.Dockerfile" \
    --tag "$IMAGE" "$ROOT/tests" >/dev/null
  docker run --rm \
    --network bridge \
    --env XRAY_RECOVERY_IN_CONTAINER=1 \
    --volume "$ROOT:/workspace:ro" \
    "$IMAGE" \
    bash /workspace/tests/xray-recoverable-deploy.sh
  exit
fi

fixture=/tmp/vpn-fleet-xray-recovery
old_version='Xray 1.0.0 (recovery smoke)'
old_config='{"version":"old-recovery-smoke"}'
if command -v systemctl >/dev/null 2>&1; then
  systemctl_path="$(command -v systemctl)"
  systemctl_real="${systemctl_path}.recovery-real"
else
  systemctl_path=/usr/local/bin/systemctl
  systemctl_real=''
fi

cleanup() {
  if [ -f "$fixture/service.pid" ]; then
    service_pid="$(cat "$fixture/service.pid")"
    kill "$service_pid" 2>/dev/null || true
  fi
  if [ -n "$systemctl_real" ] && [ -e "$systemctl_real" ]; then
    mv "$systemctl_real" "$systemctl_path"
  elif [ -z "$systemctl_real" ]; then
    rm -f "$systemctl_path"
  fi
  rm -rf "$fixture"
  rm -f /etc/systemd/system/sing-box-recovery-smoke.service
  rm -f /etc/systemd/system/xray-recovery-smoke.service
}
trap cleanup EXIT

mkdir -p "$fixture/bin" "$fixture/xray" "$fixture/sing-box"

cat > "$fixture/bin/sing-box" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = version ]; then
  echo 'sing-box version 1.13.13'
  exit 0
fi
if [ "${1:-}" = check ]; then
  exit 0
fi
exit 0
EOF
chmod 0755 "$fixture/bin/sing-box"

cat > "$fixture/bin/xray" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = version ]; then
  echo 'Xray 1.0.0 (recovery smoke)'
  exit 0
fi
if [ "${1:-}" = run ] && [ "${2:-}" = -test ]; then
  exit 0
fi
exit 0
EOF
chmod 0755 "$fixture/bin/xray"
printf '%s\n' "$old_config" > "$fixture/xray/config.json"
cp "$fixture/xray/config.json" "$fixture/old-config.json"

cat > "$fixture/fake-systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

fixture=/tmp/vpn-fleet-xray-recovery
action=''
service=''
for argument in "$@"; do
  case "$argument" in
    daemon-reload|--no-pager|--property=*|--value)
      ;;
    show|is-enabled|is-active|start|restart|stop)
      action="$argument"
      ;;
    *)
      service="$argument"
      ;;
  esac
done

alive() {
  [ -f "$fixture/service.pid" ] \
    && kill -0 "$(cat "$fixture/service.pid")" 2>/dev/null
}

start_service() {
  nohup sleep 300 >/dev/null 2>&1 &
  echo "$!" > "$fixture/service.pid"
}

case "$action" in
  '')
    exit 0
    ;;
  show)
    if alive; then
      active=active
      sub=running
    else
      active=inactive
      sub=dead
    fi
    printf '%s\n' \
      'LoadState=loaded' \
      "ActiveState=$active" \
      "SubState=$sub" \
      'UnitFileState=enabled'
    ;;
  is-enabled)
    echo enabled
    ;;
  is-active)
    if alive; then
      echo active
    else
      echo inactive
      exit 3
    fi
    ;;
  stop)
    if alive; then
      kill "$(cat "$fixture/service.pid")" 2>/dev/null || true
    fi
    rm -f "$fixture/service.pid"
    ;;
  start|restart)
    if [ "$service" = xray-recovery-smoke ] \
        && [ -e "$fixture/fail-next-xray-restart" ] \
        && ! "$fixture/bin/xray" version | grep -qF 'Xray 1.0.0 '; then
      rm -f "$fixture/fail-next-xray-restart"
      if alive; then
        kill "$(cat "$fixture/service.pid")" 2>/dev/null || true
      fi
      rm -f "$fixture/service.pid"
      echo 'synthetic Xray restart failure' >&2
      exit 1
    fi
    if alive; then
      kill "$(cat "$fixture/service.pid")" 2>/dev/null || true
    fi
    start_service
    ;;
esac
EOF
chmod 0755 "$fixture/fake-systemctl"

cat > "$fixture/fake-ss" <<'EOF'
#!/usr/bin/env bash
echo 'LISTEN 0 4096 0.0.0.0:443 0.0.0.0:*'
EOF
chmod 0755 "$fixture/fake-ss"

if [ -n "$systemctl_real" ]; then
  mv "$systemctl_path" "$systemctl_real"
fi
cp "$fixture/fake-systemctl" "$systemctl_path"
if command -v ss >/dev/null 2>&1; then
  ss_path="$(command -v ss)"
else
  ss_path=/usr/local/bin/ss
fi
cp "$fixture/fake-ss" "$ss_path"

"$systemctl_path" start xray-recovery-smoke
touch "$fixture/fail-next-xray-restart"

set +e
ANSIBLE_CONFIG=/workspace/ansible.cfg \
  ANSIBLE_ROLES_PATH=/workspace/roles \
  ansible-playbook -i localhost, \
    /workspace/tests/xray-recoverable-deploy.yml \
    > "$fixture/negative.log" 2>&1
negative_rc=$?
set -e

[ "$negative_rc" -ne 0 ] || {
  echo 'FAIL: the injected restart failure did not fail the play' >&2
  exit 1
}
[ ! -e "$fixture/fail-next-xray-restart" ] || {
  echo 'FAIL: the synthetic restart failure was not reached' >&2
  exit 1
}
grep -qF 'synthetic Xray restart failure' "$fixture/negative.log" || {
  echo 'FAIL: the negative run failed for an unexpected reason' >&2
  exit 1
}
[ "$("$fixture/bin/xray" version)" = "$old_version" ] || {
  echo 'FAIL: Xray binary was not rolled back' >&2
  exit 1
}
cmp -s "$fixture/old-config.json" "$fixture/xray/config.json" || {
  echo 'FAIL: Xray config was not rolled back with the binary' >&2
  exit 1
}
"$systemctl_path" is-active xray-recovery-smoke >/dev/null || {
  echo 'FAIL: the previous Xray service was not restored' >&2
  exit 1
}

ANSIBLE_CONFIG=/workspace/ansible.cfg \
  ANSIBLE_ROLES_PATH=/workspace/roles \
  ansible-playbook -i localhost, \
    /workspace/tests/xray-recoverable-deploy.yml \
    > "$fixture/apply.log" 2>&1 || {
      tail -n 80 "$fixture/apply.log" >&2
      echo 'FAIL: Xray deployment after rollback did not succeed' >&2
      exit 1
    }

ANSIBLE_CONFIG=/workspace/ansible.cfg \
  ANSIBLE_ROLES_PATH=/workspace/roles \
  ansible-playbook -i localhost, \
    /workspace/tests/xray-recoverable-deploy.yml \
    > "$fixture/idempotent.log" 2>&1 || {
      tail -n 80 "$fixture/idempotent.log" >&2
      echo 'FAIL: repeated Xray deployment failed' >&2
      exit 1
    }

grep -Eq 'changed=0[[:space:]]+unreachable=0[[:space:]]+failed=0' \
  "$fixture/idempotent.log" || {
    grep -B 1 '^changed:' "$fixture/idempotent.log" >&2 || true
    sed -n '/PLAY RECAP/,$p' "$fixture/idempotent.log" >&2
    echo 'FAIL: repeated Xray deployment was not idempotent' >&2
    exit 1
  }

echo 'recoverable Xray deploy: rollback restored pair and service; repeat changed=0'
