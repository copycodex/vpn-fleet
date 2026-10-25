#!/usr/bin/env bash
# Общее хозяйство шагов визарда: печать, вопросы, проверка каталога.
# Файл только подключается (source), сам ничего не делает.
#
# Каждый шаг визарда — отдельный исполняемый файл рядом. Порядок шагов знает
# wizard.py, а не имена файлов: номер в имени живёт до первого слияния или
# разделения шага, после чего врёт.

WIZARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$WIZARD_DIR/.." && pwd)"

AGE_DIR="${AGE_DIR:-$HOME/.config/sops/age}"
SSH_DIR="${SSH_DIR:-$HOME/.ssh}"
VENV_DIR="${VENV_DIR:-$HOME/.venv/vpn-fleet}"
SOPS_BIN_DIR="${SOPS_BIN_DIR:-/usr/local/bin}"

# Префикс печати задаёт сам шаг: WIZARD_STEP="deps" и так далее.
say()  { echo "${WIZARD_STEP:-wizard}: $*"; }
warn() { echo "WARN: $*" >&2; }
fail() { echo "ERROR: $*" >&2; exit 1; }


interactive() { [ -t 0 ]; }

confirm_install() {
  if [ "${ASSUME_YES:-0}" -eq 1 ]; then
    return 0
  fi
  interactive || fail "установка требует TTY или явного --yes"
  local answer
  read -r -p "Установить перечисленные зависимости? [y/N] " answer ||
    fail "подтверждение установки не получено"
  case "$answer" in
    y | Y | yes | YES) return 0 ;;
    *) fail "установка отменена" ;;
  esac
}

# printf %-22s считает БАЙТЫ, а кириллица в UTF-8 занимает по два — колонка
# разъезжается. Отступ считаем в символах: ${#...} под C.UTF-8 их и считает.

row() {
  local pad=$((22 - ${#1}))
  [ "$pad" -ge 1 ] || pad=1
  printf "  %s%*s%s\n" "$1" "$pad" "" "$2"
}


CHOICE=""
choose() {
  local prompt="$1"; shift
  local options=("$@") index answer
  echo
  echo "$prompt"
  for index in "${!options[@]}"; do
    echo "  $((index + 1))) ${options[$index]}"
  done
  echo "  n) завести новый"
  while :; do
    read -r -p "что берём [1]: " answer
    answer="${answer:-1}"
    if [ "$answer" = "n" ]; then CHOICE=""; return 0; fi
    if [[ "$answer" =~ ^[0-9]+$ ]] && [ "$answer" -ge 1 ] &&
       [ "$answer" -le "${#options[@]}" ]; then
      # Read by keys.sh after choose returns; ShellCheck checks this file alone.
      # shellcheck disable=SC2034
      CHOICE="${options[$((answer - 1))]}"
      return 0
    fi
    echo "  введи номер из списка или n"
  done
}


require_repo() {
  local file
  for file in Makefile ansible.cfg requirements-ci.txt requirements.yml; do
    [ -f "$ROOT/$file" ] || fail "в $ROOT нет $file — это не корень vpn-fleet"
  done
}


# sudo нужен ровно двум шагам: apt и установке бинаря в системный каталог.
# Если всё уже стоит, sudo не вызывается вовсе.
sudo_prefix() {
  if [ "$(id -u)" -eq 0 ]; then
    return 0
  fi
  command -v sudo >/dev/null 2>&1 || fail "нужен root или sudo: $1"
  echo sudo
}
