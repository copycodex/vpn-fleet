#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
fixture_dir="$(mktemp -d)"
trap 'rm -rf "$fixture_dir"' EXIT
python3 tests/reality-keypair-fixture.py >"$fixture_dir/good.json"
python3 tests/reality-keypair-fixture.py --wrong-public >"$fixture_dir/bad.json"

python3 scripts/check-reality-keypair.py <"$fixture_dir/good.json" >/dev/null
if python3 scripts/check-reality-keypair.py \
    <"$fixture_dir/bad.json" >"$fixture_dir/output" 2>&1; then
    echo 'ERROR: mismatched Reality pair passed the local check' >&2
    exit 1
fi
if grep -Fq 'PrivateKey:' "$fixture_dir/output"; then
    echo 'ERROR: key material escaped into the check output' >&2
    exit 1
fi

if ansible-playbook -i tests/inventory playbooks/site.yml \
    --check --tags reality_preflight -e ansible_become=false \
    -e "@$fixture_dir/bad.json" >"$fixture_dir/output" 2>&1; then
    echo 'ERROR: mismatched Reality pair passed site preflight' >&2
    exit 1
fi
if ! grep -Fq 'Verify the Reality private key' "$fixture_dir/output"; then
    echo 'ERROR: site preflight failed before the key-pair gate' >&2
    exit 1
fi
if ansible-playbook -i tests/inventory playbooks/export-clients.yml \
    --check -e "@$fixture_dir/bad.json" >"$fixture_dir/output" 2>&1; then
    echo 'ERROR: mismatched Reality pair passed client export' >&2
    exit 1
fi
if ! grep -Fq 'Verify the Reality private key' "$fixture_dir/output"; then
    echo 'ERROR: client export failed before the key-pair gate' >&2
    exit 1
fi
ansible-playbook -i tests/inventory playbooks/site.yml \
    --check --tags reality_preflight -e ansible_become=false \
    -e "@$fixture_dir/good.json" >/dev/null
echo 'reality key pair: ok'
