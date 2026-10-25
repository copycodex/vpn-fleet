"""Общее хозяйство шагов визарда на Python.

Здесь живёт то, что иначе разошлось бы копиями по шагам и однажды разъехалось:
печать с именем шага, правило имени установки, вопросы и загрузка владельца
правила двери. Сам по себе модуль ничего не делает.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_STEP = "wizard"


def set_step(name: str) -> None:
    global _STEP
    _STEP = name


def say(message: str) -> None:
    print(f"{_STEP}: {message}")


def warn(message: str) -> None:
    print(f"WARN: {message}", file=sys.stderr)


def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1


def age_dir() -> Path:
    return Path(os.environ.get("AGE_DIR", str(Path.home() / ".config" / "sops" / "age")))


def ssh_dir() -> Path:
    return Path(os.environ.get("SSH_DIR", str(Path.home() / ".ssh")))


def private_dir() -> Path:
    return Path(os.environ.get("VPN_FLEET_PRIVATE_DIR", str(ROOT / "private")))


def age_key_of(fleet: str) -> Path:
    return age_dir() / f"{fleet}.txt"


def fleet_key_of(fleet: str) -> Path:
    return ssh_dir() / f"{fleet}_ed25519"


def resolve_fleet() -> str:
    """Имя установки: явное, иначе единственный существующий ключ.

    То же правило, что в wizard/keys.sh и в Makefile. Несколько ключей без
    явного имени — отказ: угадывать, каким из них шифруется дерево, нельзя.
    """
    explicit = os.environ.get("FLEET", "").strip()
    if explicit:
        if not NAME_RE.match(explicit):
            raise ValueError(
                f"FLEET={explicit}: допустимы буквы, цифры, дефис и подчёркивание"
            )
        return explicit
    keys = sorted(path.stem for path in age_dir().glob("*.txt"))
    if not keys:
        raise ValueError(
            f"в {age_dir()} нет age-ключа — сначала wizard/keys.sh (шаг 1)"
        )
    if len(keys) > 1:
        raise ValueError(
            f"в {age_dir()} несколько ключей ({', '.join(keys)}) — назови: FLEET=<имя>"
        )
    return keys[0]


def use_fleet_age_key(fleet: str) -> str | None:
    """Поставить окружению ключ ЭТОЙ установки. Возвращает вытесненный путь.

    Правило то же, что в Makefile, и по той же причине. Рабочая станция может
    экспортировать SOPS_AGE_KEY_FILE с ключом другого проекта — тогда sops
    берёт чужой ключ и падает уже посреди прогона: «no master key was able to
    decrypt». Визард, который
    ставил переменную мягко (setdefault) и потому окружению проигрывал.

    Мягко здесь нельзя: какой ключ у этой установки, знает визард, а не среда.
    Если ключа нет на диске — не трогаем ничего, об этом скажет WARNINGS.
    """
    key = age_key_of(fleet)
    if not key.is_file():
        return None
    previous = os.environ.get("SOPS_AGE_KEY_FILE", "")
    os.environ["SOPS_AGE_KEY_FILE"] = str(key)
    # Тот же ключ для community.sops: ansible.cfg его не фиксирует, потому что
    # имя установки не константа.
    os.environ["ANSIBLE_SOPS_AGE_KEYFILE"] = str(key)
    if previous and Path(previous) != key:
        return previous
    return None


def first_contact_module():
    """first-contact.py — владелец правила двери; имя с дефисом не импортируется."""
    path = ROOT / "scripts" / "first-contact.py"
    spec = importlib.util.spec_from_file_location("vpn_fleet_first_contact", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def require_tty() -> None:
    if not sys.stdin.isatty():
        raise ValueError("шаг интерактивный: запускай его в терминале")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default:
            return default


def ask_yes(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer[0] in ("y", "д")


def ask_choice(prompt: str, allowed: tuple[str, ...], default: str) -> str:
    while True:
        answer = ask(f"{prompt} ({'|'.join(allowed)})", default)
        if answer in allowed:
            return answer
        print(f"  допустимо только: {', '.join(allowed)}")


CHOICE_PROMPT = "YOUR CHOSE"


def menu(title: str, items: list[str], default: int = 1) -> int:
    """Пронумерованное меню: печатает пункты и возвращает номер выбранного.

    Букв в ответах нет вовсе — ни `n`, ни `q`. «Back» и «Quit» такие же пункты
    списка, как остальные, и нумеруются вместе с ними: человеку не приходится
    помнить, где здесь цифра, а где буква. Отсюда же правило «всегда есть шаг
    назад»: пункт Back обязан быть в каждом меню, кроме самого верхнего, где
    его роль играет Quit.
    """
    print()
    print(title)
    for index, item in enumerate(items, 1):
        print(f"  {index}) {item}")
    print()
    while True:
        answer = ask(CHOICE_PROMPT, str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            return int(answer)
        print(f"  введи номер от 1 до {len(items)}")
