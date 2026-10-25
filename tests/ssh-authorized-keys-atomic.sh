#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="vpn-fleet-authorized-keys-atomic:test"

if [ "${AUTHORIZED_KEYS_TEST_IN_CONTAINER:-0}" != 1 ]; then
  command -v docker >/dev/null 2>&1 || {
    echo 'FAIL: docker is required for the isolated authorized_keys test' >&2
    exit 1
  }

  docker build --quiet \
    --file "$ROOT/tests/ssh-authorized-keys-atomic.Dockerfile" \
    --tag "$IMAGE" "$ROOT/tests" >/dev/null
  docker run --rm \
    --network none \
    --env AUTHORIZED_KEYS_TEST_IN_CONTAINER=1 \
    --volume "$ROOT:/workspace:ro" \
    "$IMAGE" \
    bash /workspace/tests/ssh-authorized-keys-atomic.sh
  exit
fi

fixture=/tmp/vpn-fleet-authorized-keys
home=/home/keytest
authorized_keys="$home/.ssh/authorized_keys"
failures=0
monitor_pid=
sshd_pid=

cleanup() {
  if [ -n "$monitor_pid" ]; then
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true
  fi
  if [ -n "$sshd_pid" ]; then
    kill "$sshd_pid" 2>/dev/null || true
    wait "$sshd_pid" 2>/dev/null || true
  fi
  rm -rf "$fixture"
}
trap cleanup EXIT

pass() {
  echo "ok: $1"
}

fail() {
  echo "FAIL: $1" >&2
  failures=$((failures + 1))
}

fingerprints() {
  ssh-keygen -lf "$1" 2>/dev/null | awk '{print $2}' | sort | paste -sd, -
}

mkdir -p "$fixture" /run/sshd
useradd --create-home --shell /bin/sh keytest
passwd -d keytest >/dev/null
install -d -m 0700 -o keytest -g keytest "$home/.ssh"

for key in old-one old-two new-one new-two; do
  ssh-keygen -q -t ed25519 -N '' -C "$key" -f "$fixture/$key"
done

cat "$fixture/old-one.pub" "$fixture/old-two.pub" > "$authorized_keys"
chown keytest:keytest "$authorized_keys"
chmod 0600 "$authorized_keys"
cp "$authorized_keys" "$fixture/old-authorized-keys"
cat "$fixture/new-one.pub" "$fixture/new-two.pub" > "$fixture/new-authorized-keys"
old_fingerprints="$(fingerprints "$fixture/old-authorized-keys")"
new_fingerprints="$(fingerprints "$fixture/new-authorized-keys")"

cat > "$fixture/watch-authorized-keys.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

directory="$1"
target="$2"
old_fingerprints="$3"
new_fingerprints="$4"
events="$5"
invalid="$6"

inotifywait --monitor --quiet --event moved_to --format '%f' "$directory" |
  while IFS= read -r name; do
    [ "$name" = authorized_keys ] || continue
    current="$(ssh-keygen -lf "$target" 2>/dev/null | awk '{print $2}' | sort | paste -sd, - || true)"
    printf '%s\n' "$current" >> "$events"
    if [ "$current" != "$old_fingerprints" ] && [ "$current" != "$new_fingerprints" ]; then
      printf '%s\n' "$current" >> "$invalid"
    fi
  done
EOF
chmod 0755 "$fixture/watch-authorized-keys.sh"
: > "$fixture/events"
: > "$fixture/invalid"
"$fixture/watch-authorized-keys.sh" \
  "$home/.ssh" "$authorized_keys" "$old_fingerprints" "$new_fingerprints" \
  "$fixture/events" "$fixture/invalid" &
monitor_pid=$!
sleep 0.2

ANSIBLE_CONFIG=/workspace/ansible.cfg \
  ANSIBLE_ROLES_PATH=/workspace/roles \
  ansible-playbook -i localhost, \
    /workspace/tests/ssh-authorized-keys-atomic.yml \
    > "$fixture/apply.log" 2>&1 || {
      tail -n 80 "$fixture/apply.log" >&2
      fail 'authorized_keys role failed'
    }

sleep 0.2
kill "$monitor_pid" 2>/dev/null || true
wait "$monitor_pid" 2>/dev/null || true
monitor_pid=

event_count="$(wc -l < "$fixture/events")"
if [ -s "$fixture/invalid" ] || [ "$event_count" -ne 1 ]; then
  fail "atomic replacement invariant violated (replacement events=$event_count; expected exactly one old-or-new transition)"
else
  pass 'atomic replacement invariant: every observed file was the old or new complete set'
fi

if grep -qF '\n' "$authorized_keys"; then
  fail 'authorized_keys contains a literal \\n instead of real line feeds'
else
  pass 'authorized_keys contains real line feeds, not a literal \\n'
fi

cat > "$fixture/sshd_config" <<EOF
Port 2222
ListenAddress 127.0.0.1
HostKey $fixture/host-key
PidFile $fixture/sshd.pid
AuthorizedKeysFile .ssh/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin no
AllowUsers keytest
LogLevel ERROR
EOF
ssh-keygen -q -t ed25519 -N '' -f "$fixture/host-key"
/usr/sbin/sshd -D -e -f "$fixture/sshd_config" > "$fixture/sshd.log" 2>&1 &
sshd_pid=$!
sleep 0.2

ssh_with_key() {
  ssh -q \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -p 2222 -i "$1" keytest@127.0.0.1 true
}

if ssh_with_key "$fixture/new-one" && ssh_with_key "$fixture/new-two"; then
  pass 'all declared keys authenticate through sshd'
else
  fail 'not all declared keys authenticate through sshd'
fi

ANSIBLE_CONFIG=/workspace/ansible.cfg \
  ANSIBLE_ROLES_PATH=/workspace/roles \
  ansible-playbook -i localhost, \
    /workspace/tests/ssh-authorized-keys-atomic.yml \
    > "$fixture/idempotent.log" 2>&1 || {
      tail -n 80 "$fixture/idempotent.log" >&2
      fail 'repeated authorized_keys role run failed'
    }

if grep -Eq 'changed=0[[:space:]]+unreachable=0[[:space:]]+failed=0' \
  "$fixture/idempotent.log"; then
  pass 'repeated run is idempotent (changed=0)'
else
  sed -n '/PLAY RECAP/,$p' "$fixture/idempotent.log" >&2
  fail 'repeated authorized_keys role run was not idempotent'
fi

if [ "$failures" -ne 0 ]; then
  echo "authorized_keys atomic test: $failures failure(s)" >&2
  exit 1
fi

echo 'authorized_keys atomic test: all checks passed'
