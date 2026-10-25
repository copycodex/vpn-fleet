#!/usr/bin/env bash
# Установка sing-box-клиента (TUN full-tunnel) с автостартом на ноуте.
# Бинарь sing-box и /etc/sing-box/config.json должны быть уже на месте
# (config = `make export NODE=<node>` → clients/<node>-<name>.json → /etc/sing-box/config.json).
set -euo pipefail

UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sing-box-client.service"
UNIT_DST="/etc/systemd/system/sing-box-client.service"

[ -x /usr/local/bin/sing-box ] || { echo "ERROR: /usr/local/bin/sing-box не установлен"; exit 1; }
[ -f /etc/sing-box/config.json ] || { echo "ERROR: /etc/sing-box/config.json отсутствует (сделай export + install конфига)"; exit 1; }

echo ">> validate config"
sudo /usr/local/bin/sing-box check -c /etc/sing-box/config.json

echo ">> проверка юнита до установки"
sudo systemd-analyze verify "$UNIT_SRC"

echo ">> install unit -> $UNIT_DST"
# Обратный билет: этот ноут ходит в сеть через сам sing-box, поэтому юнит,
# который не стартует, = ноут без интернета. Держим предыдущий рядом.
if [ -f "$UNIT_DST" ]; then
  # Рядом с конфигом, а не в каталоге юнитов: systemd там ничего лишнего не ждёт.
  sudo install -d -o root -g root -m0700 /etc/sing-box/rollback
  BACKUP="/etc/sing-box/rollback/sing-box-client.service.$(date +%Y%m%d-%H%M%S)"
  sudo install -m0644 "$UNIT_DST" "$BACKUP"
  echo "   бэкап: $BACKUP"
fi
sudo install -m0644 "$UNIT_SRC" "$UNIT_DST"
sudo systemctl daemon-reload
sudo systemctl enable sing-box-client.service
# enable --now не перезапускает уже работающий сервис — новые настройки юнита
# без этого не применились бы.
sudo systemctl restart sing-box-client.service
sleep 5

if ! systemctl is-active --quiet sing-box-client.service; then
  echo "!! сервис не поднялся — возвращаю прежний юнит" >&2
  if [ -n "${BACKUP:-}" ]; then
    sudo install -m0644 "$BACKUP" "$UNIT_DST"
    sudo systemctl daemon-reload
    sudo systemctl restart sing-box-client.service
  fi
  journalctl -u sing-box-client.service -n 20 --no-pager >&2
  exit 1
fi

echo ">> status"
systemctl --no-pager --full status sing-box-client.service | head -12
echo ">> external IP (должен быть IP ноды):"
curl -s https://api.ipify.org && echo
