#!/usr/bin/env bash
# Шаг 0 визарда: зависимости control host.
#
# Сначала смотрит и называет, потом спрашивает, и только потом ставит. Молча не
# делает ничего: человек видит весь список до того, как что-то поставится.
#
# Usage: wizard/deps.sh            спросит перед установкой
#        wizard/deps.sh --check    только проверка, ничего не ставит
#        wizard/deps.sh --yes      ставить без вопросов
#
# Переопределяется окружением: VENV_DIR, SOPS_BIN_DIR.
set -euo pipefail
# C.UTF-8, а не C: ansible-galaxy отказывается работать в неюникодной локали
# («Ansible requires the locale encoding to be UTF-8»).
export LC_ALL=C.UTF-8

WIZARD_STEP="deps"
# shellcheck source=wizard/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

# Версия и контрольные суммы — из релиза getsops/sops. Бинарь ставится только
# при полном совпадении sha256: скачанное из сети не устанавливается на веру.
SOPS_VERSION="3.9.1"
SOPS_SHA256_amd64="cd795109851c3a483bbaa66d15d19ddb2227ac0352b39e25d96b67d51edb6225"
SOPS_SHA256_arm64="bc946fef11dbe199587adac567037b69374c4202f928ca138443539efc85b357"

APT_PACKAGES=(git python3 python3-venv python3-pip openssh-client age curl)
COLLECTIONS=(ansible.posix community.general community.sops)

CHECK_ONLY=0
ASSUME_YES=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --yes | -y) ASSUME_YES=1 ;;
    -h | --help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "ERROR: неизвестный аргумент: $1" >&2; exit 1 ;;
  esac
  shift
done

# Что именно придётся доставить: заполняется аудитом, читается шагами установки.
NEED_PACKAGES=()
NEED_SOPS=0
NEED_VENV=0
NEED_DEPS=0
NEED_COLLECTIONS=0

missing_packages() {
  local package
  for package in "${APT_PACKAGES[@]}"; do
    dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null |
      grep -qx installed || echo "$package"
  done
}

# Состояние sops: нет / ровно пин / новее пина / старее пина. Старее — это не
# придирка: файлы парка писаны 3.9.1, и читать их обязан тот, кто умеет формат.
sops_state() {
  command -v sops >/dev/null 2>&1 || { echo "missing"; return; }
  local have
  have="$(sops --version 2>/dev/null | head -1 | awk '{print $2}')"
  [ -n "$have" ] || { echo "unknown"; return; }
  if [ "$have" = "$SOPS_VERSION" ]; then
    echo "ok $have"
  elif [ "$(printf '%s\n%s\n' "$SOPS_VERSION" "$have" | sort -V | head -1)" = "$SOPS_VERSION" ]; then
    echo "newer $have"
  else
    echo "old $have"
  fi
}

venv_ready()  { [ -x "$VENV_DIR/bin/python" ]; }
deps_ready()  { venv_ready && "$VENV_DIR/bin/python" -c 'import pexpect, yaml' 2>/dev/null; }

collections_missing() {
  venv_ready || return 0
  local listed name
  listed="$("$VENV_DIR/bin/ansible-galaxy" collection list 2>/dev/null || true)"
  for name in "${COLLECTIONS[@]}"; do
    grep -q "^${name} " <<< "$listed" || echo "$name"
  done
}

