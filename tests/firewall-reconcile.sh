#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="vpn-fleet-firewall-reconcile:test"

if [ "${FIREWALL_TEST_IN_CONTAINER:-0}" != 1 ]; then
  command -v docker >/dev/null 2>&1 || {
    echo "FAIL: docker is required for the isolated firewall test" >&2
    exit 1
  }

  docker build --quiet \
    --file "$ROOT/tests/firewall-reconcile.Dockerfile" \
    --tag "$IMAGE" "$ROOT" >/dev/null
  docker run --rm \
    --privileged \
    --network none \
    --env FIREWALL_TEST_IN_CONTAINER=1 \
    --volume "$ROOT:/workspace:ro" \
    "$IMAGE" \
    bash /workspace/tests/firewall-reconcile.sh
  exit
fi

fixture=/tmp/vpn-fleet-firewall-reconcile
mkdir -p "$fixture"

python3 /workspace/tests/firewall-defaults.py

# The base play must reject a missing declaration before common, SSH, or UFW
# can change the node. The direct role invocation must reject it too.
cat > "$fixture/missing-inventory.yml" <<'EOF'
all:
  hosts:
    missing-profile:
      ansible_connection: local
      ansible_python_interpreter: /usr/bin/python3
EOF
before_ufw="$(ufw status 2>/dev/null || true)"
for playbook in /workspace/playbooks/base.yml /workspace/tests/firewall-profile.yml; do
  if ANSIBLE_CONFIG=/workspace/ansible.cfg ANSIBLE_ROLES_PATH=/workspace/roles \
     ansible-playbook -i "$fixture/missing-inventory.yml" "$playbook" \
       > "$fixture/missing.log" 2>&1; then
    echo "FAIL: $playbook accepted a missing public firewall profile" >&2
    exit 1
  fi
  if ! grep -qF 'missing-profile' "$fixture/missing.log" || \
     ! grep -qF 'firewall_allowed_udp_ports' "$fixture/missing.log" || \
     ! grep -qF 'firewall_allowed_tcp_ports' "$fixture/missing.log"; then
      tail -n 40 "$fixture/missing.log" >&2
      echo 'FAIL: missing-profile rejection did not name the node and declarations' >&2
      exit 1
  fi
  grep -Eq 'changed=0[[:space:]]+unreachable=0[[:space:]]+failed=1' "$fixture/missing.log" || {
    tail -n 30 "$fixture/missing.log" >&2
    echo 'FAIL: validation happened after a node change' >&2
    exit 1
  }
  [ "$(ufw status 2>/dev/null || true)" = "$before_ufw" ] || {
    echo 'FAIL: validation touched UFW' >&2
    exit 1
  }
done
echo 'firewall profile: missing declaration rejected before any change'
# Invalid declarations must fail in base pre_tasks with no change.  Empty lists
# are valid and pass the shared validation task.
for value in null '"bad"' '{port: 443}' '[{comment: missing-port}]'; do
  cat > "$fixture/invalid-inventory.yml" <<EOF
all:
  hosts:
    invalid-profile:
      ansible_connection: local
      ansible_python_interpreter: /usr/bin/python3
      firewall_allowed_tcp_ports: $value
      firewall_allowed_udp_ports: []
EOF
  if ANSIBLE_CONFIG=/workspace/ansible.cfg ANSIBLE_ROLES_PATH=/workspace/roles \
     ansible-playbook -i "$fixture/invalid-inventory.yml" /workspace/playbooks/base.yml \
       > "$fixture/invalid.log" 2>&1; then
    echo "FAIL: base accepted invalid TCP declaration $value" >&2; exit 1
  fi
  grep -qF 'invalid-profile' "$fixture/invalid.log"
  grep -qF 'firewall_allowed_tcp_ports' "$fixture/invalid.log"
  if [ "$value" = '[{comment: missing-port}]' ]; then
    grep -qF '"msg": "invalid-profile: firewall_allowed_tcp_ports entries must be mappings with an integer port"' "$fixture/invalid.log"
  else
    grep -qF '"msg": "invalid-profile: declare these public firewall variables as lists: firewall_allowed_tcp_ports"' "$fixture/invalid.log"
  fi
  grep -Eq 'changed=0[[:space:]]+unreachable=0[[:space:]]+failed=1' "$fixture/invalid.log" || {
    tail -n 30 "$fixture/invalid.log" >&2; exit 1;
  }
done
cat > "$fixture/empty-inventory.yml" <<'EOF'
all:
  hosts:
    empty-profile:
      ansible_connection: local
      ansible_python_interpreter: /usr/bin/python3
      firewall_allowed_tcp_ports: []
      firewall_allowed_udp_ports: []
EOF
# The direct validation entry point must accept an explicitly empty profile.
cat > "$fixture/validate-only.yml" <<'EOF'
- hosts: all
  gather_facts: false
  tasks:
    - ansible.builtin.import_role:
        name: firewall
        tasks_from: validate_profile
EOF
ANSIBLE_CONFIG=/workspace/ansible.cfg ANSIBLE_ROLES_PATH=/workspace/roles \
  ansible-playbook -i "$fixture/empty-inventory.yml" "$fixture/validate-only.yml" \
    > "$fixture/empty.log" 2>&1 || { tail -n 30 "$fixture/empty.log" >&2; exit 1; }
