# Смена клиентского конфига без риска остаться без сети

`switch-config.sh` подменяет `/etc/sing-box/config.json` так, что потерять
доступ нельзя: перед установкой взводится таймер отката, и если связь после
рестарта не подтвердится — конфиг вернётся сам.

## Порядок

```bash
sudo client/switch-config.sh apply clients/<node>-<client>.json --hold 30
```

For a client whose management path is a different WireGuard endpoint, override
the address checked before confirmation:

```bash
sudo SWITCH_MGMT_GATEWAY=<mgmt-gateway-ip> \
  SWITCH_SKIP_EXIT_CHECK=1 \
  client/switch-config.sh apply clients/<node>-<client>.json --hold 60
```

`SWITCH_SKIP_EXIT_CHECK=1` is only for a partial-tunnel host: the switch still
requires a stable service and the configured management gateway, while system
traffic is intentionally allowed to keep its direct exit.

**Правило без исключений: применять только так.** Никаких `install ... config.json
&& systemctl restart` руками — именно так рабочая машина осталась без сети на 37
минут (408 рестартов подряд), и чинить её пришлось с другого компьютера.

Что произойдёт:

1. новый конфиг проверяется `sing-box check` — невалидный не установится вовсе;
2. текущий копируется в `/etc/sing-box/rollback/pre-<дата>.json` и в `last.json`;
3. **взводится автооткат** (`systemd-run --on-active=30`, дефолт 30 с) — до
   любого реального запуска нового конфига;
4. **smoke-run**: минимальный probe из allowlist `log`, `dns`, `ntp`,
   `certificate`, `outbounds`, `route` реально запускается на несколько
   секунд. Системные `endpoints`, `inbounds` и `experimental` в probe не попадают;
5. конфиг устанавливается, `sing-box-client.service` перезапускается;
6. проверяется связь: сервис активен и не крутится в crash loop, внешний IP
   **совпадает с IP ноды** из конфига, пинг mgmt-шлюза (`10.88.0.1` по
   умолчанию — см. `roles/wireguard/defaults/main.yml`);
7. что-то из этого не так → откат немедленно, не дожидаясь таймера.

Про пункт 6: просто «внешний IP получен» — негодная проверка. Когда sing-box
мёртв, TUN исчезает и трафик течёт напрямую: IP есть, VPN нет. Поэтому сверяем
IP с адресом ноды (`SWITCH_EXPECT_IP="203.0.113.10"` переопределяет, если выход
заведомо другой).

Дальше проверяешь, что всё нужное живо (браузер, почта, что угодно), и:

```bash
sudo /usr/local/sbin/vpn-switch confirm     # оставить новый конфиг
```

Не подтвердил в течение `--hold` секунд — старый конфиг вернётся автоматически.

## Если что-то пошло не так

Команды локальные, работают даже при полностью мёртвом VPN:

```bash
sudo /usr/local/sbin/vpn-switch rollback    # вернуть предыдущий конфиг
sudo /usr/local/sbin/vpn-switch status      # что стоит, взведён ли таймер, есть ли связь
```

Ручной вариант, если скрипт недоступен:

```bash
sudo install -o root -g root -m0600 \
  /etc/sing-box/rollback/last.json /etc/sing-box/config.json
sudo systemctl restart sing-box-client.service
```

Совсем аварийный выход — выключить туннель целиком, машина уйдёт в интернет
напрямую:

```bash
sudo systemctl stop sing-box-client.service
```

## Проверка после переключения

```bash
curl -s https://api.ipify.org            # должен быть IP ноды
ping -c3 10.88.0.1                       # mgmt-шлюз на ноде
ping -c3 10.88.0.2                       # первое устройство в mgmt-сети
```

Торренты, наоборот, обязаны идти **мимо** тоннеля (`singbox_client_direct_processes`):

```bash
# при запущенной закачке — соединения qbittorrent должны идти с домашнего IP
sudo ss -tnp | grep qbittorrent | head
```

Бэкапы в `/etc/sing-box/rollback/` не удалять, пока новый конфиг не обкатан.
