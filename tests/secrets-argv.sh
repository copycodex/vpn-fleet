#!/usr/bin/env bash
# Observe synthetic client credentials in /proc while the public make targets run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REAL_ANSIBLE_PLAYBOOK="$(command -v ansible-playbook)"
REAL_JQ="$(command -v jq)"
REAL_QRENCODE="$(command -v qrencode)"
WORK="$(mktemp -d)"
BIN="$WORK/bin"
FIXTURE="$WORK/fixture"
ARTIFACTS="$WORK/artifacts"
MARKERS="$WORK/markers"
PHASE="$WORK/phase"
FINDINGS="$WORK/findings"
# Unique markers keep a concurrent checkout's synthetic process arguments out
# of this run's observation while still exercising the same credential shape.
EXPORT_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
NEW_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

cleanup() {
  rm -rf "$WORK"
}
trap cleanup EXIT
umask 077
mkdir -p "$BIN" "$FIXTURE" "$ARTIFACTS"

cat >"$FIXTURE/inventory.yml" <<EOF
all:
  hosts:
    ci:
      ansible_host: 192.0.2.159
      ansible_user: synthetic
      ansible_connection: local
      ansible_ssh_private_key_file: /tmp/synthetic-identity
      node_name: ci
      node_location: test
      singbox_vless_security: tls
      singbox_vless_tls_server_name: vpn.test.invalid
      singbox_hy2_enabled: true
      singbox_legacy_client_names: []
      singbox_client_names: [ci-client]
      node_ci_clients:
        - name: ci-client
          uuid: $EXPORT_UUID
      node_ci_hy2_password: synthetic-hy2-password-159h-long
      node_ci_hy2_certificate: |-
        -----BEGIN CERTIFICATE-----
        synthetic
        -----END CERTIFICATE-----
      node_ci_hy2_certificate_public_key_sha256: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
EOF

cat >"$FIXTURE/node.yml" <<'EOF'
all:
  hosts:
    ci:
      node_name: ci
      singbox_client_names:
        - ci-client
EOF
printf '{"node_ci_clients":[{"name":"ci-client","uuid":"%s"}]}\n' \
  "$EXPORT_UUID" >"$FIXTURE/secrets.sops.yml"
printf '%s\n' 'creation_rules: []' >"$FIXTURE/.sops.yaml"

# The observer reads values from a 0600 file; credentials are never its argv.
printf 'UUID\t%s\n' "$EXPORT_UUID" >"$MARKERS"
printf 'UUID\t%s\n' "$NEW_UUID" >>"$MARKERS"
printf 'VLESS_URL\tvless://%s@' "$EXPORT_UUID" >>"$MARKERS"

cat >"$BIN/ansible-playbook" <<EOF
#!/usr/bin/env bash
set -euo pipefail
if [[ "\$*" == *playbooks/add-client.yml* ]]; then
  exec env \
    VPN_FLEET_SECRETS_FILE="$FIXTURE/secrets.sops.yml" \
    VPN_FLEET_SOPS_FILENAME_OVERRIDE="$FIXTURE/secrets.sops.yml" \
    VPN_FLEET_SOPS_CONFIG="$FIXTURE/.sops.yaml" \
    VPN_FLEET_INVENTORY_FILE="$FIXTURE/node.yml" \
    "$ROOT/scripts/add-client.sh" ci argv-test-client
fi
exec "$REAL_ANSIBLE_PLAYBOOK" -i "$FIXTURE/inventory.yml" "\$@" \
  -e clients_dir="$ARTIFACTS" \
  -e singbox_export_bin="$BIN/sing-box" \
  -e singbox_legacy_bin="$BIN/sing-box"
EOF

cat >"$BIN/sing-box" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == version ]]; then
  printf '%s\n' 'sing-box version 1.13.13'
fi
exit 0
EOF

cat >"$BIN/qrencode" <<EOF
#!/usr/bin/env bash
set -euo pipefail
if [[ "\${1:-}" == --version ]]; then
  exec "$REAL_QRENCODE" "\$@"
fi
case "\$*" in
  *vless://*) sleep 1 ;;
esac
exec "$REAL_QRENCODE" "\$@"
EOF

cat >"$BIN/uuidgen" <<EOF
#!/usr/bin/env bash
printf '%s\n' '$NEW_UUID'
EOF

cat >"$BIN/jq" <<EOF
#!/usr/bin/env bash
set -euo pipefail
case "\$*" in
  *$NEW_UUID*) sleep 1 ;;
esac
exec "$REAL_JQ" "\$@"
EOF

cat >"$BIN/sops" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case " $* " in
  *" --encrypt "*) cat ;;
  *" --decrypt "*) cat "${@: -1}" ;;
  *) exit 2 ;;
esac
EOF
chmod 0700 "$BIN"/*

python3 - "$MARKERS" "$PHASE" "$FINDINGS" <<'PY' &
import pathlib
import sys
import time

markers_path, phase_path, findings_path = map(pathlib.Path, sys.argv[1:])
markers = []
for line in markers_path.read_bytes().splitlines():
    kind, value = line.split(b"\t", 1)
    markers.append((kind.decode(), value))
seen = set()
while True:
    try:
        phase = phase_path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        phase = "setup"
    if phase == "done":
        break
    for cmdline_path in pathlib.Path("/proc").glob("[0-9]*/cmdline"):
        try:
            cmdline = cmdline_path.read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for kind, value in markers:
            key = (phase, kind)
            if phase != "setup" and value in cmdline and key not in seen:
                with findings_path.open("a", encoding="ascii") as stream:
                    stream.write(f"{phase}\t{kind}\n")
                seen.add(key)
    time.sleep(0.002)
PY
observer_pid=$!

printf '%s\n' export >"$PHASE"
PATH="$BIN:$PATH" make -s -C "$ROOT" export NODE=ci >/dev/null
printf '%s\n' add-client >"$PHASE"
PATH="$BIN:$PATH" make -s -C "$ROOT" add-client NODE=ci NAME=argv-test-client >/dev/null
printf '%s\n' 'done' >"$PHASE"
wait "$observer_pid"

if [[ -n "${ARGV_TEST_ARTIFACT_DIR:-}" ]]; then
  mkdir -p "$ARGV_TEST_ARTIFACT_DIR"
  cp -a "$ARTIFACTS/." "$ARGV_TEST_ARTIFACT_DIR/"
  cp -a "$FIXTURE/secrets.sops.yml" "$ARGV_TEST_ARTIFACT_DIR/add-client-secrets.sops.yml"
  cp -a "$FIXTURE/node.yml" "$ARGV_TEST_ARTIFACT_DIR/add-client-inventory.yml"
fi

status=0
for phase_name in export add-client; do
  for marker_kind in UUID VLESS_URL; do
    if [[ -f "$FINDINGS" ]] && grep -Fqx "$phase_name"$'\t'"$marker_kind" "$FINDINGS"; then
      printf 'not ok - %s exposed synthetic %s in /proc/*/cmdline\n' \
        "$phase_name" "$marker_kind"
      status=1
    fi
  done
done
if ((status != 0)); then
  exit "$status"
fi
printf '%s\n' 'ok - export and add-client kept synthetic credentials out of process argv'
