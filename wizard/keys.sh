#!/usr/bin/env bash
# Шаг 1 визарда: ключи установки.
#
# Ключей ровно два и они общие на все серверы: age шифрует частное дерево,
# SSH-ключ админа открывает любую ноду. Имена серверов появляются позже и живут
# в инвентаре.
#
# Имя установки нигде не зашито:
#   - задано явно                   → берём его;
#   - не задано и ключей нет        → vpn-fleet-01;
#   - не задано и ключ ровно один   → берём его, ничего не создаём;
#   - не задано, а ключей несколько → в терминале спросим, иначе отказ.
# Тем же правилом живёт Makefile, поэтому обычно переменную писать не нужно.
#
# Ничего не перезаписывает: перезапись age-ключа — это потеря доступа ко всем
# секретам парка, и такой команды здесь быть не может. Значения ключей не
# печатаются никогда — только пути.
#
# Usage: wizard/keys.sh
#        FLEET=<имя> wizard/keys.sh
#
# Переопределяется окружением: FLEET, AGE_KEY_FILE, FLEET_KEY_FILE, AGE_DIR, SSH_DIR.
set -euo pipefail
export LC_ALL=C.UTF-8

WIZARD_STEP="keys"
# shellcheck source=wizard/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

FLEET_NAME_DEFAULT="vpn-fleet"
FLEET="${FLEET:-}"

EXISTING=()

age_key_of()    { echo "$AGE_DIR/$1.txt"; }
fleet_key_of() { echo "$SSH_DIR/${1}_ed25519"; }

# Первое свободное имя вида vpn-fleet-NN: свободным считается то, у которого
# не занят ни один из двух ключей.
free_fleet_name() {
  local index=1 candidate
  while [ "$index" -le 99 ]; do
    candidate="$(printf '%s-%02d' "$FLEET_NAME_DEFAULT" "$index")"
    if [ ! -e "$(age_key_of "$candidate")" ] &&
       [ ! -e "$(fleet_key_of "$candidate")" ]; then
      echo "$candidate"
      return 0
    fi
    index=$((index + 1))
  done
  echo "${FLEET_NAME_DEFAULT}-$(date +%Y%m%d)"
}

