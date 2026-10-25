#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="vpn-fleet-reality-frontend-off:test"

if [ "${FRONTEND_OFF_IN_CONTAINER:-0}" != 1 ]; then
  docker build --quiet -f "$ROOT/tests/reality-frontend-off.Dockerfile" -t "$IMAGE" "$ROOT" >/dev/null
  docker run --rm --network none -e FRONTEND_OFF_IN_CONTAINER=1 \
    -v "$ROOT:/workspace:ro" "$IMAGE" bash /workspace/tests/reality-frontend-off.sh
  exit
fi

python3 - <<'PY'
import yaml
with open('/workspace/playbooks/deploy-node.yml', encoding='utf-8') as stream:
    plays = yaml.safe_load(stream)
deploy = plays[-1]
assert any(task.get('ansible.builtin.import_role', {}).get('name') == 'reality_frontend_off'
           for task in deploy.get('pre_tasks', [])), 'direct Xray transition is not in deploy pre_tasks'
PY

cat >/etc/haproxy/haproxy.cfg <<'EOF'
global
    daemon
defaults
    mode tcp
    timeout connect 1s
    timeout client 1m
    timeout server 1m
frontend synthetic
    bind :8443
    default_backend synthetic
backend synthetic
    server local 127.0.0.1:1443
EOF
service haproxy start >/dev/null
awk 'NR>1 {split($2,a,":"); if (a[length(a)]=="20FB") found=1} END {exit !found}' /proc/net/tcp || {
  echo 'FAIL: synthetic HAProxy did not bind 8443' >&2
  exit 1
}

export ANSIBLE_CONFIG=/workspace/ansible.cfg
export ANSIBLE_ROLES_PATH=/workspace/roles
ansible-playbook -i localhost, /workspace/tests/reality-frontend-off.yml >/tmp/first.log 2>&1 || {
  tail -n 50 /tmp/first.log >&2
  exit 1
}
if service haproxy status >/dev/null 2>&1; then
  echo 'FAIL: old HAProxy is still running' >&2
  exit 1
fi
ansible-playbook -i localhost, /workspace/tests/reality-frontend-off.yml >/tmp/second.log 2>&1 || {
  tail -n 50 /tmp/second.log >&2
  exit 1
}
grep -Eq 'changed=0[[:space:]]+unreachable=0[[:space:]]+failed=0' /tmp/second.log || {
  echo 'FAIL: disabling HAProxy again is not idempotent' >&2
  exit 1
}
echo 'reality frontend off: ok'
