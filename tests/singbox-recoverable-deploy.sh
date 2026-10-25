#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="vpn-fleet-singbox-recovery:test"

if [ "${SINGBOX_RECOVERY_IN_CONTAINER:-0}" != 1 ]; then
  command -v docker >/dev/null 2>&1 || {
    echo "FAIL: docker is required for the isolated deployment test" >&2
    exit 1
  }

  docker build --quiet \
    --file "$ROOT/tests/singbox-recoverable-deploy.Dockerfile" \
    --tag "$IMAGE" "$ROOT/tests" >/dev/null
  docker run --rm \
    --network bridge \
    --env SINGBOX_RECOVERY_IN_CONTAINER=1 \
    --volume "$ROOT:/workspace:ro" \
    "$IMAGE" \
    bash /workspace/tests/singbox-recoverable-deploy.sh
  exit
fi

fixture=/tmp/vpn-fleet-singbox-recovery
old_version='sing-box version 1.0.0'
old_config='{"version":"old-recovery-smoke"}'
if command -v systemctl >/dev/null 2>&1; then
  systemctl_path="$(command -v systemctl)"
  systemctl_real="${systemctl_path}.recovery-real"
else
  systemctl_path=/usr/local/bin/systemctl
  systemctl_real=''
fi

cleanup() {
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
  echo 'sing-box version 1.0.0'
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
  echo 'Xray 26.3.27 (recovery smoke)'
  exit 0
fi
if [ "${1:-}" = run ] && [ "${2:-}" = -test ]; then
  exit 0
fi
exit 0
EOF
chmod 0755 "$fixture/bin/xray"
printf '%s\n' "$old_config" > "$fixture/sing-box/config.json"
cp "$fixture/sing-box/config.json" "$fixture/old-config.json"

cat > "$fixture/fake-systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

fixture=/tmp/vpn-fleet-singbox-recovery
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

case "$action" in
  show)
    printf '%s\n' \
      'LoadState=loaded' \
      'ActiveState=active' \
      'SubState=running' \
      'UnitFileState=enabled'
    ;;
  is-enabled)
    echo enabled
    ;;
  is-active)
    echo active
    ;;
  restart)
    if [ "$service" = sing-box-recovery-smoke ] \
        && [ -e "$fixture/fail-next-singbox-restart" ] \
        && ! "$fixture/bin/sing-box" version | grep -qF 'sing-box version 1.0.0'; then
      rm -f "$fixture/fail-next-singbox-restart"
      echo 'synthetic sing-box restart failure' >&2
      exit 1
    fi
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

touch "$fixture/fail-next-singbox-restart"

set +e
ANSIBLE_CONFIG=/workspace/ansible.cfg \
  ANSIBLE_ROLES_PATH=/workspace/roles \
  ansible-playbook -i localhost, \
    /workspace/tests/singbox-recoverable-deploy.yml \
    > "$fixture/negative.log" 2>&1
negative_rc=$?
set -e

[ "$negative_rc" -ne 0 ] || {
  echo 'FAIL: the injected restart failure did not fail the play' >&2
  exit 1
}
[ ! -e "$fixture/fail-next-singbox-restart" ] || {
  tail -n 80 "$fixture/negative.log" >&2
  echo 'FAIL: the synthetic restart failure was not reached' >&2
  exit 1
}
grep -qF 'synthetic sing-box restart failure' "$fixture/negative.log" || {
  echo 'FAIL: the negative run failed for an unexpected reason' >&2
  exit 1
}
[ "$("$fixture/bin/sing-box" version)" = "$old_version" ] || {
  echo 'FAIL: sing-box binary was not rolled back' >&2
  exit 1
}
cmp -s "$fixture/old-config.json" "$fixture/sing-box/config.json" || {
  echo 'FAIL: sing-box config was not rolled back with the binary' >&2
  exit 1
}
"$systemctl_path" is-active sing-box-recovery-smoke >/dev/null || {
  echo 'FAIL: the previous sing-box service was not restored' >&2
  exit 1
}

ANSIBLE_CONFIG=/workspace/ansible.cfg \
  ANSIBLE_ROLES_PATH=/workspace/roles \
  ansible-playbook -i localhost, \
    /workspace/tests/singbox-recoverable-deploy.yml \
    > "$fixture/apply.log" 2>&1 || {
      tail -n 80 "$fixture/apply.log" >&2
      echo 'FAIL: sing-box deployment after rollback did not succeed' >&2
      exit 1
    }

ANSIBLE_CONFIG=/workspace/ansible.cfg \
  ANSIBLE_ROLES_PATH=/workspace/roles \
  ansible-playbook -i localhost, \
    /workspace/tests/singbox-recoverable-deploy.yml \
    > "$fixture/idempotent.log" 2>&1 || {
      tail -n 80 "$fixture/idempotent.log" >&2
      echo 'FAIL: repeated sing-box deployment failed' >&2
      exit 1
    }

grep -Eq 'changed=0[[:space:]]+unreachable=0[[:space:]]+failed=0' \
  "$fixture/idempotent.log" || {
    sed -n '/PLAY RECAP/,$p' "$fixture/idempotent.log" >&2
    echo 'FAIL: repeated sing-box deployment was not idempotent' >&2
    exit 1
  }

echo 'recoverable sing-box deploy: rollback restored pair and service; repeat changed=0'
