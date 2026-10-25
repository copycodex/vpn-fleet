#!/usr/bin/env bash
# Reproducible local sshd stand for first contact. No production coordinates enter it.
set -euo pipefail
export LC_ALL=C

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="vpn-fleet-first-contact-stand:local"
PREFIX="vpn-fleet-first-contact"

containers=(
  "$PREFIX-expired"
  "$PREFIX-normal"
  "$PREFIX-wrong"
  "$PREFIX-key"
  "$PREFIX-probe"
  "$PREFIX-resume"
  "$PREFIX-absent"
)
ports=(22022 22023 22024 22025 22026 22027 22028)
WORK_DIR=""

down() {
  for container in "${containers[@]}"; do
    docker rm -f "$container" >/dev/null 2>&1 || true
  done
}

stand_cleanup() {
  if [ "${VPN_FLEET_KEEP_STAND:-0}" = 1 ]; then
    echo "first-contact stand: kept for diagnostics"
    return
  fi
  if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
    rm -rf "$WORK_DIR"
  fi
  down
}

up() {
  down
  docker build -q -t "$IMAGE" "$ROOT/tests/first-contact-stand" >/dev/null
  for index in "${!containers[@]}"; do
    expire=0
    { [ "$index" -eq 0 ] || [ "$index" -eq 4 ]; } && expire=1
    publish=(-p "127.0.0.1:${ports[$index]}:22")
    # The same sshd is exposed on a second port so bootstrap can prove that
    # only the known_hosts token changed while every key type/body stayed put.
    if [ "$index" -eq 0 ]; then
      publish+=(-p "127.0.0.1:22424:22")
    fi
    docker run -d --name "${containers[$index]}" \
      -e "EXPIRE_PASSWORD=$expire" \
      "${publish[@]}" "$IMAGE" >/dev/null
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
    [ "$ready" -eq 1 ] || { echo "ERROR: stand sshd did not start" >&2; exit 1; }
  done
  echo "first-contact stand: up"
}

make_private_tree() {
  local private_dir="$1"
  local recipient="$2"
  local fleet_key="$3"
  mkdir -p "$private_dir/inventories/nodes" \
    "$private_dir/inventories/group_vars/all"
  : > "$private_dir/inventories/known_hosts"
  cat > "$private_dir/.sops.yaml" <<EOF
creation_rules:
  - path_regex: first-entry[.]sops[.]yml$
    encrypted_regex: ^password$
    age: $recipient
  - path_regex: inventories/group_vars/all/secrets[.]sops[.]yml$
    age: $recipient
EOF
  for spec in expired:22022 normal:22023 wrong:22024 key:22025 resume:22027 absent:22028; do
    node="${spec%%:*}"
    port="${spec##*:}"
    cat > "$private_dir/inventories/nodes/$node.yml" <<EOF
all:
  hosts:
    $node:
      ansible_host: 127.0.0.1
      ansible_user: fresh
      ansible_port: $port
      ansible_ssh_private_key_file: $fleet_key
      node_name: $node
EOF
  done
  first_entry_file="$private_dir/inventories/first-entry.sops.yml"
  : > "$first_entry_file"
  for number in $(seq 1 88); do
    printf '# stand production-shaped documentation %02d\n' "$number" >> "$first_entry_file"
  done
  cat >> "$first_entry_file" <<'EOF'
first_entry:
    expired:
        # password-expired provider branch
        user: fresh
        port: 22022
        method: password
        # owner-managed SOPS value
        password: Stand-Initial9!Password
    normal:
        user: fresh
        port: 22023
        method: password
        password: Stand-Initial9!Password
    wrong:
        user: fresh
        port: 22024
        method: password
        password: Stand-Wrong9!Password
    key:
        user: fresh
        port: 22025
        method: key
    resume:
        user: fresh
        port: 22027
        method: password
        password: Stand-Resume8!Password
EOF
  cat > "$private_dir/inventories/group_vars/all/secrets.sops.yml" <<'EOF'
# stand marker must be encrypted with the rest of this production-shaped file
node_expired_console_password: ""
node_normal_console_password: Stand-Existing1!Normal
node_wrong_console_password: Stand-Existing2!Wrong
node_key_console_password: Stand-Existing3!Key
node_resume_console_password: Stand-Existing4!Resume
node_absent_console_password: Stand-Existing5!Absent
node_shared_scalar: Stand-Existing6!Scalar
node_shared_list:
  - Stand-Existing7!List
node_shared_map:
  nested: Stand-Existing8!Map
EOF
  SOPS_AGE_KEY_FILE="$4" python3 "$ROOT/scripts/secrets_lock.py" "$first_entry_file" -- \
    sops --config "$private_dir/.sops.yaml" --encrypt --in-place "$first_entry_file"
  SOPS_AGE_KEY_FILE="$4" python3 "$ROOT/scripts/secrets_lock.py" \
    "$private_dir/inventories/group_vars/all/secrets.sops.yml" -- \
    sops --config "$private_dir/.sops.yaml" --encrypt --in-place \
    "$private_dir/inventories/group_vars/all/secrets.sops.yml"
}

