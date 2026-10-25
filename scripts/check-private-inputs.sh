#!/usr/bin/env bash
# Отказ до сетевого действия: частные входы либо заполнены, либо работа не
# начинается. Никакого fallback к чужим значениям — незаполненный инвентарь
# это остановка, а не «возьмём что-нибудь».
#
# Значения не печатаются: скрипт сообщает только имя поля и файл.
#
# Usage: scripts/check-private-inputs.sh [node]      (default: все ноды)
set -euo pipefail
export LC_ALL=C

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRIVATE_DIR="${VPN_FLEET_PRIVATE_DIR:-$ROOT/private}"
INVENTORY_DIR="$PRIVATE_DIR/inventories/nodes"
SECRETS="$PRIVATE_DIR/inventories/group_vars/all/secrets.sops.yml"
SSH_KEYS="$PRIVATE_DIR/inventories/group_vars/all/ssh_keys.yml"
KNOWN_HOSTS="$PRIVATE_DIR/inventories/known_hosts"
SOPS_CONFIG="$PRIVATE_DIR/.sops.yaml"

fail=0
miss() { echo "MISSING: $*" >&2; fail=$((fail + 1)); }

for path in "$SOPS_CONFIG" "$SECRETS" "$SSH_KEYS" "$KNOWN_HOSTS"; do
  [ -f "$path" ] || miss "нет файла ${path#"$ROOT"/}"
done

if [ -f "$SOPS_CONFIG" ]; then
  grep -qE 'age1[0-9a-z]{50,}' "$SOPS_CONFIG" ||
    miss "${SOPS_CONFIG#"$ROOT"/}: нет настоящего age recipient"
  if grep -qF 'age1REPLACE_WITH_YOUR_OWN_RECIPIENT' "$SOPS_CONFIG"; then
    miss "${SOPS_CONFIG#"$ROOT"/}: остался плейсхолдер получателя"
  fi
fi

if [ -f "$SSH_KEYS" ]; then
  grep -qE '^[[:space:]]*-[[:space:]]*name:' "$SSH_KEYS" ||
    miss "${SSH_KEYS#"$ROOT"/}: ssh_authorized_keys_devops пуст — bootstrap оставит ноду без двери"
fi

if [ -f "$KNOWN_HOSTS" ]; then
  grep -qE '^[^#[:space:]]' "$KNOWN_HOSTS" ||
    miss "${KNOWN_HOSTS#"$ROOT"/}: host key ноды ещё не принят first-contact по TOFU"
fi

shopt -s nullglob
nodes=()
if [ "$#" -gt 0 ]; then
  nodes=("$INVENTORY_DIR/$1.yml")
  [ -f "${nodes[0]}" ] || miss "нет инвентаря ${nodes[0]#"$ROOT"/}"
else
  nodes=("$INVENTORY_DIR"/*.yml)
  [ "${#nodes[@]}" -gt 0 ] || miss "в ${INVENTORY_DIR#"$ROOT"/} нет ни одного инвентаря"
fi

for node in "${nodes[@]}"; do
  [ -f "$node" ] || continue
  rel="${node#"$ROOT"/}"
  # `|| true` обязателен: grep без совпадений возвращает 1, а под
  # `set -euo pipefail` это убивает скрипт молча, прямо здесь, ещё до единого
  # сообщения — то есть «плейсхолдеров нет» выглядело бы как отказ гейта без
  # причины.
  placeholders="$( { grep -nE '^[[:space:]]*[A-Za-z0-9_]+:[[:space:]]*REPLACE_ME[[:space:]]*(#.*)?$' "$node" ||
    true; } | sed -E 's/^([0-9]+):[[:space:]]*([A-Za-z0-9_]+):.*/\2(:\1)/' | tr '\n' ' ')"
  [ -z "$placeholders" ] || miss "$rel: не заполнены поля: $placeholders"

  node_name="$(sed -nE 's/^[[:space:]]*node_name:[[:space:]]*"?([A-Za-z0-9_-]+)"?[[:space:]]*$/\1/p' "$node" | head -1)"
  if [ -z "$node_name" ]; then
    miss "$rel: нет node_name"
  elif [ -f "$SECRETS" ]; then
    prefix="node_${node_name//-/_}"
    for suffix in \
      reality_private_key reality_short_id clients \
      hy2_password hy2_certificate hy2_key hy2_certificate_public_key_sha256; do
      grep -qE "^${prefix}_${suffix}:" "$SECRETS" ||
        miss "$rel: в секретах нет ${prefix}_${suffix} (имя ноды и префикс в SOPS обязаны совпадать)"
    done
  fi

  # Единственный владелец правила первого входа — Python-гейт. Shell только
  # включает его причины в общий список частных входов и не разбирает YAML.
  [ -z "$node_name" ] || {
    gate_reasons=""
    if ! gate_reasons="$(VPN_FLEET_PRIVATE_DIR="$PRIVATE_DIR" \
      python3 "$ROOT/scripts/first-contact.py" "$(basename "$node" .yml)" \
      --reasons-only 2>&1)"; then
      while IFS= read -r reason; do
        [ -z "$reason" ] || miss "$reason"
      done <<< "$gate_reasons"
    fi
  }
done

if [ "$fail" -ne 0 ]; then
  echo "ERROR: частные входы не готовы ($fail) — сетевые шаги 51b не запускать" >&2
  exit 1
fi
echo "check-private-inputs: ok"
