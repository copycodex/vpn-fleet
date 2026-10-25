#!/usr/bin/env python3
"""Завести сервер или дозаполнить уже заведённый.

Шаг 0.5 гайда одной командой: файл инвентаря, запись в карте первого входа и
строки секретов с префиксом ноды. Всё это можно написать руками — скрипт лишь
набирает те же файлы быстрее и без опечаток.

Три правила, из которых он состоит:

1. Ничего не перезаписывает. Существующий сервер дозаполняется: скрипт
   показывает, чего не хватает, и пишет только недостающее.
2. Своего признака «касание пройдено» не выдумывает. Состояние двери
   спрашивается у владельца правила — scripts/first-contact.py.
3. Пароль не появляется ни в argv, ни в выводе, ни в открытом файле: он
   читается скрытым вводом и уходит внутрь sops-сессии.

Сети не касается вовсе: первое касание запускается отдельной командой, которую
скрипт печатает в конце.
"""

from __future__ import annotations

import getpass
import base64
import hashlib
import ipaddress
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover - названная причина вместо traceback
    yaml = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    ROOT,
    NAME_RE,
    ask,
    ask_choice,
    ask_yes,
    menu,
    warn,
    fail,
    first_contact_module,
    private_dir,
    resolve_fleet,
    say,
    set_step,
    use_fleet_age_key,
)
from hoster import door_of, load as load_hosters, print_route, route_of, select as select_hoster  # noqa: E402
from deletion_state import marker_path  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts"))

USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOSTNAME_RE = re.compile(rf"^(?=.{{1,253}}$){LABEL}(?:\.{LABEL})*$")
# Документационные диапазоны (RFC 5737, 3849) в новых Python числятся private,
# а сказать про них надо иначе: это не «чужая локалка», это адрес из примера.
DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)
SECRET_DEFAULTS = {
    "clients": "[]",
    "console_password": '""',
}
REALITY_NAMES = ("reality_private_key", "reality_public_key", "reality_short_id")
HY2_NAMES = (
    "hy2_password", "hy2_certificate", "hy2_key",
    "hy2_certificate_public_key_sha256",
)


def suggest_name(existing: list[str], fleet: str, hoster: str) -> str:
    """Конвенция имени: <установка>-<хостер>-NN, первый свободный номер."""
    base = f"{fleet}-{hoster}"
    for index in range(1, 100):
        candidate = f"{base}-{index:02d}"
        if candidate not in existing:
            return candidate
    return base


