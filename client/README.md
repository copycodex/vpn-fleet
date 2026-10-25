# client/ — клиентский tooling (tracked)

Установка sing-box-клиента (TUN full-tunnel) с автостартом на ноуте/телефоне.

> Не путать с `clients/` (в `.gitignore`) — там **сгенерённые** per-client конфиги с UUID/секретами. Здесь — только secret-free tooling (юнит + install-скрипт).

Windows-клиент живёт в `client/windows/` (скрипты автозапуска, вотчдога и
ярлыков ON/OFF). Бандл собирается `make win-bundle`, инструкция пользователю
едет внутри бандла (`README.txt`).

## Шаги

1. Поставить бинарь sing-box (pin версии — `roles/singbox/defaults/main.yml`):

   ```
   sudo install -m0755 sing-box /usr/local/bin/sing-box
   ```

2. Сгенерить и положить конфиг клиента. Первый раз — как ниже; **на живой
   машине конфиг меняется только через `switch-config.sh apply --hold 30`**
   (см. [`SWITCH.md`](SWITCH.md)), иначе рискуешь остаться без сети:

   ```
   make export NODE=<node>
   sudo install -m0600 clients/<node>-<client>.json /etc/sing-box/config.json
   ```

3. Автостарт:

   ```
   ./client/install.sh
   ```

## Проверка

- `curl -s https://api.ipify.org` → IP ноды.
- `resolvectl query example.com` → DNS не течёт мимо туннеля.
- `sudo ss -tunp | grep <node-ip>` → TCP/443 = Reality, UDP/443 = Hysteria2.
- Приватные подсети (LAN и управляющая сеть) идут `direct` (split-tunnel).
- Торренты идут **мимо** туннеля: `cp /usr/bin/curl /tmp/qbittorrent &&
  /tmp/qbittorrent -s https://api.ipify.org` → домашний IP, не IP ноды. Правило
  матчится по имени процесса, а для этого юниту нужны `CAP_DAC_READ_SEARCH` и
  `CAP_SYS_PTRACE` (см. комментарий в `sing-box-client.service`) — без них
  правило молча не срабатывает.

Для периодической независимой проверки Reality, Hysteria2 и SSH см.
[`probe/README.md`](probe/README.md).

## Killswitch

`strict_route: true` в TUN не даёт трафику течь мимо туннеля, пока sing-box жив; `Restart=always` поднимает его обратно при падении. Жёсткий killswitch на уровне firewall (нет sing-box → нет интернета) — отдельным шагом (nftables), пока не настроен.