run_apply() {
  local private_dir="$1"
  local age_key="$2"
  local node="$3"
  VPN_FLEET_PRIVATE_DIR="$private_dir" SOPS_AGE_KEY_FILE="$age_key" \
    python3 "$ROOT/scripts/first-contact.py" "$node" --apply
}

test_stand() {
  up
  local work
  work="$(mktemp -d)"
  WORK_DIR="$work"
  trap stand_cleanup EXIT
  age-keygen -o "$work/age-key.txt" >/dev/null 2>&1
  recipient="$(age-keygen -y "$work/age-key.txt")"
  ssh-keygen -q -t ed25519 -N "" -f "$work/fleet-key"
  private_dir="$work/private"
  make_private_tree "$private_dir" "$recipient" "$work/fleet-key" "$work/age-key.txt"
  initial_comment_count="$(grep -cE '^[[:space:]]*#' \
    "$private_dir/inventories/first-entry.sops.yml")"
  initial_console="$(SOPS_AGE_KEY_FILE="$work/age-key.txt" \
    sops --config "$private_dir/.sops.yaml" --decrypt \
    --extract '["node_expired_console_password"]' \
    "$private_dir/inventories/group_vars/all/secrets.sops.yml")"
  [ -z "$initial_console" ] || {
    echo "ERROR: stand console password did not decrypt as empty" >&2
    exit 1
  }
  initial_provider="$(SOPS_AGE_KEY_FILE="$work/age-key.txt" \
    sops --config "$private_dir/.sops.yaml" --decrypt \
    --extract '["first_entry"]["expired"]["password"]' \
    "$private_dir/inventories/first-entry.sops.yml")"
  [ "$initial_provider" = 'Stand-Initial9!Password' ] || {
    echo "ERROR: stand provider password did not round-trip through SOPS" >&2
    exit 1
  }

  # Bootstrap owns the hand-off from the accepted door key to the
  # steady-state port. Use the same container for the matching case and a
  # different container (therefore a different generated host key) for the
  # refusal case. The playbook is limited to its local post-task tag: no role
  # task and no live node is touched.
  bootstrap_known_hosts="$work/bootstrap-known_hosts"
  bootstrap_header="$work/bootstrap-known-hosts-header"
  cat > "$bootstrap_header" <<'EOF'
# production-shaped bootstrap known_hosts documentation
# comments must survive the port hand-off
EOF
  cp "$bootstrap_header" "$bootstrap_known_hosts"
  ssh-keyscan -T 5 -p 22022 127.0.0.1 2>/dev/null | \
    sed 's/^\[[^]]*\]:22022 /127.0.0.1 /' | \
    awk '$2 ~ /^(ssh-|ecdsa-)/ { print $0 " vpn-fleet:bootstrap-stand:door" }' \
    >> "$bootstrap_known_hosts"
  bootstrap_inventory="$work/bootstrap-inventory.yml"
  cat > "$bootstrap_inventory" <<EOF
all:
  hosts:
    bootstrap-stand:
      ansible_host: 127.0.0.1
      ansible_port: 22
      bootstrap_user: fresh
      bootstrap_door_port: 22
      bootstrap_ssh_port: 22424
EOF
  bootstrap_args=(
    "$ROOT/playbooks/bootstrap.yml"
    -i "$bootstrap_inventory"
    -l bootstrap-stand
    --tags bootstrap-known-hosts
    -e "bootstrap_known_hosts_file=$bootstrap_known_hosts"
  )
  LC_ALL=C.UTF-8 ansible-playbook "${bootstrap_args[@]}" >/dev/null
  after_first="$work/bootstrap-known-hosts-after-first"
  cp "$bootstrap_known_hosts" "$after_first"
  LC_ALL=C.UTF-8 ansible-playbook "${bootstrap_args[@]}" >/dev/null
  cmp -s "$after_first" "$bootstrap_known_hosts"
  head -n 2 "$bootstrap_known_hosts" | cmp -s "$bootstrap_header" -
  awk '$1 == "127.0.0.1" { print $2, $3 }' "$bootstrap_known_hosts" | sort -u > \
    "$work/bootstrap-door-keys"
  awk '$1 == "[127.0.0.1]:22424" { print $2, $3 }' "$bootstrap_known_hosts" | sort -u > \
    "$work/bootstrap-steady-keys"
  [ -s "$work/bootstrap-door-keys" ]
  cmp -s "$work/bootstrap-door-keys" "$work/bootstrap-steady-keys"

  mismatch_before="$work/bootstrap-known-hosts-before-mismatch"
  cp "$bootstrap_known_hosts" "$mismatch_before"
  set +e
  mismatch_output="$(LC_ALL=C.UTF-8 ansible-playbook "${bootstrap_args[@]}" \
    -e bootstrap_ssh_port=22023 2>&1)"
  mismatch_rc=$?
  set -e
  [ "$mismatch_rc" -ne 0 ]
  [[ "$mismatch_output" == *"known_hosts не изменён"* ]]
  cmp -s "$mismatch_before" "$bootstrap_known_hosts"
  echo "bootstrap stand: steady-state host key hand-off passed"

  # The stand must reproduce the provider's forced-password-change branch.
  python3 - "$private_dir" <<'PY'
import pexpect
import sys

child = pexpect.spawn(
    "ssh",
    ["-tt", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
     "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
     "-p", "22026", "fresh@127.0.0.1", "true"],
    encoding="utf-8",
    timeout=10,
)
child.expect("password:")
child.sendline("Stand-Initial9!Password")
child.expect("You are required to change your password immediately")
child.close(force=True)
print("stand: forced password change reproduced")
PY

  # Removing remote PTY must produce a named refusal rather than a hang.
  ssh-keyscan -T 5 -p 22022 127.0.0.1 2>/dev/null > \
    "$private_dir/inventories/known_hosts"
  python3 - "$ROOT" "$private_dir" "$work/fleet-key" <<'PY'
import importlib.util
from pathlib import Path
import sys

root, private, fleet_key = sys.argv[1:]
spec = importlib.util.spec_from_file_location("first_contact_stand_module", Path(root) / "scripts/first-contact.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
door = module.NetworkDoor("expired", "127.0.0.1", "fresh", 22022, "password", True, Path(fleet_key))
reason = module._change_password(
    door,
    Path(private) / "inventories/known_hosts",
    "Stand-Initial9!Password",
    "Stand-NoPty9!Password",
    force_tty=False,
)
assert reason and "PTY" in reason
print("stand: missing PTY rejected with a named reason")
PY
  : > "$private_dir/inventories/known_hosts"

  expired_output="$(run_apply "$private_dir" "$work/age-key.txt" expired)"
  [[ "$expired_output" == *"ключ принят без сверки с консолью (TOFU)"* ]]
  [[ "$expired_output" == *"ключ установлен и доказан отдельным соединением"* ]]
  [[ "$expired_output" != *"Stand-Initial9!Password"* ]]
  rerun_output="$(run_apply "$private_dir" "$work/age-key.txt" expired)"
  [[ "$rerun_output" == *"уже сделано"* ]]

  normal_output="$(run_apply "$private_dir" "$work/age-key.txt" normal)"
  [[ "$normal_output" == *"ключ установлен и доказан отдельным соединением"* ]]
  docker exec "$PREFIX-resume" \
    sh -c "echo 'fresh:Stand-Resume8!Password' | chpasswd"
  resume_output="$(run_apply "$private_dir" "$work/age-key.txt" resume)"
  [[ "$resume_output" == *"ключ установлен и доказан отдельным соединением"* ]]
  secrets_file="$private_dir/inventories/group_vars/all/secrets.sops.yml"
  if grep -qF 'stand marker must be encrypted' "$secrets_file"; then
    echo "stand marker leaked into $secrets_file as plaintext" >&2
    exit 1
  fi
  [ "$(grep -cE '^node_.*_console_password: ENC\[' "$secrets_file")" -eq 6 ]
  SOPS_AGE_KEY_FILE="$work/age-key.txt" sops --config "$private_dir/.sops.yaml" \
    --decrypt "$secrets_file" | python3 -c '
import sys, yaml
d = yaml.safe_load(sys.stdin)
assert d["node_expired_console_password"]
assert d["node_normal_console_password"]
assert d["node_wrong_console_password"] == "Stand-Existing2!Wrong"
assert d["node_key_console_password"] == "Stand-Existing3!Key"
assert d["node_resume_console_password"]
assert d["node_absent_console_password"] == "Stand-Existing5!Absent"
assert d["node_shared_scalar"] == "Stand-Existing6!Scalar"
assert d["node_shared_list"] == ["Stand-Existing7!List"]
assert d["node_shared_map"] == {"nested": "Stand-Existing8!Map"}
'

  set +e
  wrong_output="$(run_apply "$private_dir" "$work/age-key.txt" wrong 2>&1)"
  wrong_rc=$?
  key_output="$(run_apply "$private_dir" "$work/age-key.txt" key 2>&1)"
  key_rc=$?
  absent_output="$(run_apply "$private_dir" "$work/age-key.txt" absent 2>&1)"
  absent_rc=$?
  set -e
  [ "$wrong_rc" -ne 0 ]
  [[ "$wrong_output" == *"повторных попыток не было"* ]]
  [[ "$wrong_output" != *"Stand-Wrong9!Password"* ]]
  [ "$key_rc" -ne 0 ]
  [[ "$key_output" == *"method: key запрещает попытку парольного входа"* ]]
  [ "$absent_rc" -ne 0 ]
  [[ "$absent_output" == *"запись first_entry отсутствует, а ключ ноду не открывает"* ]]

  SOPS_AGE_KEY_FILE="$work/age-key.txt" sops --config "$private_dir/.sops.yaml" \
    --decrypt "$private_dir/inventories/first-entry.sops.yml" | python3 -c '
import sys, yaml
entries = yaml.safe_load(sys.stdin)["first_entry"]
assert set(entries) == {"wrong", "key"}
assert entries["wrong"]["user"] == "fresh"
assert entries["key"]["method"] == "key"
'
  [ "$(grep -cE '^[[:space:]]*#' "$private_dir/inventories/first-entry.sops.yml")" \
    -eq "$initial_comment_count" ]

  # Replace the saved key for the wrong-password endpoint with another stand
  # key. StrictHostKeyChecking must stop before a password is sent.
  # The previous refusal deliberately persisted pending. Clear that synthetic
  # fixture before exercising the independent host-key mismatch branch.
  SOPS_AGE_KEY_FILE="$work/age-key.txt" python3 - "$ROOT" "$private_dir" "$work/fleet-key" <<'PY'
import importlib.util
from pathlib import Path
import sys

root, private, fleet_key = sys.argv[1:]
spec = importlib.util.spec_from_file_location("first_contact_stand_module", Path(root) / "scripts/first-contact.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
door = module.NetworkDoor("wrong", "127.0.0.1", "fresh", 22024, "password", True, Path(fleet_key))
assert module._clear_pending_password(door, Path(private), Path(private) / ".sops.yaml") is None
PY
  token='[127.0.0.1]:22024'
  ssh-keygen -R "$token" -f "$private_dir/inventories/known_hosts" >/dev/null 2>&1
  ssh-keyscan -T 5 -p 22023 127.0.0.1 2>/dev/null | \
    sed "s/^\[[^]]*\]:22023 /$token /" >> "$private_dir/inventories/known_hosts"
  set +e
  mismatch_output="$(run_apply "$private_dir" "$work/age-key.txt" wrong 2>&1)"
  mismatch_rc=$?
  set -e
  [ "$mismatch_rc" -ne 0 ]
  [[ "$mismatch_output" == *"на этом адресе и порту принят другой ключ"* ]]
  [[ "$mismatch_output" == *"known_hosts не изменён"* ]]

  # Leave a single already-completed entry and prove its removal serializes an
  # exhausted map explicitly, which is the hand-off contract for later chunks.
  first_entry_file="$private_dir/inventories/first-entry.sops.yml"
  : > "$first_entry_file"
  for number in $(seq 1 88); do
    printf '# stand production-shaped documentation %02d\n' "$number" >> "$first_entry_file"
  done
  cat >> "$first_entry_file" <<'EOF'
first_entry:
    expired:
        # password-expired provider branch
        user: fresh
        port: 22022
        method: password
        # owner-managed SOPS value
        password: Stand-Initial9!Password
EOF
  final_comment_count="$(grep -cE '^[[:space:]]*#' "$first_entry_file")"
  SOPS_AGE_KEY_FILE="$work/age-key.txt" python3 "$ROOT/scripts/secrets_lock.py" \
    "$first_entry_file" -- sops --config "$private_dir/.sops.yaml" \
    --encrypt --in-place "$first_entry_file"
  run_apply "$private_dir" "$work/age-key.txt" expired >/dev/null
  grep -qxF 'first_entry: {}' "$first_entry_file"
  [ "$(grep -cE '^[[:space:]]*#' "$first_entry_file")" -eq "$final_comment_count" ]

  ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o "UserKnownHostsFile=$private_dir/inventories/known_hosts" \
    -i "$work/fleet-key" -p 22022 fresh@127.0.0.1 true
  echo "first-contact stand: all scenarios passed"
}

if [ "${FIRST_CONTACT_STAND_LIBRARY:-0}" = 1 ]; then
  return 0
fi

case "${1:-}" in
  up) up ;;
  down) down; echo "first-contact stand: down" ;;
  test) test_stand ;;
  *) echo "Usage: $0 {up|down|test}" >&2; exit 2 ;;
esac
