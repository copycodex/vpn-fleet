#!/usr/bin/env bash
# Crash at every discovered side effect, then converge on loopback sshd.
set -euo pipefail
export LC_ALL=C
# shellcheck source=tests/first-contact-stand.sh
FIRST_CONTACT_STAND_LIBRARY=1 source "$(dirname "$0")/first-contact-stand.sh"

resume_image=vpn-fleet-first-contact-stand:local
resume_container=vpn-fleet-first-contact-interrupt
resume_work=""

cleanup_resume() {
  docker rm -f "$resume_container" >/dev/null 2>&1 || true
  if [ -n "$resume_work" ]; then rm -rf "$resume_work"; fi
}

reset_node() {
  local port="$1" expire="$2"
  docker rm -f "$resume_container" >/dev/null 2>&1 || true
  docker run -d --name "$resume_container" -e "EXPIRE_PASSWORD=$expire" \
    -p "127.0.0.1:$port:22" "$resume_image" >/dev/null
  local ready=0
  for _ in $(seq 1 40); do
    if ssh-keyscan -T 1 -p "$port" 127.0.0.1 >/dev/null 2>&1; then
      ready=1; break
    fi
    sleep 0.25
  done
  [ "$ready" -eq 1 ] || { echo 'resume stand: sshd did not start' >&2; exit 1; }
}

verify_converged() {
  local private_dir="$1" node="$2" age_key="$3" fleet_key="$4" port="$5"
  SOPS_AGE_KEY_FILE="$age_key" python3 - "$ROOT" "$private_dir" "$node" "$fleet_key" "$port" <<'PY'
import importlib.util
from pathlib import Path
import subprocess
import sys
import pexpect
import yaml

root, private, node, fleet_key, port = sys.argv[1:]
private = Path(private)
spec = importlib.util.spec_from_file_location("stand_first_contact", Path(root) / "scripts/first-contact.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
config = private / ".sops.yaml"
secrets = private / "inventories/group_vars/all/secrets.sops.yml"
key = f"node_{node}_console_password"
value = subprocess.run(["sops", "--config", str(config), "--decrypt", "--extract",
                        f'["{key}"]', str(secrets)], capture_output=True, text=True, check=True).stdout.rstrip("\n")
assert value
door = module.NetworkDoor(node, "127.0.0.1", "fresh", int(port), "password", False, Path(fleet_key))
known = private / "inventories/known_hosts"
child = pexpect.spawn("ssh", ["-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
                              "-o", "NumberOfPasswordPrompts=1", "-o", "StrictHostKeyChecking=yes",
                              "-o", f"UserKnownHostsFile={known}", "-p", port,
                              "fresh@127.0.0.1", "true"], encoding="utf-8", timeout=15)
assert child.expect(["password:", pexpect.EOF, pexpect.TIMEOUT]) == 0
child.sendline(value)
assert child.expect([pexpect.EOF, pexpect.TIMEOUT]) == 0
child.close()
assert child.exitstatus == 0, "saved console password does not open sshd"
assert module._key_opens(door, known), "fleet key does not open sshd"
assert not module._pending_exists(door, private), "pending remains"
first = yaml.safe_load((private / "inventories/first-entry.sops.yml").read_text())
assert node not in (first.get("first_entry") or {}), "first_entry remains"
PY
}

run_crash_matrix() {
  local node="$1" port="$2" expire="$3" base="$4" age_key="$5" fleet_key="$6"
  local k=1 private_dir counter rc retry_rc kind phase path entry_live pending_paths=0
  while :; do
    reset_node "$port" "$expire"
    private_dir="$resume_work/$node-$k"
    cp -a "$base" "$private_dir"
    counter="$resume_work/effects-$node-$k"
    set +e
    output="$(VPN_FLEET_PRIVATE_DIR="$private_dir" SOPS_AGE_KEY_FILE="$age_key" \
      STAND_K="$k" STAND_COUNTER_FILE="$counter" PYTHONPATH="$resume_work/hook" \
      python3 "$ROOT/scripts/first-contact.py" "$node" --apply 2>&1)"
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
      verify_converged "$private_dir" "$node" "$age_key" "$fleet_key" "$port"
      [ "$pending_paths" -gt 0 ] || {
        echo "resume stand: $node never resumed by pending with first_entry" >&2; return 1;
      }
      echo "resume stand: $node discovered $((k - 1)) interruption points; pending retries=$pending_paths; completed at k=$k"
      break
    fi
    if [ "$rc" -ne 97 ]; then
      echo "resume stand: $node k=$k initial run failed rc=$rc" >&2
      echo "$output" >&2
      return 1
    fi
    entry_live="$(python3 - "$private_dir/inventories/first-entry.sops.yml" "$node" <<'PY'
import sys
import yaml
with open(sys.argv[1], encoding="utf-8") as source:
    entries = yaml.safe_load(source).get("first_entry") or {}
print("live" if sys.argv[2] in entries else "cleared")
PY
)"
    set +e
    retry="$(run_apply "$private_dir" "$age_key" "$node" 2>&1)"
    retry_rc=$?
    set -e
    if [ "$retry_rc" -ne 0 ]; then
      echo "resume stand: $node k=$k retry failed rc=$retry_rc" >&2
      echo "$retry" >&2
      return 1
    fi
    verify_converged "$private_dir" "$node" "$age_key" "$fleet_key" "$port"
    IFS='|' read -r _ kind phase < "$counter"
    if [[ "$retry" == *'фактический пароль pending'* ]]; then
      path=pending
      if [ "$entry_live" = live ]; then
        pending_paths=$((pending_paths + 1))
      fi
    elif [[ "$retry" == *'фактический пароль provider'* || "$retry" == *'пароль первого входа сменён'* || "$retry" == *'готово — ключ установлен'* ]]; then
      path=provider
    elif [[ "$retry" == *'ключ уже открывает ноду'* || "$retry" == *'уже сделано'* ]]; then
      path=key
    else
      echo "resume stand: $node k=$k retry path unknown" >&2; exit 1
    fi
    echo "resume stand: $node k=$k kind=$kind phase=$phase retry=$path first_entry=$entry_live recovered"
    k=$((k + 1))
  done
}

