#!/usr/bin/env bash
# Подмена /etc/sing-box/config.json со страховкой от потери связи.
#
#   switch-config.sh apply <новый.json> [--hold 30]    подменить и взвести автооткат
#   switch-config.sh confirm                           связь есть → снять автооткат
#   switch-config.sh rollback                          вернуть предыдущий конфиг
#   switch-config.sh status                            что стоит, взведён ли таймер
#
# Логика apply: валидация → бэкап → ВЗВЕСТИ таймер отката → smoke-run →
# установка → рестарт → проверка связи. Таймер взводится до любого
# реального запуска нового конфига: если машина зависнет или связь пропадёт,
# откат всё равно случится сам через --hold секунд. Проверка связи не прошла —
# откат немедленный.
#
# ДВА СЛУЧАЯ, при которых клиент может оставить машину без сети:
#   1. `sing-box check` НЕ ловит ошибки старта сервиса (например
#      "detour to an empty direct outbound makes no sense") — конфиг валиден,
#      но демон падает в рантайме и уходит в crash loop. Поэтому здесь есть
#      smoke_run(): конфиг без inbounds реально запускается на несколько секунд.
#   2. Проверка «есть внешний IP» проходит и при МЁРТВОМ sing-box: TUN исчез,
#      трафик течёт напрямую. Поэтому connectivity_check() требует активный
#      сервис без рестартов И совпадение внешнего IP с адресом ноды из конфига.
set -euo pipefail

CONFIG=/etc/sing-box/config.json
ROLLBACK_DIR=/etc/sing-box/rollback
LAST=$ROLLBACK_DIR/last.json
SINGBOX=/usr/local/bin/sing-box
UNIT=sing-box-autorollback
SELF_INSTALLED=/usr/local/sbin/vpn-switch
SERVICE=sing-box-client.service
MGMT_GATEWAY="${SWITCH_MGMT_GATEWAY:-10.88.0.1}"

die() { echo "ERROR: $*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "нужен root: sudo $0 $*"; }

# Адреса нод из конфига: при full-tunnel внешний IP обязан быть одним из них.
# Переопределяется через SWITCH_EXPECT_IP="203.0.113.10 203.0.113.11" (цепочки, split-exit).
expected_exit_ips() {
  local cfg="${1:-$CONFIG}"
  if [ -n "${SWITCH_EXPECT_IP:-}" ]; then
    # shellcheck disable=SC2086  # разбиение по пробелам здесь и нужно
    printf '%s\n' $SWITCH_EXPECT_IP
    return 0
  fi
  jq -r '[.outbounds[]? | select(.type=="vless" or .type=="hysteria2" or
                                 .type=="trojan" or .type=="shadowsocks")
          | .server] | unique[]' "$cfg" 2>/dev/null || true
}

# Живой ли сервис: активен и не крутится в crash loop (счётчик рестартов замер).
service_healthy() {
  local n1 n2
  systemctl is-active --quiet "$SERVICE" || return 1
  n1="$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || echo 0)"
  sleep 4
  n2="$(systemctl show -p NRestarts --value "$SERVICE" 2>/dev/null || echo 0)"
  systemctl is-active --quiet "$SERVICE" || return 1
  [ "$n1" = "$n2" ]
}

connectivity_check() {
  local cfg="${1:-$CONFIG}" external ping_rc=1 svc_rc=0 expect matched=1
  service_healthy || svc_rc=1
  if [ "${SWITCH_SKIP_EXIT_CHECK:-0}" = 1 ]; then
    external="SKIPPED"
    matched=0
  else
    external="$(curl -s --max-time 20 https://api.ipify.org || true)"
  fi
  ping -c2 -W3 "$MGMT_GATEWAY" >/dev/null 2>&1 && ping_rc=0 || ping_rc=1
  expect="$(expected_exit_ips "$cfg" | tr '\n' ' ')"
  if [ "${SWITCH_SKIP_EXIT_CHECK:-0}" = 1 ]; then
    matched=0
  elif [ -n "$external" ] && [ -n "${expect// /}" ]; then
    case " $expect " in *" $external "*) matched=0 ;; *) matched=1 ;; esac
  elif [ -n "$external" ]; then
    matched=0   # некому сравнивать (нет прокси-outbound) — не заваливаем
  fi

  echo "  сервис:       $(systemctl is-active "$SERVICE")$([ $svc_rc -eq 0 ] || echo ' (мёртв или в crash loop)')"
  echo "  внешний IP:   ${external:-НЕТ ОТВЕТА}$([ $matched -eq 0 ] || echo " (ожидался один из: ${expect:-?} — трафик идёт МИМО тоннеля)")"
  echo "  mgmt-шлюз:    $([ $ping_rc -eq 0 ] && echo "$MGMT_GATEWAY отвечает" || echo "молчит")"

  [ $svc_rc -eq 0 ] || return 1
  [ "${SWITCH_SKIP_EXIT_CHECK:-0}" = 1 ] || [ -n "$external" ] || return 1
  [ $matched -eq 0 ] || return 1
  return 0
}

