#!/usr/bin/env python3
"""Предполёт и запуск бутстрапа ноды.

Бутстрап — единственный шаг, который закрывает за собой дверь: он заводит
devops, переносит SSH на нестандартный порт и выключает root и парольный вход.
Ошибка здесь стоит доступа к машине, поэтому всё, что можно проверить локально,
проверяется ДО того, как плейбук тронет ноду.

Проверок четыре, и каждая отвечает на свой вопрос:

1. Первое касание пройдено? Если запись ноды всё ещё в карте — бутстрапу рано.
2. Чем мы войдём после? Ключ, которым ходит ansible, обязан быть в списке
   админов: роль ставит пользователю devops РОВНО этот список (exclusive) и
   следом выключает пароль и root.
3. Будет ли известен host key на новом порту? Запись вида [<host>]:<port>
   добавляет сам плейбук после переезда; предполёт лишь говорит, есть она уже
   или появится в этом прогоне.
4. Какой дверью входим сейчас? Либо --from <user>@<port>, либо steady-state.

Сети скрипт не касается сам: всё сетевое делает плейбук, которого он зовёт.

Usage: wizard/bootstrap.py <node> [--from <user>@<port>] [--check]
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
FROM_RE = re.compile(r"^(?P<user>[^@\s]+)@(?P<port>\d{1,5})$")


def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def say(message: str) -> None:
    print(f"bootstrap: {message}")


def warn(message: str) -> None:
    print(f"WARN: {message}", file=sys.stderr)


def first_contact_module():
    """Владелец правил двери и формы known_hosts; имя с дефисом не импортируется."""
    path = ROOT / "scripts" / "first-contact.py"
    spec = importlib.util.spec_from_file_location("vpn_fleet_first_contact", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def host_vars(module, private_dir: Path, node: str) -> tuple[dict | None, str | None]:
    inventory = private_dir / "inventories" / "nodes" / f"{node}.yml"
    if not inventory.is_file():
        return None, f"нет инвентаря {inventory}"
    data, reasons = module._load_yaml(inventory, str(inventory))
    if reasons:
        return None, reasons[0]
    try:
        host = data["all"]["hosts"][node]
    except (KeyError, TypeError):
        return None, f"{inventory}: в all.hosts нет mapping для {node}"
    if not isinstance(host, dict):
        return None, f"{inventory}: в all.hosts нет mapping для {node}"
    return host, None


def check_first_contact(module, private_dir: Path, node: str) -> str | None:
    """Бутстрапу рано, пока дверь описана записью в карте первого входа."""
    result = module.inspect_door(node, private_dir)
    if result.reasons:
        return result.reasons[0]
    node_name, _, inventory_reasons = module._inventory_key(node, private_dir)
    if inventory_reasons or node_name is None:
        return inventory_reasons[0] if inventory_reasons else "node_name не определён"
    found, _, _, entry_reasons = module._entry_for_node(node_name, private_dir)
    if entry_reasons:
        return entry_reasons[0]
    if found:
        return (
            f"{node}: запись в карте первого входа ещё на месте — "
            f"сначала «make first-contact NODE={node}»"
        )
    return None


def check_admin_key(private_dir: Path, host: dict, node: str) -> str | None:
    """Ключ, которым ходит ansible, обязан быть в списке админов.

    Роль ставит devops РОВНО ssh_authorized_keys_devops с exclusive: true и
    следом гасит пароль и root. Список без нашего ключа — это дверь, которая
    закрывается за нами снаружи.
    """
    keys_file = private_dir / "inventories" / "group_vars" / "all" / "ssh_keys.yml"
    if not keys_file.is_file():
        return f"нет {keys_file}: бутстрап оставит ноду без ключей"

    declared = host.get("ansible_ssh_private_key_file")
    if not isinstance(declared, str) or not declared.strip():
        return f"{node}: в инвентаре не объявлен ansible_ssh_private_key_file"
    public = Path(os.path.expanduser(declared.strip() + ".pub"))
    if not public.is_file():
        return f"{node}: не читается публичная часть ключа {public}"

    fields = public.read_text(encoding="utf-8").split()
    if len(fields) < 2:
        return f"{public}: не похоже на публичный ключ"
    mine = " ".join(fields[:2])

    listed = re.findall(
        r"pubkey:\s*['\"]?(ssh-[a-z0-9-]+ [A-Za-z0-9+/=]+)",
        keys_file.read_text(encoding="utf-8"),
    )
    if not listed:
        return (
            f"{keys_file}: ssh_authorized_keys_devops пуст — бутстрап заведёт "
            "devops без единого ключа и выключит пароль"
        )
    if mine not in listed:
        return (
            f"{keys_file}: ключа, которым ходит ansible, нет в "
            "ssh_authorized_keys_devops — после бутстрапа вход будет потерян"
        )
    return None


def check_known_hosts(module, private_dir: Path, host: dict, node: str) -> str | None:
    """Предупреждение, а не отказ: сам прогон идёт по старой двери и пройдёт."""
    address = host.get("ansible_host")
    port = host.get("bootstrap_ssh_port", host.get("ansible_port"))
    if not isinstance(address, str) or not isinstance(port, int):
        return f"{node}: не объявлены ansible_host или порт steady-state"
    known_hosts = private_dir / "inventories" / "known_hosts"
    token = module._known_host_token(address, port)
    if any(token in module._known_host_fields(line)[0]
           for line in module._owned_known_hosts(known_hosts, node, "steady")):
        return None
    return (
        f"в known_hosts пока нет ключа ноды на порт {port}. До первого бутстрапа "
        f"так и должно быть: плейбук добавит запись сам, после переезда порта и "
        f"только сверив полученный ключ с уже принятым для двери. Если после "
        f"прогона записи не появится — следующий шаг упадёт на Host key "
        f"verification failed."
    )


def check_port_on_node(
    module, private_dir: Path, host: dict, node: str, door: tuple[str, int] | None
) -> tuple[bool, str]:
    """Занят ли steady-state порт на самой ноде.

    До первого касания этого знать нельзя — внутрь мы попадаем только после
    него. Зато теперь можно и нужно: если порт занят, sshd туда не встанет,
    бутстрап оставит ноду на старом порту, а инвентарь будет утверждать обратное.

    Смотрим только слушающие сокеты, ничего не меняем.
    """
    steady_port = host.get("bootstrap_ssh_port", host.get("ansible_port"))
    address = host.get("ansible_host")
    key = host.get("ansible_ssh_private_key_file")
    if not isinstance(steady_port, int) or not isinstance(address, str) or not isinstance(key, str):
        return True, "порт или адрес не объявлены — проверить нечем"

    # A repeat run enters through the already established steady-state door.
    # Its listener is the sshd that makes this very check possible.
    if door is None or door[1] == steady_port:
        return True, f"порт {steady_port} уже используется текущей SSH-дверью"

    user, port = door if door else (
        str(host.get("bootstrap_user", "root")),
        int(host.get("bootstrap_ssh_port", 22)),
    )
    known_hosts = private_dir / "inventories" / "known_hosts"
    command = [
        "ssh",
        # -n и stdin=DEVNULL — не украшение. С унаследованным терминалом на
        # входе ssh остаётся висеть после того, как удалённая команда уже
        # отработала, и предполёт стоит бесконечно, ничего не печатая:
        # такой зависший сеанс не сообщает об ошибке.
        # Владелец правила двери (scripts/first-contact.py:_key_opens) так и
        # делает с самого начала — здесь этого просто не было.
        "-n",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", f"IdentityFile={os.path.expanduser(key)}",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=15",
        "-p", str(port),
        f"{user}@{address}",
        f"command -v ss >/dev/null && ss -Hltn 'sport = :{steady_port}' || echo NO-SS",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return True, f"проверка порта {steady_port} не уложилась в 30 с — пропускаю"
    except OSError as error:
        return True, f"проверку порта не удалось выполнить: {error}"
    if result.returncode != 0:
        return True, "нода не ответила на проверку порта — пропускаю, судить не по чему"
    output = result.stdout.strip()
    if output == "NO-SS":
        return True, f"на ноде нет ss — занятость порта {steady_port} не проверена"
    if not output:
        return True, f"порт {steady_port} на ноде свободен"
    return False, (
        f"порт {steady_port} на ноде уже кем-то слушается — sshd туда не встанет. "
        "Выбери другой порт и поправь инвентарь: ansible_port, bootstrap_ssh_port "
        "и ssh_port одним значением"
    )


def parse_from(value: str) -> tuple[str, int]:
    match = FROM_RE.match(value)
    if match is None:
        raise ValueError("--from задаётся как <user>@<port>, например ubuntu@22")
    port = int(match.group("port"))
    if not 1 <= port <= 65535:
        raise ValueError("--from: порт вне диапазона 1..65535")
    return match.group("user"), port


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("node", help="имя ноды в инвентаре")
    parser.add_argument(
        "--from",
        dest="door",
        default="",
        help="дверь свежей ноды: <user>@<port>, та, которую оставил first-contact",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="только предполёт, плейбук не запускать",
    )
    args = parser.parse_args()

    # Форма двери разбирается первой: опечатка в --from не повод сначала
    # ходить по файлам и печатать зелёные строки предполёта.
    door: tuple[str, int] | None = None
    if args.door:
        try:
            door = parse_from(args.door)
        except ValueError as error:
            return fail(str(error))

    private_dir = Path(os.environ.get("VPN_FLEET_PRIVATE_DIR", str(ROOT / "private")))
    module = first_contact_module()

    host, reason = host_vars(module, private_dir, args.node)
    if reason or host is None:
        return fail(reason or "инвентарь не прочитан")

    reason = check_first_contact(module, private_dir, args.node)
    if reason:
        return fail(reason)
    say("первое касание пройдено: дверь открывается ключом")

    reason = check_admin_key(private_dir, host, args.node)
    if reason:
        return fail(reason)
    say("ключ ansible есть в списке админов — бутстрап не запрёт нас снаружи")

    reason = check_known_hosts(module, private_dir, host, args.node)
    if reason:
        warn(reason)
    else:
        say("host key на steady-state порт известен")

    # Единственная проверка, которую нельзя сделать локально: внутрь ноды мы
    # попадаем только после первого касания.
    ok, note = check_port_on_node(module, private_dir, host, args.node, door)
    if not ok:
        return fail(note)
    say(note)

    command = ["ansible-playbook", "playbooks/bootstrap.yml", "-l", args.node]
    if door is not None:
        user, port = door
        command.extend(["-e", f"bootstrap_door_user={user}", "-e", f"bootstrap_door_port={port}"])
        say(f"дверь прогона: {user}:{port}")
    else:
        say(
            "дверь прогона: steady-state из инвентаря "
            f"({host.get('bootstrap_user', '?')}:{host.get('bootstrap_ssh_port', '?')}) "
            "— это повторный прогон на уже пробутстрапленной ноде"
        )

    if args.check:
        print()
        say("предполёт пройден; запуск не выполнялся (--check)")
        print("  " + " ".join(command))
        return 0

    print()
    say("запускаю плейбук")
    result = subprocess.run(command, cwd=str(ROOT), check=False)
    if result.returncode != 0:
        return result.returncode

    address = host.get("ansible_host", "<ip>")
    port = host.get("bootstrap_ssh_port", "?")
    user = host.get("bootstrap_user", "devops")
    print()
    say("готово. Проверь новый вход ДРУГИМ терминалом, не закрывая этот:")
    print(
        f"  ssh -p {port} {user}@{address} "
        "-o UserKnownHostsFile=private/inventories/known_hosts true"
    )
    say("если не пускает — консоль провайдера и node_<нода>_console_password в SOPS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
