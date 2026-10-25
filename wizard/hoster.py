#!/usr/bin/env python3
"""Шаг 3 визарда: хостер и маршрут.

Хостер — это не косметика и не тег. Он отвечает на два вопроса, которые дальше
определяют всё: КАК нода появляется на свет и КАКОЙ дверью она отдаётся.

Отсюда два маршрута, и они не сводятся друг к другу:

  panel      ноду создаёшь руками в панели провайдера, IP и пароль приходят
             письмом. Дверь — password (или key, если провайдер кладёт ключ).
             Так живут OVH и HostHatch.

  terraform  ноды нет вовсе, пока её не создал make provision: terraform
             заводит сервер и сразу кладёт в него наш публичный ключ. Пароля
             не существует в природе, дверь всегда key, а IP появляется только
             в выводе terraform. Так живёт Hetzner.

Каталог профилей — публичный inventories/hosters.yml: там дефолты и заметки,
координат нет. Новый профиль добавляется сюда же, чтобы в следующий раз быть
в списке.

Usage: wizard/hoster.py            выбрать хостер и увидеть его маршрут
       wizard/hoster.py --list     показать каталог
       wizard/hoster.py --add      завести профиль нового хостера
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    ROOT,
    NAME_RE,
    ask,
    ask_choice,
    fail,
    menu,
    say,
    set_step,
)

try:
    import yaml
except ImportError:  # pragma: no cover — названная причина вместо traceback
    yaml = None  # type: ignore[assignment]

CATALOGUE = ROOT / "inventories" / "hosters.yml"
ROUTES = ("panel", "terraform")


def load() -> tuple[dict, str | None]:
    if yaml is None:
        return {}, "не хватает зависимости PyYAML; установи requirements-ci.txt"
    if not CATALOGUE.is_file():
        return {}, f"нет каталога хостеров {CATALOGUE}"
    try:
        data = yaml.safe_load(CATALOGUE.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}, f"{CATALOGUE}: файл не разбирается как YAML"
    hosters = (data or {}).get("hosters")
    if not isinstance(hosters, dict) or not hosters:
        return {}, f"{CATALOGUE}: нет ни одного профиля в hosters"
    return hosters, None


def route_of(profile: dict) -> str:
    route = profile.get("provisioning")
    return route if route in ROUTES else "panel"


def door_of(profile: dict) -> dict:
    entry = profile.get("first_entry")
    return entry if isinstance(entry, dict) else {}


def describe(name: str, profile: dict) -> str:
    door = door_of(profile)
    return (
        f"{profile.get('title', name)} — {route_of(profile)}, дверь "
        f"{door.get('user', '?')}:{door.get('port', '?')} "
        f"({door.get('method', '?')})"
    )


def print_route(name: str, profile: dict) -> None:
    """Маршрут печатается целиком: человек должен видеть, куда он идёт."""
    route = route_of(profile)
    print()
    print(f"Маршрут хостера {profile.get('title', name)}: {route}")
    print()
    if route == "terraform":
        print("  1. terraform/terraform.tfvars — имя ноды, тип, локация, наш публичный ключ")
        print("  2. make provision — terraform создаёт сервер и кладёт в него ключ")
        print("  3. terraform output -raw ipv4 — адрес появляется только здесь")
        print("  4. make new-server — инвентарь и запись в карте: method key, пароля нет")
        print("  5. make first-contact — доказать вход ключом и погасить запись")
        print("  6. make bootstrap — devops, переезд порта, закрытие root")
    else:
        print("  1. создать ноду в панели провайдера, получить IP и данные входа")
        print("  2. make new-server — инвентарь и запись в карте первого входа")
        print("  3. make first-contact — принять host key, сменить пароль, поставить ключ")
        print("  4. make bootstrap — devops, переезд порта, закрытие root")
    print()
    notes = profile.get("notes")
    if isinstance(notes, list) and notes:
        print("Что знать про этого хостера:")
        for note in notes:
            print(f"  - {' '.join(str(note).split())}")
        print()


def choose(hosters: dict, counts: dict[str, int] | None = None) -> tuple[str, str, dict]:
    """Меню хостеров. Возвращает ('hoster', имя, профиль) либо ('back'|'quit', '', {}).

    Пункты «Create hoster», «Back» и «Quit» идут номерами следом за списком:
    буквенных ответов в визарде нет. Заводить сервер, не назвав хостера,
    нельзя — от него зависят и маршрут, и дверь, поэтому это меню стоит
    первым, а не внутри создания сервера.
    """
    while True:
        names = sorted(hosters)
        items = []
        for name in names:
            line = describe(name, hosters[name])
            if counts:
                number = counts.get(name, 0)
                line += f", серверов: {number}" if number else ", серверов нет"
            items.append(line)
        items += ["Create hoster", "Back", "Quit"]

        picked = menu("CHOOSE HOSTER:", items)
        if picked <= len(names):
            name = names[picked - 1]
            return "hoster", name, hosters[name]
        if picked == len(names) + 1:
            created = add(hosters)
            if created is None:
                continue
            name, profile = created
            hosters[name] = profile
            return "hoster", name, profile
        if picked == len(names) + 2:
            return "back", "", {}
        return "quit", "", {}


def add(hosters: dict) -> tuple[str, dict] | None:
    """Новый профиль дописывается в конец каталога: hosters — последний ключ.

    Перед записью профиль показывается целиком: каталог — файл репозитория, и
    молча дописывать в него строки нельзя. Отказ здесь — тот же шаг назад.
    """
    while True:
        name = ask("короткое имя хостера, например vultr")
        if not NAME_RE.match(name):
            print("  имя — буквы, цифры, дефис и подчёркивание")
            continue
        if name in hosters:
            print(f"  профиль {name} уже есть")
            continue
        break

    title = ask("человеческое имя", name)
    route = ask_choice("как появляется нода", ROUTES, "panel")
    user = ask("пользователь первого входа", "root")
    while True:
        port = ask("порт первого входа", "22")
        if port.isdigit() and 1 <= int(port) <= 65535:
            break
        print("  порт — число 1..65535")
    method = ask_choice(
        "чем открывается дверь", ("password", "key"),
        "key" if route == "terraform" else "password",
    )
    note = ask("заметка (что знать до первого входа), пустая — пропустить", " ").strip()

    block = [
        "",
        f"  {name}:",
        f"    title: {title}",
        f"    provisioning: {route}",
        "    first_entry:",
        f"      user: {user}",
        f"      port: {port}",
        f"      method: {method}",
    ]
    if note:
        block.extend(["    notes:", f'      - "{note}"'])
    print()
    print(f"ПРОФИЛЬ — допишется в {CATALOGUE}:")
    for line in block[1:]:
        print(f"  {line}")
    if menu("WRITE?:", ["Write", "Back"]) == 2:
        say("профиль не записан")
        return None

    text = CATALOGUE.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    CATALOGUE.write_text(text + "\n".join(block) + "\n", encoding="utf-8")
    say(f"профиль {name} добавлен в {CATALOGUE}")

    profile = {
        "title": title,
        "provisioning": route,
        "first_entry": {"user": user, "port": int(port), "method": method},
    }
    if note:
        profile["notes"] = [note]
    return name, profile


def select(counts: dict[str, int] | None = None) -> tuple[str, dict] | None:
    """Точка входа для других шагов: профиль либо None, если человек вышел."""
    hosters, reason = load()
    if reason:
        raise ValueError(reason)
    action, name, profile = choose(hosters, counts)
    if action != "hoster":
        return None
    return name, profile


def main() -> int:
    set_step("hoster")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="показать каталог")
    parser.add_argument("--add", action="store_true", help="завести новый профиль")
    args = parser.parse_args()

    hosters, reason = load()
    if reason:
        return fail(reason)

    if args.list:
        for name in sorted(hosters):
            print(f"{name}: {describe(name, hosters[name])}")
        return 0

    if not sys.stdin.isatty():
        return fail("шаг интерактивный: запускай его в терминале (или --list)")

    if args.add:
        created = add(hosters)
        if created is None:
            return 0
        name, profile = created
    else:
        action, name, profile = choose(hosters)
        if action != "hoster":
            return 0

    print_route(name, profile)
    print("Дальше — сервер: make new-server (шаги 4–9).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