def check_address(value: str) -> tuple[bool, str | None]:
    """Форма адреса и его публичность. Вторым возвращается повод переспросить."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        # Четыре числа через точку, где какое-то больше 255, — формально годное
        # DNS-имя, но почти наверняка опечатка в адресе: молча принимать нельзя.
        if re.fullmatch(r"[0-9.]+", value):
            return False, "похоже на IPv4 с ошибкой"
        if HOSTNAME_RE.match(value):
            return True, None
        return False, "не похоже ни на IP, ни на имя хоста"
    if address.is_loopback:
        return True, "это loopback — нода по нему не откроется ниоткуда"
    if any(address in network for network in DOCUMENTATION_NETWORKS):
        return True, "это адрес из документационного диапазона, а не настоящий"
    if address.is_private or address.is_link_local:
        return True, "это адрес частной сети, а не публичный"
    if not address.is_global:
        return True, "это служебный или документационный диапазон, а не публичный адрес"
    return True, None


def address_taken_by(private: Path, host: str) -> str | None:
    """Тот же адрес у другой ноды — почти всегда опечатка или переустановка."""
    nodes = private / "inventories" / "nodes"
    for path in sorted(nodes.glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            hosts = data["all"]["hosts"]
        except (OSError, KeyError, TypeError, yaml.YAMLError):
            continue
        for name, values in (hosts or {}).items():
            if isinstance(values, dict) and values.get("ansible_host") == host:
                return str(name)
    return None


def probe_ssh(host: str, port: int) -> bool:
    """Отвечает ли вообще что-нибудь по SSH. Ничего не принимает и не пишет."""
    result = subprocess.run(
        ["ssh-keyscan", "-T", "5", "-p", str(port), host],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


STEADY_USER_DEFAULT = "devops"
STEADY_PORT_DEFAULT = 2424

# Порты, которые нода занимает сама. Протокол важен: SSH — TCP, поэтому
# столкнуться он может только с Reality; Hysteria2 и WireGuard живут в UDP и
# сидят на тех же числах совершенно законно.
TRANSPORT_PORTS = (
    ("Reality", "tcp", 443),
    ("Hysteria2", "udp", 443),
    ("WireGuard", "udp", 51821),
)


def check_steady_port(port: int, door_port: int) -> tuple[bool, str | None]:
    """Годится ли порт под steady-state SSH. Второе — повод переспросить."""
    if not 1 <= port <= 65535:
        return False, "порт вне диапазона 1..65535"
    for name, proto, number in TRANSPORT_PORTS:
        if number == port and proto == "tcp":
            return False, f"{port}/tcp занимает {name} — sshd туда не встанет"
    if port == 22:
        return True, "это 22: переезд порта тогда ничего не меняет"
    if port == door_port:
        return True, f"это порт первого входа ({door_port}), переезда не будет"
    if port < 1024:
        return True, "порт из привилегированного диапазона — так обычно не делают"
    for name, proto, number in TRANSPORT_PORTS:
        if number == port:
            return True, f"совпадает с {name} ({number}/{proto}); SSH в TCP, они разойдутся"
    return True, None


def free_steady_port(door_port: int) -> int:
    """Случайный порт из верхнего диапазона, не спорящий ни с чем объявленным."""
    for _ in range(50):
        candidate = secrets.randbelow(40000) + 20000
        ok, note = check_steady_port(candidate, door_port)
        if ok and note is None:
            return candidate
    return STEADY_PORT_DEFAULT


def ask_steady_access(door_port: int) -> tuple[str, int]:
    """Шаг 6: кем и по какому порту нода открывается ПОСЛЕ бутстрапа."""
    while True:
        user = ask("пользователь после бутстрапа", STEADY_USER_DEFAULT)
        if USER_RE.match(user):
            break
        print("  имя пользователя — буквы, цифры, точка, дефис и подчёркивание")

    default = STEADY_PORT_DEFAULT
    ok, note = check_steady_port(default, door_port)
    if not ok or note is not None:
        default = free_steady_port(door_port)
        say(f"{STEADY_PORT_DEFAULT} не годится ({note}) — предлагаю {default}")

    while True:
        answer = ask("порт, на который переедет SSH", str(default))
        if not answer.isdigit() or not 1 <= int(answer) <= 65535:
            print("  порт — число 1..65535")
            continue
        port = int(answer)
        ok, note = check_steady_port(port, door_port)
        if not ok:
            print(f"  {note}")
            continue
        if note and not ask_yes(f"  {note}. Всё равно берём?", default=False):
            continue
        return user, port


# На части сетей фильтр режет ClientHello с SNI популярных доменов.
# Для Reality нужен dest с TLS 1.3 и h2; доступность SNI проверяет preflight.
REALITY_DEST_DEFAULT = "www.samsung.com"
HY2_PORT_DEFAULT = 443
WG_PORT_DEFAULT = 51821


def probe_tls13(host: str, port: int = 443) -> str | None:
    """Отвечает ли dest по TLS 1.3. Только смотрим: ничего не отправляем внутрь.

    Reality маскируется под этот сайт, и если он не умеет TLS 1.3, маскировка
    не состоится. Проверка необязательная: openssl может отсутствовать.
    """
    if not shutil.which("openssl"):
        return None
    result = subprocess.run(
        ["openssl", "s_client", "-connect", f"{host}:{port}", "-servername", host,
         "-tls1_3", "-brief"],
        input="",
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    output = result.stdout + result.stderr
    if "TLSv1.3" not in output:
        return None
    for line in output.splitlines():
        if "Negotiated TLS1.3 group" in line or line.strip().startswith("Ciphersuite"):
            return line.strip()
    return "TLSv1.3"


def ask_transports(steady_port: int) -> dict:
    """Шаг 7: чем нода светит наружу и на каких портах.

    Единственный настоящий конфликт здесь — TCP: Reality и SSH в одном стеке.
    Hysteria2 и WireGuard живут в UDP и с SSH не спорят, зато спорят между собой.
    """
    while True:
        dest = ask("Reality: под какой сайт маскируемся (dest)", REALITY_DEST_DEFAULT)
        if HOSTNAME_RE.match(dest):
            break
        print("  это должно быть доменное имя, например www.samsung.com")
    sni = ask("Reality: SNI (обычно тот же сайт)", dest)

    if ask_yes(f"проверить, что {dest} отвечает по TLS 1.3?"):
        detail = probe_tls13(dest)
        if detail:
            say(f"{dest}: TLS 1.3 есть — {detail}")
        else:
            warn(
                f"{dest} не ответил по TLS 1.3 (или нет openssl). Reality маскируется "
                "под этот сайт: возьми тот, что заведомо умеет TLS 1.3"
            )

    while True:
        answer = ask("Reality: порт (TCP)", "443")
        if not answer.isdigit() or not 1 <= int(answer) <= 65535:
            print("  порт — число 1..65535")
            continue
        reality_port = int(answer)
        if reality_port == steady_port:
            print(f"  {reality_port}/tcp уже занят SSH этой ноды — выбери другой")
            continue
        break

    hy2_enabled = ask_yes("Hysteria2 включаем?")
    hy2_port = HY2_PORT_DEFAULT
    if hy2_enabled:
        while True:
            answer = ask("Hysteria2: порт (UDP)", str(HY2_PORT_DEFAULT))
            if answer.isdigit() and 1 <= int(answer) <= 65535:
                hy2_port = int(answer)
                break
            print("  порт — число 1..65535")

    wg_enabled = ask_yes("WireGuard-выход включаем?", default=False)
    wg_port = WG_PORT_DEFAULT
    if wg_enabled:
        while True:
            answer = ask("WireGuard-выход: порт (UDP)", str(WG_PORT_DEFAULT))
            if not answer.isdigit() or not 1 <= int(answer) <= 65535:
                print("  порт — число 1..65535")
                continue
            wg_port = int(answer)
            if hy2_enabled and wg_port == hy2_port:
                print(f"  {wg_port}/udp уже занят Hysteria2 — выбери другой")
                continue
            break

    return {
        "dest": dest,
        "sni": sni,
        "reality_port": reality_port,
        "hy2_enabled": hy2_enabled,
        "hy2_port": hy2_port,
        "wg_enabled": wg_enabled,
        "wg_port": wg_port,
    }


def firewall_block(transports: dict) -> str:
    """Списки firewall выводятся из транспортов, а не пишутся руками.

    SSH сюда не попадает намеренно: его открывает сама роль по ssh_port и с
    rate-limit.
    """
    lines = [
        "      firewall_allowed_tcp_ports:",
        f'        - {{port: {transports["reality_port"]}, comment: "VLESS Reality"}}',
    ]
    udp = []
    if transports["hy2_enabled"]:
        udp.append(f'        - {{port: {transports["hy2_port"]}, comment: "Hysteria2"}}')
    if transports["wg_enabled"]:
        udp.append(f'        - {{port: {transports["wg_port"]}, comment: "WireGuard exit"}}')
    if udp:
        lines.append("      firewall_allowed_udp_ports:")
        lines.extend(udp)
    else:
        lines.append("      firewall_allowed_udp_ports: []")
    return "\n".join(lines) + "\n"


CLIENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def ask_clients() -> tuple[list[str], list[str]]:
    """Шаг 8: имена клиентов и те из них, что сидят на App Store sing-box.

    UUID здесь не спрашиваются и не придумываются: их кладёт в шифротекст
    scripts/add-client.sh, он же дописывает имя в инвентарь — одной атомарной
    операцией. Иначе список в инвентаре и список в SOPS разъедутся, а это
    ровно та расходимость, которую потом приходится разбирать отдельно.
    """
    while True:
        answer = ask("имена клиентов через запятую", "phone")
        names = [item.strip() for item in answer.replace(";", ",").split(",")]
        names = [name for name in names if name]
        bad = [name for name in names if not CLIENT_RE.match(name)]
        if bad:
            print(f"  не годятся как имена: {', '.join(bad)}")
            continue
        if not names:
            print("  нужен хотя бы один клиент")
            continue
        if len(set(names)) != len(names):
            print("  имена повторяются")
            continue
        break

    legacy: list[str] = []
    if ask_yes("кто-то из них на App Store sing-box (схема 1.11)?", default=False):
        while True:
            answer = ask("имена legacy-клиентов через запятую", "")
            legacy = [item.strip() for item in answer.replace(";", ",").split(",")]
            legacy = [name for name in legacy if name]
            unknown = [name for name in legacy if name not in names]
            if unknown:
                print(f"  таких клиентов нет в списке: {', '.join(unknown)}")
                continue
            break
    return names, legacy


def map_has_node(entry_file: Path, node: str) -> bool:
    """Есть ли запись ноды в карте. Читается открытая часть, без расшифровки."""
    try:
        from first_contact_text import _locate

        _locate(entry_file.read_text(encoding="utf-8"), node)
        return True
    except Exception:
        return False


def add_clients(private: Path, node: str, names: list[str]) -> str | None:
    """Заводит клиентов владельцем правила — scripts/add-client.sh."""
    script = ROOT / "scripts" / "add-client.sh"
    secrets = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    environment = dict(os.environ)
    environment.update(
        {
            "VPN_FLEET_SECRETS_FILE": str(secrets),
            "VPN_FLEET_SOPS_FILENAME_OVERRIDE": str(secrets),
            "VPN_FLEET_SOPS_CONFIG": str(private / ".sops.yaml"),
            "VPN_FLEET_INVENTORY_FILE": str(
                private / "inventories" / "nodes" / f"{node}.yml"
            ),
        }
    )
    for name in names:
        result = subprocess.run(
            [str(script), node, name],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            return f"клиент {name} не заведён: {detail[-1] if detail else 'без причины'}"
        say(f"клиент {name} заведён: имя в инвентаре, UUID в шифротексте")
    return None


def show_plan(
    *,
    node: str,
    hoster_name: str,
    profile: dict,
    host: str,
    user: str,
    port: int,
    method: str,
    password: str,
    steady_user: str,
    steady_port: int,
    transports: dict,
    clients: list[str],
    legacy_clients: list[str],
    inventory: Path,
    entry_file: Path,
    secrets_file: Path,
) -> None:
    """Сводка перед записью: человек видит весь план до того, как он исполнен.

    Пароль в сводку не попадает — только факт, что он задан, и его длина.
    """
    udp = []
    if transports["hy2_enabled"]:
        udp.append(f"{transports['hy2_port']}")
    if transports["wg_enabled"]:
        udp.append(f"{transports['wg_port']}")

    if method == "key":
        door = f"{user}:{port}, ключом парка, пароля нет"
    elif password:
        door = f"{user}:{port}, паролем (задан, {len(password)} символов)"
    else:
        door = f"{user}:{port}, паролем (ПУСТ — впишешь позже)"

    names = ", ".join(
        f"{name} (legacy)" if name in legacy_clients else name for name in clients
    )

    print()
    print("Что будет сделано:")
    print(f"  сервер          {node} — {profile.get('title', hoster_name)}, "
          f"маршрут {route_of(profile)}")
    print(f"  адрес           {host}")
    print(f"  дверь           {door}")
    print(f"  после бутстрапа {steady_user}:{steady_port} — одним значением в "
          "ansible_port, bootstrap_ssh_port и ssh_port")
    print(f"  Reality         {transports['dest']} (SNI {transports['sni']}), "
          f"{transports['reality_port']}/tcp")
    print(f"  Hysteria2       "
          f"{str(transports['hy2_port']) + '/udp' if transports['hy2_enabled'] else 'выключен'}")
    print(f"  WireGuard-выход "
          f"{str(transports['wg_port']) + '/udp' if transports['wg_enabled'] else 'выключен'}")
    print(f"  firewall        tcp {transports['reality_port']}; "
          f"udp {', '.join(udp) if udp else '—'}")
    print(f"  клиенты         {names}")
    print()
    print("Файлы:")
    print(f"  создать   {inventory}")
    print(f"  дописать  {entry_file} — запись {node}")
    if secrets_file.is_file():
        print(f"  дописать  {secrets_file} — строки ноды и клиенты")
    else:
        print(f"  создать   {secrets_file} — строки ноды и клиенты")
    print(f"  завести   клиентов через scripts/add-client.sh: {len(clients)}")
    print()


def ask_password() -> str:
    """Пароль первого входа: скрытый ввод, значение не печатается никогда.

    Печатается только длина. Это не утечка, а единственный способ заметить
    обрезанную вставку до того, как она уедет в шифротекст и первый вход
    упрётся в неверный пароль.
    """
    while True:
        value = getpass.getpass("пароль первого входа (не отображается, можно вставить): ")
        if not value:
            return ""
        stripped = value.strip()
        if stripped != value:
            say(f"по краям вставки нашлись пробелы или перевод строки ({len(value)} символов)")
            if ask_yes("  убрать их?"):
                value = stripped
        if len(value) < 6 and not ask_yes(
            f"  всего {len(value)} символов — точно весь пароль?", default=False
        ):
            continue
        say(f"пароль принят: {len(value)} символов, значение не показано")
        return value


def door_state(module, node: str, private: Path) -> str:
    result = module.inspect_door(node, private)
    if result.reasons:
        return "не готов: " + result.reasons[0]
    return result.state


def known_servers(private: Path) -> list[str]:
    nodes = private / "inventories" / "nodes"
    return sorted(path.stem for path in nodes.glob("*.yml"))


def hoster_counts(private: Path) -> dict[str, int]:
    """Сколько серверов уже заведено у каждого хостера.

    Считается по node_location в инвентаре — тому самому полю, которым
    визард потом находит дверь этого хостера. Второго источника правды о
    принадлежности ноды нет и заводить не надо.
    """
    counts: dict[str, int] = {}
    if yaml is None:
        return counts
    nodes = private / "inventories" / "nodes"
    for path in sorted(nodes.glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        try:
            host = data["all"]["hosts"][path.stem]
        except (KeyError, TypeError):
            continue
        name = str(host.get("node_location", "")) if isinstance(host, dict) else ""
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def ask_port(default: int = 22) -> int:
    while True:
        answer = ask("порт первого входа", str(default))
        if answer.isdigit() and 1 <= int(answer) <= 65535:
            return int(answer)
        print("  порт — число 1..65535")


def write_inventory(
    private: Path,
    node: str,
    host: str,
    key_file: Path,
    location: str,
    steady_user: str,
    steady_port: int,
    transports: dict,
    legacy_clients: list[str],
) -> tuple[bool, Path]:
    target = private / "inventories" / "nodes" / f"{node}.yml"
    if target.exists():
        return False, target
    template = ROOT / "inventories" / "nodes" / "example.yml.template"
    if not template.is_file():
        raise ValueError(f"нет образца инвентаря {template}")

    text = template.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^    example:$", f"    {node}:", text)
    text = re.sub(r"(?m)^(      ansible_host:).*$", rf"\1 {host}", text)
    text = re.sub(r"(?m)^(      node_name:).*$", rf"\1 {node}", text)
    text = re.sub(
        r"(?m)^(      ansible_ssh_private_key_file:).*$", rf"\1 {key_file}", text
    )
    text = re.sub(r'(?m)^(      node_location:).*$', rf'\1 "{location}"', text)
    # Четыре поля steady-state держатся вместе: ansible_* — чем ходим после
    # бутстрапа, bootstrap_* — во что нода переезжает. Разъедутся — бутстрап
    # уедет в один порт, а команды после него пойдут в другой.
    text = re.sub(r"(?m)^(      ansible_user:).*$", rf"\1 {steady_user}", text)
    text = re.sub(r"(?m)^(      ansible_port:).*$", rf"\1 {steady_port}", text)
    text = re.sub(r"(?m)^(      bootstrap_user:).*$", rf"\1 {steady_user}", text)
    text = re.sub(r"(?m)^(      bootstrap_ssh_port:).*$", rf"\1 {steady_port}", text)
    text = re.sub(r"(?m)^(      ssh_port:).*$", rf"\1 {steady_port}", text)

    text = re.sub(
        r'(?m)^(      singbox_reality_dest:).*$', rf'\1 "{transports["dest"]}"', text
    )
    text = re.sub(
        r'(?m)^(      singbox_reality_sni:).*$', rf'\1 "{transports["sni"]}"', text
    )
    text = re.sub(
        r"(?m)^(      singbox_reality_port:).*$", rf"\1 {transports['reality_port']}", text
    )
    # The local Xray backend must never compete with the public HAProxy port.
    backend_port = 1444 if transports["reality_port"] == 1443 else 1443
    text = re.sub(
        r"(?m)^(      xray_listen_port:).*$", rf"\1 {backend_port}", text
    )
    text = re.sub(
        r"(?m)^(      singbox_hy2_enabled:).*$",
        rf"\1 {str(transports['hy2_enabled']).lower()}", text,
    )
    text = re.sub(
        r"(?m)^(      singbox_hy2_port:).*$", rf"\1 {transports['hy2_port']}", text
    )
    text = re.sub(
        r"(?m)^(      singbox_wg_exit_enabled:).*$",
        rf"\1 {str(transports['wg_enabled']).lower()}", text,
    )
    text = re.sub(
        r"(?m)^(      singbox_wg_exit_port:).*$", rf"\1 {transports['wg_port']}", text
    )

    # Имена клиентов из образца убираются: список наполняет add-client.sh,
    # который тем же движением кладёт UUID в SOPS. Пустой маркер — его вход.
    text = re.sub(
        r"(?m)^      singbox_client_names:\n(?:        - .*\n)*",
        "      singbox_client_names:\n",
        text,
        count=1,
    )
    if legacy_clients:
        legacy_block = "      singbox_legacy_client_names:\n" + "".join(
            f"        - {name}\n" for name in legacy_clients
        )
    else:
        legacy_block = "      singbox_legacy_client_names: []\n"
    text = re.sub(
        r"(?m)^      singbox_legacy_client_names:(?: \[\])?\n(?:        - .*\n)*",
        legacy_block,
        text,
        count=1,
    )

    # Списки firewall переписываются целиком: они выводятся из транспортов.
    text = re.sub(
        # (?m), но не (?s): с флагом s точка съедает переводы строк и замена
        # утаскивает весь хвост файла. Проверено на себе.
        r"(?m)^      firewall_allowed_tcp_ports:\n(?:        - .*\n)*"
        r"      firewall_allowed_udp_ports:(?: \[\])?\n(?:        - .*\n)*",
        firewall_block(transports),
        text,
        count=1,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    pending_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=f".{node}.", suffix=".tmp", delete=False,
        ) as pending:
            pending_path = Path(pending.name)
            pending.write(text)
            pending.flush()
            os.fchmod(pending.fileno(), 0o600)
        os.replace(pending_path, target)
    finally:
        if pending_path is not None:
            pending_path.unlink(missing_ok=True)
    return True, target


def write_server_records(module, private: Path, config: Path, entry_file: Path,
                         record: dict, password: str, inventory_args: tuple):
    """Commit inventory first so a retry can find and finish the node."""
    try:
        inventory = write_inventory(*inventory_args)
    except (OSError, ValueError) as error:
        return None, f"инвентарь не сохранён: {type(error).__name__}"
    reason = module._run_sops_editor(
        config, entry_file, EDITOR_INSERT_ENTRY,
        {"record": json.dumps(record), "password": password,
         "module_dir": str(ROOT / "scripts")},
    )
    if reason:
        return None, reason
    return inventory, None


EDITOR_INSERT_ENTRY = """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