# `sing-box check` валидирует схему, но НЕ старт сервиса. Запускаем минимальный
# probe из безопасного allowlist. В него не попадают inbounds, endpoints,
# experimental и другие верхнеуровневые поля. Успех = процесс дожил до
# таймаута (rc=124).
smoke_run() {
  local cfg="$1" tmp out rc=0
  command -v jq >/dev/null 2>&1 || {
    echo "нужен jq: sudo apt install -y jq" >&2
    return 1
  }
  tmp="$(mktemp -d)"
  jq '{log, dns, ntp, certificate, outbounds, route}
      | with_entries(select(.value != null))' \
    "$cfg" > "$tmp/probe.json" || {
      rm -rf "$tmp"
      echo "не разобрал JSON $cfg" >&2
      return 1
    }
  set +e
  out="$(timeout 6 "$SINGBOX" run -c "$tmp/probe.json" -D "$tmp" 2>&1)"
  rc=$?
  set -e
  rm -rf "$tmp"
  if [ "$rc" -ne 124 ]; then
    echo "$out" >&2
    echo "конфиг проходит check, но НЕ стартует (rc=$rc)" >&2
    return 1
  fi
}

disarm() {
  systemctl stop "$UNIT.timer" 2>/dev/null || true
  systemctl reset-failed "$UNIT.timer" "$UNIT.service" 2>/dev/null || true
}

do_rollback() {
  [ -f "$LAST" ] || die "нет бэкапа $LAST"
  "$SINGBOX" check -c "$LAST" || die "бэкап не проходит валидацию — чинить руками"
  install -o root -g root -m0600 "$LAST" "$CONFIG"
  systemctl reset-failed "$SERVICE" 2>/dev/null || true
  systemctl restart "$SERVICE"
  disarm
  echo ">> откат выполнен, конфиг восстановлен из $LAST"
  systemctl --no-pager --full status "$SERVICE" | head -4
  # Откат тоже может не подняться (например нода умерла) — говорим об этом вслух.
  service_healthy || echo "ВНИМАНИЕ: после отката сервис не здоров — смотри journalctl -u $SERVICE"
}

case "${1:-status}" in
  apply)
    need_root "$@"
    NEW="${2:?укажи путь к новому конфигу}"
    # ЖЁСТКОЕ ПРАВИЛО: конфиг применяется только с коротким автооткатом.
    # 30s по умолчанию — не подтвердил вручную, значит вернулись назад.
    HOLD=30
    [ "${3:-}" = "--hold" ] && HOLD="${4:?число секунд}"
    [ -f "$NEW" ] || die "нет файла $NEW"
    [ -x "$SINGBOX" ] || die "$SINGBOX не установлен"

    echo ">> валидация нового конфига"
    "$SINGBOX" check -c "$NEW" || die "новый конфиг невалиден — ничего не менял"

    echo ">> бэкап текущего"
    install -d -o root -g root -m0700 "$ROLLBACK_DIR"
    stamp="$(date +%Y%m%d-%H%M%S)"
    install -o root -g root -m0600 "$CONFIG" "$ROLLBACK_DIR/pre-$stamp.json"
    install -o root -g root -m0600 "$CONFIG" "$LAST"
    echo "   $ROLLBACK_DIR/pre-$stamp.json"

    # Копия скрипта вне $HOME, чтобы таймер не зависел от домашнего каталога.
    if [ "$(readlink -f "$0")" != "$(readlink -f "$SELF_INSTALLED")" ]; then
      install -o root -g root -m0755 "$0" "$SELF_INSTALLED"
    fi

    echo ">> взвожу автооткат через ${HOLD}s"
    disarm
    systemd-run --unit="$UNIT" --on-active="${HOLD}" --timer-property=AccuracySec=1s \
      "$SELF_INSTALLED" rollback >/dev/null
    echo "   снять: sudo $SELF_INSTALLED confirm"

    echo ">> smoke-run (реальный старт без системных endpoints и TUN)"
    if ! smoke_run "$NEW"; then
      disarm
      die "smoke-run не прошёл — конфиг не менял"
    fi
    echo "   стартует"

    echo ">> установка нового конфига"
    install -o root -g root -m0600 "$NEW" "$CONFIG"
    systemctl restart "$SERVICE"
    sleep 6

    echo ">> проверка связи"
    if connectivity_check "$CONFIG"; then
      echo
      echo "Связь есть. Проверь, что всё нужное работает, и подтверди:"
      echo "    sudo $SELF_INSTALLED confirm"
      echo "Иначе через ${HOLD}s конфиг вернётся сам."
    else
      echo
      echo "Связи нет — откатываюсь немедленно."
      do_rollback
      exit 1
    fi
    ;;

  confirm)
    need_root "$@"
    disarm
    echo ">> автооткат снят, новый конфиг остаётся"
    ;;

  rollback)
    need_root "$@"
    do_rollback
    ;;

  status)
    echo "конфиг:    $CONFIG"
    [ -f "$CONFIG" ] && echo "изменён:   $(stat -c %y "$CONFIG")"
    echo "бэкап:     $([ -f "$LAST" ] && stat -c %y "$LAST" || echo "нет")"
    echo -n "автооткат: "
    systemctl is-active --quiet "$UNIT.timer" && \
      echo "ВЗВЕДЁН, сработает $(systemctl show -p NextElapseUSecRealtime --value "$UNIT.timer")" || \
      echo "снят"
    echo "сервис:    $(systemctl is-active "$SERVICE")"
    connectivity_check || true
    ;;

  *)
    sed -n '2,9p' "$0"
    ;;
esac
