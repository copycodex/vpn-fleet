#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
preflight_inventory=tests/inventory
test_inventory="$(mktemp -d)"
trap 'rm -rf "$test_inventory"' EXIT
mkdir -p "$test_inventory/nodes"
python3 tests/reality-keypair-fixture.py >"$test_inventory/pair.json"
sed '/^      xray_version:/d' tests/inventory/nodes/ci.yml \
    >"$test_inventory/nodes/ci.yml"

check_failure() {
    local label="$1"
    local expected="$2"
    shift 2
    local output
    output="$(mktemp)"
    if ansible-playbook -i "$preflight_inventory" playbooks/site.yml \
        --check --tags reality_preflight -e ansible_become=false \
        -e "@$test_inventory/pair.json" "$@" >"$output" 2>&1; then
        echo "ERROR: $label passed" >&2
        rm -f "$output"
        exit 1
    fi
    if ! grep -Fq "$expected" "$output"; then
        echo "ERROR: $label failed for a different reason" >&2
        cat "$output" >&2
        rm -f "$output"
        exit 1
    fi
    rm -f "$output"
}

preflight_inventory="$test_inventory"
check_failure 'undeclared Xray version' 'xray_version must be declared' \
    -e xray_listen=127.0.0.1 -e reality_frontend_enabled=true
preflight_inventory=tests/inventory
check_failure 'loopback without frontend' 'reality_frontend_enabled' \
    -e xray_listen=127.0.0.1 -e xray_listen_port=1443 \
    -e reality_frontend_enabled=false
check_failure 'frontend without loopback' 'loopback' \
    -e xray_listen=0.0.0.0 -e reality_frontend_enabled=true
check_failure 'frontend/backend mismatch' 'reality_frontend_backend_port does not match Xray' \
    -e xray_listen=127.0.0.1 -e reality_frontend_enabled=true \
    -e reality_frontend_backend_port=1555
check_failure 'missing Xray checksum' 'xray_checksums' \
    -e xray_version=0.0.0 -e reality_frontend_enabled=true \
    -e xray_listen=127.0.0.1

ansible-playbook -i tests/inventory playbooks/site.yml \
    --check --tags reality_preflight -e ansible_become=false \
    -e xray_listen=127.0.0.1 -e xray_listen_port=1443 \
    -e reality_frontend_enabled=true \
    -e "@$test_inventory/pair.json" >/dev/null
echo 'reality frontend preflight: ok'
