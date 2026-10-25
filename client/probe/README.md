# Transport probe

Проверяет каждый сгенерированный транспорт через отдельный локальный SOCKS и
сравнивает внешний адрес с IP ноды. Дополнительно выполняет SSH-проверку с
`BatchMode=yes`. UUID и другие credentials не пишутся в лог.

```bash
make export NODE=<node>
./client/probe/install.sh
```

Экспорт создаёт приватные конфиги в `clients/probe/`; installer копирует их в
`~/.config/vpn-fleet/probe/`. Таймер запускается каждые 20 минут. Результаты:
`~/.local/state/vpn-fleet/probe.log`; ошибки также отправляются через
`notify-send`, если доступна пользовательская D-Bus session.

SSH target и порт берутся из inventory. Нужный private key должен быть доступен
обычному `ssh` — через ssh-agent либо стандартный `~/.ssh/config`.

Ручной запуск:

```bash
systemctl --user start vpn-probe.service
journalctl --user -u vpn-probe.service
```