# Аудит ничего не меняет: он только смотрит и называет. Решение — отдельным шагом.
audit() {
  local state missing collections
  echo
  echo "Зависимости шага 0:"

  mapfile -t NEED_PACKAGES < <(missing_packages)
  if [ "${#NEED_PACKAGES[@]}" -eq 0 ]; then
    row "системные пакеты" "есть"
  else
    row "системные пакеты" "нет: ${NEED_PACKAGES[*]}"
  fi

  state="$(sops_state)"
  case "$state" in
    missing) NEED_SOPS=1; row "sops" "нет — поставлю $SOPS_VERSION по sha256" ;;
    ok*)     row "sops" "есть, ${state#ok }" ;;
    newer*)  row "sops" "есть, ${state#newer } — новее пина $SOPS_VERSION, годится" ;;
    old*)    NEED_SOPS=1
             row "sops" "есть, ${state#old } — старее пина $SOPS_VERSION, заменю" ;;
    *)       NEED_SOPS=1; row "sops" "версия не читается — поставлю $SOPS_VERSION" ;;
  esac

  if venv_ready; then
    row "venv" "есть: $VENV_DIR"
  else
    NEED_VENV=1
    row "venv" "нет — создам: $VENV_DIR"
  fi

  if deps_ready; then
    row "python-зависимости" "есть: pexpect, PyYAML, ansible"
  else
    NEED_DEPS=1
    row "python-зависимости" "нет — поставлю из requirements-ci.txt"
  fi

  if ! venv_ready; then
    NEED_COLLECTIONS=1
    row "коллекции ansible" "проверю после venv"
  else
    mapfile -t collections < <(collections_missing)
    if [ "${#collections[@]}" -eq 0 ]; then
      row "коллекции ansible" "есть: ${COLLECTIONS[*]}"
    else
      NEED_COLLECTIONS=1
      row "коллекции ansible" "нет: ${collections[*]}"
    fi
  fi

  if command -v docker >/dev/null 2>&1; then
    row "docker" "есть — нужен только локальному стенду"
  else
    row "docker" "нет — необязателен, нужен только локальному стенду"
  fi
  echo
}

step_packages() {
  local missing=("${NEED_PACKAGES[@]}")
  [ "${#missing[@]}" -gt 0 ] || return 0

  command -v apt-get >/dev/null 2>&1 ||
    fail "скрипт ставит пакеты через apt-get; на этой системе поставь руками: ${missing[*]}"

  local sudo
  sudo="$(sudo_prefix "установка пакетов ${missing[*]}")"
  say "ставлю пакеты: ${missing[*]}"
  $sudo apt-get update
  $sudo apt-get install -y "${missing[@]}"
}

step_sops() {
  [ "$NEED_SOPS" -eq 1 ] || return 0

  local arch asset expected
  case "$(uname -m)" in
    x86_64)          arch=amd64; expected="$SOPS_SHA256_amd64" ;;
    aarch64 | arm64) arch=arm64; expected="$SOPS_SHA256_arm64" ;;
    *) fail "архитектура $(uname -m) не поддержана: поставь sops $SOPS_VERSION вручную" ;;
  esac
  asset="sops-v${SOPS_VERSION}.linux.${arch}"

  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN

  say "качаю sops $SOPS_VERSION ($arch)"
  curl -fsSL -o "$tmp/sops" \
    "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/${asset}"

  local actual
  actual="$(sha256sum "$tmp/sops" | cut -d' ' -f1)"
  [ "$actual" = "$expected" ] ||
    fail "sha256 скачанного sops не совпал с релизным — бинарь не установлен"

  local sudo
  sudo="$(sudo_prefix "установка sops в $SOPS_BIN_DIR")"
  $sudo install -m 0755 "$tmp/sops" "$SOPS_BIN_DIR/sops"
  say "sops установлен: $SOPS_BIN_DIR/sops"
}

step_venv() {
  if [ "$NEED_VENV" -eq 1 ]; then
    say "создаю venv: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
  fi

  if [ "$NEED_DEPS" -eq 1 ] || [ "$NEED_VENV" -eq 1 ]; then
    say "ставлю python-зависимости из requirements-ci.txt"
    "$VENV_DIR/bin/pip" install --quiet --disable-pip-version-check \
      -r "$ROOT/requirements-ci.txt"
  fi

  if [ "$NEED_COLLECTIONS" -eq 1 ] || [ "$NEED_VENV" -eq 1 ]; then
    say "ставлю коллекции ansible из requirements.yml"
    "$VENV_DIR/bin/ansible-galaxy" collection install -r "$ROOT/requirements.yml"
  fi
}

verify() {
  local sops_version ansible_version
  sops_version="$(sops --version 2>/dev/null | head -1)"
  ansible_version="$("$VENV_DIR/bin/ansible" --version | head -1)"
  "$VENV_DIR/bin/python" -c 'import pexpect, yaml' ||
    fail "в venv не читаются pexpect и PyYAML"
  say "проверка: $sops_version, $ansible_version, pexpect и PyYAML на месте"
}


require_repo
audit

if [ "$CHECK_ONLY" -eq 1 ]; then
  say "только проверка (--check): ничего не ставил"
  exit 0
fi

confirm_install
step_packages
step_sops
step_venv
verify

echo
say "шаг 0 готов. Дальше — ключи установки: wizard/keys.sh"