# SSH-ключи, которые уже лежат в каталоге: приватный файл, рядом с которым есть
# .pub. Имя ключа установки может не совпадать с конвенцией — тогда скрипт
# обязан показать, что есть, а не заводить молча второй ключ.
existing_ssh_keys() {
  local path name owner
  shopt -s nullglob
  for path in "$SSH_DIR"/*; do
    case "$path" in
      *.pub | *known_hosts* | *config* | *authorized_keys*) continue ;;
    esac
    [ -f "$path" ] && [ -f "$path.pub" ] || continue
    name="$(basename "$path")"

    # Ключ, принадлежащий ДРУГОЙ установке, в список не попадает. Принятие —
    # это переименование файла: приняв чужой ключ, мы оставили бы ту установку
    # без ключа, и узналось бы это на первом же её прогоне.
    owner="${name%_ed25519}"
    if [ "$owner" != "$name" ] && [ "$owner" != "$FLEET" ] &&
       [ -e "$(age_key_of "$owner")" ]; then
      continue
    fi

    echo "$name"
  done
  shopt -u nullglob
}

# Приводит имя выбранного ключа к конвенции <установка>_ed25519: после этого
# скрипт, Makefile и инвентарь работают без единой переменной.
adopt_ssh_key() {
  local chosen="$1" target="$2"
  mv "$SSH_DIR/$chosen" "$target"
  mv "$SSH_DIR/$chosen.pub" "$target.pub"
  say "ключ $chosen переименован в $(basename "$target"): имя приведено к конвенции"
  warn "если на старое имя ссылался ~/.ssh/config или инвентарь — поправь их"
}

# Спрашивает имя установки. Подсказки и ошибки уходят в stderr: stdout здесь —
# возвращаемое значение, и мусор в нём испортил бы имя ключа.
ask_fleet_name() {
  local suggested="$1" answer
  while :; do
    read -r -p "имя установки [$suggested]: " answer
    answer="${answer:-$suggested}"
    if ! [[ "$answer" =~ ^[A-Za-z0-9_-]+$ ]]; then
      echo "  имя — буквы, цифры, дефис и подчёркивание" >&2
      continue
    fi
    if [ -e "$(age_key_of "$answer")" ] || [ -e "$(fleet_key_of "$answer")" ]; then
      echo "  это имя занято: ключи с ним уже лежат, а перезаписывать их нельзя" >&2
      continue
    fi
    echo "$answer"
    return 0
  done
}

# Имя не зашито: явное побеждает, иначе берём единственный существующий ключ,
# иначе заводим первый свободный. Несколько ключей без явного имени — отказ,
# потому что угадывать, каким из них открывается парк, скрипт не вправе.
resolve_fleet() {
  if [ -n "$FLEET" ]; then
    [[ "$FLEET" =~ ^[A-Za-z0-9_-]+$ ]] ||
      fail "FLEET=$FLEET: допустимы буквы, цифры, дефис и подчёркивание"
    return 0
  fi

  local found=() path name
  shopt -s nullglob
  for path in "$AGE_DIR"/*.txt; do
    name="$(basename "$path" .txt)"
    found+=("$name")
  done
  shopt -u nullglob

  case "${#found[@]}" in
    0)
      if interactive; then
        echo
        echo "Ключей установки ещё нет — заводим первую."
        FLEET="$(ask_fleet_name "$(free_fleet_name)")"
      else
        FLEET="$(free_fleet_name)"
        say "ключей ещё нет, беру имя по умолчанию: $FLEET"
      fi
      ;;
    1)
      FLEET="${found[0]}"
      say "нашёл единственный набор ключей, беру его: $FLEET"
      ;;
    *)
      if ! interactive; then
        fail "в $AGE_DIR несколько ключей (${found[*]}) — назови нужный: FLEET=<имя>"
      fi
      choose "В $AGE_DIR несколько наборов ключей. Какой берём?" "${found[@]}"
      if [ -n "$CHOICE" ]; then
        FLEET="$CHOICE"
        say "беру существующий набор: $FLEET"
      else
        FLEET="$(ask_fleet_name "$(free_fleet_name)")"
        say "завожу новый набор: $FLEET"
      fi
      ;;
  esac
}

step_age_key() {
  if [ -f "$AGE_KEY_FILE" ]; then
    say "age-ключ уже есть, не трогаю: $AGE_KEY_FILE"
    EXISTING+=("$AGE_KEY_FILE")
    return 0
  fi
  command -v age-keygen >/dev/null 2>&1 || fail "не найден age-keygen"
  mkdir -p "$(dirname "$AGE_KEY_FILE")"
  ( umask 077; age-keygen -o "$AGE_KEY_FILE" >/dev/null 2>&1 )
  chmod 600 "$AGE_KEY_FILE"
  say "age-ключ создан: $AGE_KEY_FILE"
  say "  сделай его резервную копию вне машины: потеря = потеря всех секретов"
}

step_fleet_key() {
  if [ -f "$FLEET_KEY_FILE" ]; then
    say "SSH-ключ уже есть, не трогаю: $FLEET_KEY_FILE"
    EXISTING+=("$FLEET_KEY_FILE")
    return 0
  fi
  # Ключа с ожидаемым именем нет. Прежде чем заводить новый, показываем те,
  # что уже лежат рядом: чаще всего нужный ключ есть, просто зовётся иначе.
  local candidates=()
  mapfile -t candidates < <(existing_ssh_keys)
  if [ "${#candidates[@]}" -gt 0 ]; then
    if ! interactive; then
      warn "нет $FLEET_KEY_FILE, но в $SSH_DIR уже есть ключи: ${candidates[*]}"
      fail "назови нужный явно: FLEET_KEY_FILE=$SSH_DIR/<ключ> $0"
    fi
    choose "Ключа $(basename "$FLEET_KEY_FILE") нет, а в $SSH_DIR есть:" "${candidates[@]}"
    if [ -n "$CHOICE" ]; then
      # Принятый ключ в EXISTING не попадает: этот список означает «шаг ничего
      # не делал, всё уже лежало», а здесь шаг как раз сделал работу.
      adopt_ssh_key "$CHOICE" "$FLEET_KEY_FILE"
      return 0
    fi
  fi

  mkdir -p "$(dirname "$FLEET_KEY_FILE")"
  chmod 700 "$(dirname "$FLEET_KEY_FILE")"
  ssh-keygen -t ed25519 -N "" -C "$FLEET" -f "$FLEET_KEY_FILE" >/dev/null
  say "SSH-ключ создан: $FLEET_KEY_FILE"
}

warn_existing_keys() {
  [ "${#EXISTING[@]}" -gt 0 ] || return 0
  local free path
  free="$(free_fleet_name)"
  warn "ключи «$FLEET» уже были на месте и НЕ перезаписаны:"
  for path in "${EXISTING[@]}"; do
    warn "  $path"
  done
  warn "Это повторный запуск той же установки — всё в порядке, так и надо."
  warn "Если установка ДРУГАЯ — возьми свободное имя:"
  warn "  FLEET=$free wizard/keys.sh"
  warn "  и дальше зови make с тем же именем: make <target> FLEET=$free"
}


require_repo
resolve_fleet
AGE_KEY_FILE="${AGE_KEY_FILE:-$(age_key_of "$FLEET")}"
FLEET_KEY_FILE="${FLEET_KEY_FILE:-$(fleet_key_of "$FLEET")}"

step_age_key
step_fleet_key
warn_existing_keys

cat <<EOF

Шаг 1 готов, имя установки: $FLEET.

  age-ключ:  $AGE_KEY_FILE
  SSH-ключ:  $FLEET_KEY_FILE

Публичные части, когда понадобятся:
  age-keygen -y $AGE_KEY_FILE
  cat $FLEET_KEY_FILE.pub

Дальше — частное дерево: wizard/private_tree.py (шаг 2).
EOF