here = Path(__file__).parent
target = Path(sys.argv[1])
record = json.loads((here / "record").read_text(encoding="utf-8"))
password = (here / "password").read_text(encoding="utf-8")
sys.path.insert(0, (here / "module_dir").read_text(encoding="utf-8"))
from first_contact_text import insert_entry

target.write_text(
    insert_entry(
        target.read_text(encoding="utf-8"),
        record["node"],
        user=record["user"],
        port=record["port"],
        method=record["method"],
        password=password,
    ),
    encoding="utf-8",
)
"""

EDITOR_SET_PASSWORD = """#!/usr/bin/env python3
from pathlib import Path
import sys

here = Path(__file__).parent
target = Path(sys.argv[1])
node = (here / "node").read_text(encoding="utf-8")
password = (here / "password").read_text(encoding="utf-8")
sys.path.insert(0, (here / "module_dir").read_text(encoding="utf-8"))
from first_contact_text import replace_password

target.write_text(
    replace_password(target.read_text(encoding="utf-8"), node, password),
    encoding="utf-8",
)
"""

EDITOR_ENSURE_SECRETS = """#!/usr/bin/env python3
import json
from pathlib import Path
import re
import sys

here = Path(__file__).parent
target = Path(sys.argv[1])
wanted = json.loads((here / "wanted").read_text(encoding="utf-8"))
text = target.read_text(encoding="utf-8")
missing = [
    f"{name}: {value}"
    for name, value in wanted.items()
    if not re.search(r"(?m)^" + re.escape(name) + r":", text)
]
if not missing:
    raise SystemExit(0)
