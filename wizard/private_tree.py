#!/usr/bin/env python3
"""Шаг 2 визарда: частное дерево.

Создаёт то, чего нет, и дозаполняет то, что пусто. Ничего не перезаписывает:
чужой получатель в .sops.yaml, чужой ключ в списке админов и уже заведённая
карта остаются как есть — о них шаг говорит, а не правит их молча.

Что должно получиться:

  private/.sops.yaml                                правила шифрования с нашим получателем
  private/inventories/known_hosts                   пустой, ключи примет первое касание
  private/inventories/nodes/                        каталог инвентарей
  private/inventories/first-entry.sops.yml          пустая карта: first_entry: {}
  private/inventories/group_vars/all/ssh_keys.yml   наш публичный ключ

Файла секретов здесь нет намеренно: sops не шифрует документ без данных, а
пустой `{}` ломает дозапись строк. Он заводится вместе с первым сервером —
шаг 4–9, wizard/server.py.

Usage: wizard/private_tree.py
       FLEET=<имя> wizard/private_tree.py
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    ROOT,
    age_key_of,
    ask_yes,
    fail,
    fleet_key_of,
    private_dir,
    resolve_fleet,
    say,
    set_step,
    warn,
)

PLACEHOLDER = "age1REPLACE_WITH_YOUR_OWN_RECIPIENT"
RECIPIENT_RE = re.compile(r"age1[0-9a-z]{50,}")

KNOWN_HOSTS_HEADER = """# Host keys нод, принятые автоматически по TOFU. ЧАСТНЫЙ файл.
#
# Пока здесь нет ни одного ключа — это рабочее состояние, а не поломка:
# scripts/check-private-inputs.sh на пустом файле обязан краснеть, и красным он
# тут прав. Ключ ноды кладёт сюда make first-contact перед первым SSH.
#
# Формы записи:
#   <host> ssh-ed25519 ...            порт 22
#   [<host>]:<port> ssh-ed25519 ...   любой другой порт
# Скобочная форма обязательна для нестандартного порта, иначе
# StrictHostKeyChecking=yes роняет всё сразу после бутстрапа.
#
# Переустановили ноду — удали её строки отсюда: они описывают уже несуществующий
# хост, а гейт на них остался бы зелёным.
"""

SSH_KEYS_HEADER = """---
# Публичные SSH-ключи администраторов парка. Единственный источник правды:
# руками на сервере ключи не добавляются.
#
# Роль bootstrap ставит пользователю devops РОВНО этот список (exclusive) и
# следом выключает root и парольный вход. Список без твоего ключа — это дверь,
# которая закрывается снаружи.
ssh_authorized_keys_devops:
"""


def recipient_of(fleet: str) -> tuple[str | None, str | None]:
    """Публичная часть age-ключа. Значение не печатается: это идентификатор."""
    key = age_key_of(fleet)
    if not key.is_file():
        return None, f"нет age-ключа {key} — сначала wizard/keys.sh (шаг 1)"
    result = subprocess.run(
        ["age-keygen", "-y", str(key)], text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        return None, f"age-keygen не прочитал {key}"
    value = result.stdout.strip()
    if not RECIPIENT_RE.fullmatch(value):
        return None, f"{key}: публичная часть не похожа на age-получателя"
    return value, None


def public_key_of(fleet: str) -> tuple[str | None, str | None]:
    path = Path(str(fleet_key_of(fleet)) + ".pub")
    if not path.is_file():
        return None, f"нет публичной части ключа {path} — сначала wizard/keys.sh"
    fields = path.read_text(encoding="utf-8").split()
    if len(fields) < 2:
        return None, f"{path}: не похоже на публичный ключ"
    return " ".join(fields[:2]), None


def ensure_sops_config(private: Path, recipient: str) -> str | None:
    target = private / ".sops.yaml"
    sample = ROOT / ".sops.yaml"

    if not target.exists():
        if not sample.is_file():
            return f"нет публичного образца {sample}"
        text = sample.read_text(encoding="utf-8").replace(PLACEHOLDER, recipient)
        text = text.replace(
            "# sops creation rules — ОБРАЗЕЦ. Настоящий файл лежит в private/.sops.yaml и\n"
            "# несёт настоящего получателя age; здесь плейсхолдер, и так и должно быть.",
            "# Правила шифрования частного дерева. Получатель — публичная часть\n"
            "# age-ключа этой установки. Приватная часть в репозиторий не попадает.",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        say(f"создан {target} с нашим получателем")
        return None

    text = target.read_text(encoding="utf-8")
    if recipient in text:
        say(f"{target}: получатель уже наш")
        return None
    if PLACEHOLDER in text:
        say(f"{target}: стоит плейсхолдер вместо получателя")
        if not ask_yes("  подставить наш age-получатель?"):
            warn("оставил как есть — гейт будет краснеть на плейсхолдере")
            return None
        target.write_text(text.replace(PLACEHOLDER, recipient), encoding="utf-8")
        say(f"{target}: получатель подставлен")
        return None

    warn(
        f"{target} уже несёт ЧУЖОГО получателя — не трогаю. Это файл другой "
        "установки: либо работай с её ключом, либо убери файл осознанно."
    )
    return None


def ensure_known_hosts(private: Path) -> None:
    target = private / "inventories" / "known_hosts"
    if target.exists():
        keys = [
            line
            for line in target.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        say(f"{target}: есть, ключей в нём {len(keys)}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(KNOWN_HOSTS_HEADER, encoding="utf-8")
    target.chmod(0o600)
    say(f"создан {target} — пустой, ключи примет первое касание")


def ensure_ssh_keys(private: Path, fleet: str, public_key: str) -> None:
    target = private / "inventories" / "group_vars" / "all" / "ssh_keys.yml"
    entry = f"  - name: {fleet}\n    pubkey: '{public_key}'\n"

    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SSH_KEYS_HEADER + entry, encoding="utf-8")
        say(f"создан {target} с нашим публичным ключом")
        return

    text = target.read_text(encoding="utf-8")
    if public_key in text:
        say(f"{target}: наш ключ уже в списке")
        return
    say(f"{target}: нашего ключа в списке нет")
    if not ask_yes("  дописать его?"):
        warn("оставил как есть — бутстрап заведёт devops без нашего ключа")
        return
    if not text.endswith("\n"):
        text += "\n"
    target.write_text(text + entry, encoding="utf-8")
    say(f"{target}: ключ дописан")


def ensure_first_entry(private: Path, config: Path) -> str | None:
    target = private / "inventories" / "first-entry.sops.yml"
    if target.exists():
        say(f"{target}: есть")
        return None

    sample = ROOT / "inventories" / "first-entry.sops.yml.example"
    if not sample.is_file():
        return f"нет публичного образца {sample}"
    text = sample.read_text(encoding="utf-8").replace(
        "# Первый вход в ноды — ОБРАЗЕЦ. Настоящий файл: "
        "private/inventories/first-entry.sops.yml,\n"
        "# зашифрован sops + age. Шифруется ровно поле password.",
        "# Первый вход в ноды этого парка. ЧАСТНЫЙ файл: sops + age, шифруется\n"
        "# ровно поле password.",
        1,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

    from secrets_lock import secrets_lock

    with secrets_lock(target):
        result = subprocess.run(
            ["sops", "--config", str(config), "-e", "-i", str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        return f"sops не зашифровал {target}; файл не оставлен"
    say(f"создана пустая карта первого входа: {target}")
    return None


def main() -> int:
    set_step("private-tree")
    try:
        fleet = resolve_fleet()
    except ValueError as error:
        return fail(str(error))
    say(f"установка: {fleet}")

    recipient, reason = recipient_of(fleet)
    if reason or recipient is None:
        return fail(reason or "получатель не прочитан")
    public_key, reason = public_key_of(fleet)
    if reason or public_key is None:
        return fail(reason or "публичный ключ не прочитан")

    private = private_dir()
    reason = ensure_sops_config(private, recipient)
    if reason:
        return fail(reason)

    config = private / ".sops.yaml"
    (private / "inventories" / "nodes").mkdir(parents=True, exist_ok=True)
    ensure_known_hosts(private)
    ensure_ssh_keys(private, fleet, public_key)
    reason = ensure_first_entry(private, config)
    if reason:
        return fail(reason)

    secrets = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    if secrets.exists():
        say(f"{secrets}: есть")
    else:
        say("файла секретов пока нет — он заведётся вместе с первым сервером")

    print()
    print("Шаг 2 готов. Частное дерево:")
    for path in (
        config,
        private / "inventories" / "known_hosts",
        private / "inventories" / "first-entry.sops.yml",
        private / "inventories" / "group_vars" / "all" / "ssh_keys.yml",
    ):
        print(f"  {path}")
    print()
    print("Дальше — сервер: make new-server (шаги 4–9).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