echo 'firewall profile: invalid declarations rejected before changes; explicit [] accepted'


run_role() {
  local port="$1"
  local log="$2"
  local networks="${3:-[\"192.0.2.0/24\"]}"
  local rate_limit="${4:-true}"
  local repeat_role="${5:-false}"
  local extra_vars="test_reality_port=$port"

  if [ "$port" = none ]; then
    extra_vars='{"firewall_allowed_tcp_ports": []}'
  fi

  ANSIBLE_CONFIG=/workspace/ansible.cfg \
    ANSIBLE_ROLES_PATH=/workspace/roles \
    ansible-playbook -i localhost, \
      /workspace/tests/firewall-reconcile.yml \
      --extra-vars "$extra_vars" \
      --extra-vars "{\"firewall_ssh_allow_from\": $networks, \"firewall_ssh_rate_limit\": $rate_limit, \"test_repeat_role\": $repeat_role}" \
      > "$log" 2>&1 || {
        tail -n 80 "$log" >&2
        echo "FAIL: firewall role failed for synthetic port $port" >&2
        exit 1
      }
}

# Enabling an inactive UFW through the Ansible module can report a transient
# sysctl failure in a container even though UFW becomes active.  Activate it
# once directly so the role exercises reconciliation rather than that quirk.
ufw --force enable >/dev/null
ufw allow 9000/tcp comment 'operator-owned rule' >/dev/null

run_role 8443 "$fixture/first.log"
ufw status | grep -qF '8443/tcp' || {
  echo 'FAIL: initial managed port was not opened' >&2
  exit 1
}

run_role 443 "$fixture/reconcile.log" '["192.0.2.0/24"]' true true
status="$(ufw status)"

if grep -qF '8443/tcp' <<< "$status"; then
  echo 'FAIL: removed managed port 8443/tcp remains open' >&2
  exit 1
fi
grep -qF '443/tcp' <<< "$status" || {
  echo 'FAIL: desired managed port 443/tcp is not open' >&2
  exit 1
}
grep -Eq '^2424/tcp[[:space:]]+ALLOW([[:space:]]+IN)?[[:space:]]+192\.0\.2\.0/24' \
  <<< "$status" || {
  echo 'FAIL: second role invocation removed the desired trusted SSH rule' >&2
  exit 1
}

run_role none "$fixture/empty.log" '[]'
status="$(ufw status)"
if grep -qF '443/tcp' <<< "$status"; then
  echo 'FAIL: managed port 443/tcp remains open with an empty inventory' >&2
  exit 1
fi
grep -qF '9000/tcp' <<< "$status" || {
  echo 'FAIL: reconciliation removed the unmarked operator rule' >&2
  exit 1
}
grep -Eq '^2424/tcp[[:space:]]+LIMIT([[:space:]]+IN)?[[:space:]]' \
  <<< "$status" || {
  echo 'FAIL: reconciliation removed the SSH rate-limit rule' >&2
  exit 1
}
if grep -Eq '^2424/tcp[[:space:]]+ALLOW([[:space:]]+IN)?[[:space:]]+192\.0\.2\.0/24' \
  <<< "$status"; then
  echo 'FAIL: removed trusted SSH network remains open' >&2
  exit 1
fi

run_role none "$fixture/idempotent.log" '[]'
grep -Eq 'changed=0[[:space:]]+unreachable=0[[:space:]]+failed=0' \
  "$fixture/idempotent.log" || {
    sed -n '/PLAY RECAP/,$p' "$fixture/idempotent.log" >&2
    echo 'FAIL: repeated firewall reconciliation was not idempotent' >&2
    exit 1
  }

# Leave only a stale trusted SSH rule on the SSH port.  The role must reject
# reconciliation before deleting it when rate limiting is disabled.
ufw --force delete limit 2424/tcp >/dev/null
ufw allow from 192.0.2.0/24 to any port 2424 proto tcp \
  comment 'vpn-fleet:tcp:192.0.2.0/24:2424 stale test rule' >/dev/null
if ANSIBLE_CONFIG=/workspace/ansible.cfg \
   ANSIBLE_ROLES_PATH=/workspace/roles \
   ansible-playbook -i localhost, /workspace/tests/firewall-reconcile.yml \
     --extra-vars '{"firewall_allowed_tcp_ports": [], "firewall_ssh_allow_from": [], "firewall_ssh_rate_limit": false}' \
     > "$fixture/guard.log" 2>&1; then
  echo 'FAIL: reconciliation accepted a configuration with no surviving SSH rule' >&2
  exit 1
fi
grep -qF 'No SSH allow or limit rule would remain after reconciliation' \
  "$fixture/guard.log" || {
    tail -n 40 "$fixture/guard.log" >&2
    echo 'FAIL: reconciliation failed for a reason other than the SSH guard' >&2
    exit 1
  }
ufw status | grep -qF '192.0.2.0/24' || {
  echo 'FAIL: SSH guard ran after stale rule deletion' >&2
  exit 1
}

echo 'firewall reconciliation: trusted SSH network revoked; SSH guard and double invocation verified; repeat changed=0'