if not text.endswith("\\n"):
    text += "\\n"
target.write_text(text + "\\n".join(missing) + "\\n", encoding="utf-8")
"""


def secret_keys(node: str, hy2: dict[str, str] | None = None,
                reality: dict[str, str] | None = None) -> dict[str, str]:
    prefix = "node_" + node.replace("-", "_")
    values = {f"{prefix}_{name}": value for name, value in SECRET_DEFAULTS.items()}
    if hy2 is not None:
        values.update({f"{prefix}_{name}": json.dumps(value)
                       for name, value in hy2.items()})
    if reality is not None:
        values.update({f"{prefix}_{name}": json.dumps(value)
                       for name, value in reality.items()})
    return values


def generate_reality_identity() -> dict[str, str]:
    """Generate an Xray-compatible clamped X25519 pair without an external binary."""
    spec = importlib.util.spec_from_file_location(
        "vpn_fleet_reality_check", ROOT / "scripts" / "check-reality-keypair.py"
    )
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    scalar = bytearray(secrets.token_bytes(32))
    scalar[0] &= 248
    scalar[31] = (scalar[31] & 127) | 64
    encode = lambda raw: base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return {
        "reality_private_key": encode(scalar),
        "reality_public_key": encode(checker.x25519_public(bytes(scalar))),
        "reality_short_id": secrets.token_hex(8),
    }


def hy2_sni(private: Path, node: str) -> str:
    """Read the exact SNI the sing-box role will use for this node."""
    inventory = yaml.safe_load(
        (private / "inventories" / "nodes" / f"{node}.yml").read_text(encoding="utf-8")
    )
    host = inventory["all"]["hosts"][node]
    defaults = yaml.safe_load(
        (ROOT / "roles" / "singbox" / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    sni = host.get("singbox_hy2_sni", defaults["singbox_hy2_sni"])
    if not isinstance(sni, str) or not HOSTNAME_RE.fullmatch(sni):
        raise ValueError("singbox_hy2_sni: ожидается доменное имя")
    return sni


def generate_hy2_identity(sni: str) -> dict[str, str]:
    """Generate one identity; the private key exists only in a 0700 temp dir."""
    with tempfile.TemporaryDirectory(prefix="vpn-fleet-hy2-") as directory:
        key_path = Path(directory) / "hy2.key"
        cert_path = Path(directory) / "hy2.crt"
        old_umask = os.umask(0o077)
        try:
            result = subprocess.run(
                ["openssl", "req", "-new", "-x509", "-newkey", "ec",
                 "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "3650",
                 "-subj", f"/CN={sni}", "-keyout", str(key_path),
                 "-out", str(cert_path)],
                capture_output=True, check=False,
            )
        finally:
            os.umask(old_umask)
        if result.returncode != 0:
            raise ValueError("openssl не создал Hy2 certificate/key")
        cert = cert_path.read_text(encoding="ascii")
        key = key_path.read_text(encoding="ascii")
        public = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-pubkey", "-noout"],
            capture_output=True, check=True,
        ).stdout
        der = subprocess.run(
            ["openssl", "pkey", "-pubin", "-outform", "DER"],
            input=public, capture_output=True, check=True,
        ).stdout
    return {
        "hy2_password": "hy2_" + secrets.token_urlsafe(32),
        "hy2_certificate": cert,
        "hy2_key": key,
        "hy2_certificate_public_key_sha256": base64.b64encode(
            hashlib.sha256(der).digest()
        ).decode("ascii"),
    }


def missing_secret_keys(target: Path, node: str) -> list[str]:
    """Расшифровка не нужна: sops шифрует значения, имена переменных видны в файле."""
    try:
        present = target.read_text(encoding="utf-8")
    except OSError:
        return list(secret_keys(node)) + [f"node_{node.replace('-', '_')}_{name}"
                                          for name in HY2_NAMES + REALITY_NAMES]
    expected = list(secret_keys(node)) + [f"node_{node.replace('-', '_')}_{name}"
                                          for name in HY2_NAMES + REALITY_NAMES]
    return [
        key
        for key in expected
        if not re.search(rf"(?m)^{re.escape(key)}:", present)
    ]


def new_server(
    module, private: Path, fleet: str, chosen: tuple[str, dict] | None = None
) -> int:
    """Завести сервер у названного хостера.

    Хостер приходит уже выбранным, когда сюда ведёт меню визарда: выбор
    маршрута стоит раньше создания, а не внутри него. Отдельный запуск
    (make new-server) спрашивает его сам — тем же меню.
    """
    config = private / ".sops.yaml"
    entry_file = private / "inventories" / "first-entry.sops.yml"
    secrets_file = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    for path in (config, entry_file):
        if not path.is_file():
            return fail(f"нет файла {path} — сначала wizard/private_tree.py (шаг 2)")

    if chosen is None:
        try:
            chosen = select_hoster(hoster_counts(private))
        except ValueError as error:
            return fail(str(error))
        if chosen is None:
            return 0
    hoster_name, profile = chosen
    print_route(hoster_name, profile)

    route = route_of(profile)
    if route == "terraform":
        say(f"{hoster_name} создаёт ноду терраформом: без неё заводить нечего")
        if not ask_yes("нода уже создана и IP известен?", default=False):
            print()
            print("Тогда сначала маршрут терраформа:")
            print("  terraform/terraform.tfvars — имя, тип, локация, наш публичный ключ")
            print("  make provision")
            print("  cd terraform && terraform output -raw ipv4")
            print("и возвращайся сюда с адресом.")
            return 0

    door = door_of(profile)
    existing = known_servers(private)
    suggested = suggest_name(existing, fleet, hoster_name)
    while True:
        node = ask("имя сервера", suggested)
        # Правило имени берётся не отсюда: scripts/add-client.sh проверяет его
        # строже (строчные, цифры, дефис) и откажется работать с чужой формой.
        # Разойдись они — визард завёл бы ноду, к которой нельзя добавить клиента.
        if not NODE_RE.match(node):
            print("  имя ноды — строчные буквы, цифры и дефис, начиная с буквы или цифры")
            continue
        if marker_path(private, node).exists():
            return fail(f"{node}: удаление не завершено — доведи удаление через Servers")
        if node in existing:
            print(f"  {node} уже заведён — выбери его в списке, чтобы дозаполнить")
            continue
        # Запись в карте без инвентаря — след прошлой прерванной попытки.
        # Молча завести вторую нельзя: карта такого не допускает, и отказ
        # прилетел бы позже и невнятнее.
        if map_has_node(entry_file, node):
            print(
                f"  в карте первого входа уже есть запись {node}, а инвентаря нет."
                " Останки прошлой попытки: убери запись (make secrets-edit FILE="
                f"{entry_file}) или возьми другое имя"
            )
            continue
        break

    while True:
        host = ask("публичный адрес (IP или имя хоста)")
        valid, note = check_address(host)
        if not valid:
            print(f"  {note}")
            continue
        if note and not ask_yes(f"  {note}. Всё равно берём этот адрес?", default=False):
            continue
        twin = address_taken_by(private, host)
        if twin and not ask_yes(
            f"  этот адрес уже стоит у ноды {twin}. Всё равно берём?", default=False
        ):
            continue
        break

    # Проверка сетью, но безобидная: смотрим, отвечает ли SSH, и ничего не
    # принимаем и не записываем. Host key примет только first-contact.
    probe_port = int(door.get("port", 22))
    if ask_yes(f"проверить, отвечает ли SSH на {probe_port}?"):
        if probe_ssh(host, probe_port):
            say(f"по адресу отвечает SSH на порту {probe_port}")
        else:
            warn(
                f"на порту {probe_port} никто не ответил. Если нода только что "
                "создана — дай ей минуту; если порт другой — поправь дверь ниже"
            )

    # Дефолты приходят из профиля хостера, но любое поле можно переопределить:
    # образ или шаблон ноды может отдать её иначе, чем обычно у этого хостера.
    while True:
        user = ask("пользователь первого входа", str(door.get("user", "root")))
        if USER_RE.match(user):
            break
        print("  имя пользователя — буквы, цифры, точка, дефис и подчёркивание")
    port = ask_port(int(door.get("port", 22)))
    method = ask_choice(
        "чем открывается дверь", ("password", "key"), str(door.get("method", "password"))
    )

    key_file = Path(f"~/.ssh/{fleet}_ed25519")
    password = ""
    if method == "key":
        # У такой двери пароля нет вовсе — и поля password в записи тоже. Дверь
        # держится на ключе, поэтому он обязан читаться прямо сейчас: иначе
        # ноду нечем открыть, и узнать это лучше здесь, чем в сети.
        readable = Path(os.path.expanduser(str(key_file)))
        if not (readable.is_file() and os.access(readable, os.R_OK)):
            return fail(
                f"method: key, но ключ {readable} не читается — нечем открывать "
                "дверь. Сначала wizard/keys.sh (шаг 1)"
            )
        say(f"дверь открывается ключом {readable}; пароля у неё нет")
    else:
        password = ask_password()
        if not password:
            say("пароль пуст — впишешь позже: make secrets-edit FILE=" + str(entry_file))

    steady_user, steady_port = ask_steady_access(port)
    transports = ask_transports(steady_port)
    clients, legacy_clients = ask_clients()

    inventory_path = private / "inventories" / "nodes" / f"{node}.yml"
    show_plan(
        node=node,
        hoster_name=hoster_name,
        profile=profile,
        host=host,
        user=user,
        port=port,
        method=method,
        password=password,
        steady_user=steady_user,
        steady_port=steady_port,
        transports=transports,
        clients=clients,
        legacy_clients=legacy_clients,
        inventory=inventory_path,
        entry_file=entry_file,
        secrets_file=secrets_file,
    )
    if not ask_yes("писать?"):
        say("ничего не записано")
        return 0

    written, reason = write_server_records(
        module, private, config, entry_file,
        {"node": node, "user": user, "port": port, "method": method}, password,
        (private, node, host, key_file, hoster_name, steady_user, steady_port,
         transports, legacy_clients),
    )
    if reason:
        return fail(f"запись первого входа не сохранена: {reason}")
    assert written is not None
    created, inventory = written
    say(f"{'создан' if created else 'уже был'} инвентарь: {inventory}")
    say(f"запись в карте первого входа заведена: {node}")

    return finish(module, private, node, secrets_file, config, clients)


def fill_server(module, private: Path, node: str) -> int:
    if marker_path(private, node).exists():
        return fail(f"{node}: удаление не завершено — доведи удаление")
    config = private / ".sops.yaml"
    entry_file = private / "inventories" / "first-entry.sops.yml"
    secrets_file = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"

    found, _, _, reasons = module._entry_for_node(node, private)
    if reasons:
        return fail(reasons[0])
    if not found and ask_yes(f"{node}: записи первого входа нет. Создать её для незавершённой подготовки?", default=False):
        user = ask("пользователь первого входа", "root")
        port = ask_port()
        method = ask_choice("чем открывается дверь", ("password", "key"), "password")
        password = ask_password() if method == "password" else ""
        reason = module._run_sops_editor(
            config, entry_file, EDITOR_INSERT_ENTRY,
            {"record": json.dumps({"node": node, "user": user, "port": port,
                                   "method": method}), "password": password,
             "module_dir": str(ROOT / "scripts")},
        )
        if reason:
            return fail(f"запись первого входа не сохранена: {reason}")

    state = door_state(module, node, private)
    say(f"{node}: дверь — {state}")

    if "поле password" in state and "пусто" in state:
        password = getpass.getpass("пароль первого входа (не отображается): ")
        if password:
            reason = module._run_sops_editor(
                config,
                entry_file,
                EDITOR_SET_PASSWORD,
                {
                    "node": node,
                    "password": password,
                    "module_dir": str(ROOT / "scripts"),
                },
            )
            if reason:
                return fail(f"пароль не сохранён: {reason}")
            say("пароль записан шифротекстом")

    reason = claim_known_hosts(module, private, node)
    if reason:
        return fail(reason)
    return finish(module, private, node, secrets_file, config, [])


def claim_known_hosts(module, private: Path, node: str) -> str | None:
    """Offer to assign pre-label rows only when the address has one owner."""
    known = private / "inventories" / "known_hosts"
    if not known.is_file():
        return None
    inventory = private / "inventories" / "nodes" / f"{node}.yml"
    data, reasons = module._load_yaml(inventory, str(inventory))
    if reasons:
        return reasons[0]
    try:
        host = data["all"]["hosts"][node]
        address = host["ansible_host"]
    except (KeyError, TypeError):
        return "ansible_host не задан"
    if not isinstance(address, str) or not address:
        return "ansible_host не задан"
    _, entry, _, reasons = module._entry_for_node(node, private)
    if reasons:
        return reasons[0]
    hosters, _ = load_hosters()
    profile_port = door_of(hosters.get(str(host.get("node_location", "")), {})).get("port")
    door_port = entry.get("port") if entry else profile_port
    steady_port = host.get("bootstrap_ssh_port", host.get("ansible_port"))
    tokens = {}
    for role, port in (("door", door_port), ("steady", steady_port)):
        if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
            tokens.setdefault(module._known_host_token(address, port), role)
    lines = known.read_text(encoding="utf-8").splitlines()
    candidates = []
    for index, line in enumerate(lines):
        fields = module._known_host_fields(line)
        if (fields and len(fields[0]) == 1 and fields[0][0] in tokens
                and module._known_host_owner(line) is None):
            candidates.append((index, fields[0][0], tokens[fields[0][0]]))
    if not candidates:
        return None
    shared = []
    for path in sorted(inventory.parent.glob("*.yml")):
        if path == inventory:
            continue
        other, errors = module._load_yaml(path, str(path))
        if errors:
            return errors[0]
        try:
            if other["all"]["hosts"][path.stem]["ansible_host"] == address:
                shared.append(path.stem)
        except (KeyError, TypeError):
            return f"{path.name}: ansible_host не задан"
    if shared:
        return f"known_hosts: адрес общий с {', '.join(shared)}; строки без владельца не присвоены"
    say(f"known_hosts: присвоить {len(candidates)} строк(и) ноде {node} "
        f"({', '.join(f'{token} → {role}' for _, token, role in candidates)})")
    if ask_yes("Присвоить строки known_hosts этой ноде?", default=False):
        for index, _, role in candidates:
            lines[index] = module._label_known_host_line(lines[index], node, role)
        module._write_known_host_lines(known, lines)
    return None


SECRETS_HEADER = """# Секреты парка: Reality-ключи, UUID клиентов, Hy2 identity, пароли консоли.
# ЧАСТНЫЙ файл, шифруется sops + age ЦЕЛИКОМ — вместе с комментариями.
#
# Имена переменных остаются видны в шифротексте, и это нарочно: по ним гейт
# отвечает «заведены ли секреты этой ноды», не расшифровывая файл.
#
# Строки заводит wizard/server.py по префиксу ноды; оставшиеся значения правятся:
#   make secrets-edit
"""


def create_secrets(target: Path, config: Path, node: str,
                   hy2: dict[str, str], reality: dict[str, str]) -> str | None:
    """Первый сервер заводит и сам файл: sops не шифрует документ без данных."""
    from secrets_lock import secrets_lock

    body = "".join(f"{name}: {value}\n" for name, value in secret_keys(node, hy2, reality).items())
    target.parent.mkdir(parents=True, exist_ok=True)
    with secrets_lock(target):
        if target.exists():
            return "файл секретов уже создан другим процессом; повтори запуск"
        # Открытый текст живёт во временном каталоге, шифротекст — рядом с
        # целью: /tmp бывает другой файловой системой, и os.replace через
        # границу устройств падает с EXDEV.
        handle, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                        dir=target.parent)
        os.close(handle)
        encrypted = Path(name)
        try:
            with tempfile.TemporaryDirectory(prefix="vpn-fleet-sops-") as directory:
                plain = Path(directory) / "plain.yml"
                plain.write_text(SECRETS_HEADER + body, encoding="utf-8")
                result = subprocess.run(
                    ["sops", "--config", str(config), "--encrypt", "--input-type", "yaml",
                     "--output-type", "yaml", "--filename-override", str(target),
                     "--output", str(encrypted), str(plain)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            if result.returncode != 0:
                return f"sops не зашифровал {target}; файл не оставлен"
            encrypted.chmod(0o600)
            os.replace(encrypted, target)
        finally:
            encrypted.unlink(missing_ok=True)
    return None


def finish(
    module, private: Path, node: str, secrets_file: Path, config: Path,
    clients: list[str],
) -> int:
    if marker_path(private, node).exists():
        return fail(f"{node}: удаление не завершено — доведи удаление")
    missing = missing_secret_keys(secrets_file, node)
    prefix = "node_" + node.replace("-", "_") + "_"
    missing_hy2 = [name for name in HY2_NAMES if prefix + name in missing]
    missing_reality = [name for name in REALITY_NAMES if prefix + name in missing]
    if 0 < len(missing_reality) < len(REALITY_NAMES):
        return fail("Reality identity неполная: ротация и частичная замена не выполняются; "
                    "исправь три поля вручную отдельной процедурой")
    if 0 < len(missing_hy2) < len(HY2_NAMES):
        return fail("Hy2 identity неполная: ротация и частичная замена не выполняются; "
                    "исправь четыре поля вручную отдельной процедурой")
    try:
        hy2 = generate_hy2_identity(hy2_sni(private, node)) if missing_hy2 else None
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        return fail(f"Hy2 identity не создана: {type(error).__name__}")
    try:
        reality = generate_reality_identity() if missing_reality else None
    except (OSError, ValueError, KeyError, TypeError) as error:
        return fail(f"Reality identity не создана: {type(error).__name__}")
    if not secrets_file.is_file():
        assert hy2 is not None
        assert reality is not None
        reason = create_secrets(secrets_file, config, node, hy2, reality)
        if reason:
            return fail(reason)
        say("заведён файл секретов со строками этой ноды")
        reason = add_clients(private, node, clients)
        if reason:
            return fail(reason)
        return report(module, private, node)

    if missing:
        reason = module._run_sops_editor(
            config,
            secrets_file,
            EDITOR_ENSURE_SECRETS,
            {"wanted": json.dumps({key: value for key, value in
                                   secret_keys(node, hy2, reality).items() if key in missing})},
        )
        if reason:
            return fail(f"строки секретов не дописаны: {reason}")
        say("в секреты дописаны строки: " + ", ".join(missing))
    else:
        say("строки секретов на месте")

    reason = add_clients(private, node, clients)
    if reason:
        return fail(reason)
    return report(module, private, node)


def report(module, private: Path, node: str) -> int:
    state = door_state(module, node, private)
    print()
    print(f"Сервер {node} готов настолько, насколько может быть до касания.")
    print(f"  дверь: {state}")
    print()
    print("Дальше:")
    print(f"  scripts/check-private-inputs.sh {node}")
    print(f"  make first-contact NODE={node}")
    return 0


def main() -> int:
    set_step("server")
    if yaml is None:
        return fail("не хватает зависимости PyYAML; установи requirements-ci.txt")
    if not sys.stdin.isatty():
        return fail("скрипт интерактивный: запускай его в терминале")

    private = private_dir()
    try:
        fleet = resolve_fleet()
    except ValueError as error:
        return fail(str(error))
    displaced = use_fleet_age_key(fleet)
    if displaced:
        say(f"SOPS_AGE_KEY_FILE указывал на {displaced} — беру ключ установки")
    say(f"установка: {fleet}")

    module = first_contact_module()
    servers = known_servers(private)

    # Отдельный запуск (make new-server) — это шаг «завести сервер», а не
    # копия верхнего меню визарда: там список серверов уже есть, и повторять
    # его здесь значило бы дважды спрашивать одно и то же.
    if not servers:
        return new_server(module, private, fleet)

    while True:
        picked = menu("SERVER:", ["Create server", "Fill in existing server", "Quit"])
        if picked == 3:
            return 0
        if picked == 1:
            return new_server(module, private, fleet)

        items = [f"{node} — {door_state(module, node, private)}" for node in servers]
        items += ["Back", "Quit"]
        chosen = menu("SERVERS:", items)
        if chosen == len(servers) + 1:
            continue
        if chosen == len(servers) + 2:
            return 0
        return fill_server(module, private, servers[chosen - 1])


if __name__ == "__main__":
    raise SystemExit(main())