test_resume() {
  resume_work="$(mktemp -d)"
  trap cleanup_resume EXIT
  docker build -q -t "$resume_image" "$ROOT/tests/first-contact-stand" >/dev/null
  mkdir -p "$resume_work/hook"
  cp "$ROOT/tests/first-contact-stand-interrupt.py" "$resume_work/hook/sitecustomize.py"
  age-keygen -o "$resume_work/age-key.txt" >/dev/null 2>&1
  local recipient
  recipient="$(age-keygen -y "$resume_work/age-key.txt")"
  ssh-keygen -q -t ed25519 -N '' -f "$resume_work/fleet-key"
  local base="$resume_work/base"
  make_private_tree "$base" "$recipient" "$resume_work/fleet-key" "$resume_work/age-key.txt"
  run_crash_matrix expired 22022 1 "$base" "$resume_work/age-key.txt" "$resume_work/fleet-key"
  run_crash_matrix normal 22023 0 "$base" "$resume_work/age-key.txt" "$resume_work/fleet-key"

  # Both known passwords are wrong. Record a pending candidate before taking
  # the ciphertext snapshot; the recovery attempt must make no SOPS write.
  reset_node 22023 0
  local denied="$resume_work/denied"
  cp -a "$base" "$denied"
  SOPS_AGE_KEY_FILE="$resume_work/age-key.txt" python3 - "$ROOT" "$denied" "$resume_work/fleet-key" <<'PY'
import importlib.util
from pathlib import Path
import sys
root, private, fleet_key = sys.argv[1:]
spec = importlib.util.spec_from_file_location("stand_first_contact", Path(root) / "scripts/first-contact.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
door = module.NetworkDoor("normal", "127.0.0.1", "fresh", 22023, "password", True, Path(fleet_key))
assert module._store_pending_password(door, Path(private), Path(private) / ".sops.yaml", "Stand-Pending9!Password") is None
PY
  docker exec "$resume_container" sh -c "echo 'fresh:Stand-Third9!Password' | chpasswd"
  local secrets="$denied/inventories/group_vars/all/secrets.sops.yml"
  local before after denied_output denied_rc failed
  before="$(sha256sum "$secrets" "$denied/inventories/first-entry.sops.yml")"
  set +e
  denied_output="$(run_apply "$denied" "$resume_work/age-key.txt" normal 2>&1)"
  denied_rc=$?
  set -e
  after="$(sha256sum "$secrets" "$denied/inventories/first-entry.sops.yml")"
  [ "$denied_rc" -ne 0 ] && [ "$before" = "$after" ] || {
    echo 'resume stand: neither-password case changed SOPS or succeeded' >&2; exit 1;
  }
  [[ "$denied_output" == *'ни один известный пароль'* ]] || {
    echo 'resume stand: neither-password case had no named reason' >&2; exit 1;
  }
  failed="$(docker logs "$resume_container" 2>&1 | grep -c 'Failed password for fresh' || true)"
  [ "$failed" -le 2 ] || { echo "resume stand: too many SSH password failures ($failed)" >&2; exit 1; }
  echo "resume stand: neither accepted; SOPS unchanged; failed SSH passwords=$failed"

  # Owner removed first_entry by hand after the new password took effect.
  reset_node 22023 0
  local orphan="$resume_work/orphan"
  cp -a "$base" "$orphan"
  SOPS_AGE_KEY_FILE="$resume_work/age-key.txt" python3 - "$ROOT" "$orphan" "$resume_work/fleet-key" <<'PY'
import importlib.util
from pathlib import Path
import sys
root, private, fleet_key = sys.argv[1:]
spec = importlib.util.spec_from_file_location("stand_first_contact", Path(root) / "scripts/first-contact.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
door = module.NetworkDoor("normal", "127.0.0.1", "fresh", 22023, "password", True, Path(fleet_key))
private = Path(private)
config = private / ".sops.yaml"
assert module._store_pending_password(door, private, config, "Stand-Pending9!Password") is None
assert module._remove_first_entry(door, private, config) is None
PY
  docker exec "$resume_container" sh -c "echo 'fresh:Stand-Pending9!Password' | chpasswd"
  run_apply "$orphan" "$resume_work/age-key.txt" normal >/dev/null
  verify_converged "$orphan" normal "$resume_work/age-key.txt" "$resume_work/fleet-key" 22023
  echo 'resume stand: pending accepted without first_entry; converged'
}

case "${1:-}" in
  test) test_resume ;;
  *) echo "Usage: $0 test" >&2; exit 2 ;;
esac
