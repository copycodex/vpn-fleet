#!/usr/bin/env python3
"""Визард парка: один вход, который ведёт по шагам.

Порядок шагов знает этот файл, а не имена соседних. Своей памяти у визарда нет:
стадия каждого сервера выводится из файлов и состояния двери прямо сейчас.
Поэтому прерванный на любом месте прогон продолжается с того же места, а не с
начала, и ровно то же самое можно сделать руками — визард лишь зовёт то, что
человек позвал бы сам.

Экран собран из трёх частей, и порядок их не случаен:

  KEYS       чем шифруется дерево и чем открываются ноды. Печатается всегда:
             это первое, что надо узнать, садясь за чужую или забытую машину.
  WARNINGS   только когда что-то не так, и каждая строка несёт команду, которой
             это чинится. Когда всё на месте — блока нет вовсе, а не «ок».
  MENU       пронумерованные пункты. Буквенных ответов в визарде нет нигде, и
             из каждого меню, кроме верхнего, есть Back.

Стадии сервера и что из них следует:

  запись в карте есть      → первое касание ещё не сделано
  записи нет, ключ открывает → касание пройдено, дальше бутстрап
  известен host key нового порта → бутстрап пройден, дальше site
  ключ не читается         → дверь потеряна, дальше разбираться руками

Usage: wizard/wizard.py
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    ROOT,
    age_key_of,
    ask,
    ask_yes,
    first_contact_module,
    fleet_key_of,
    menu,
    private_dir,
    require_tty,
    resolve_fleet,
    say,
    set_step,
    use_fleet_age_key,
)
from hoster import choose as choose_hoster, load as load_hosters, door_of, print_route  # noqa: E402
from server import (  # noqa: E402
    fill_server,
    hoster_counts,
    map_has_node,
    new_server,
)
from deletion_state import marked_nodes, marker_path, read_marker, write_marker  # noqa: E402

WIZARD = Path(__file__).resolve().parent

STAGE_FIRST_CONTACT = "ждёт первого касания"
STAGE_NO_DOOR = "дверь не заведена"
STAGE_BOOTSTRAP = "касание пройдено, дальше бутстрап"
STAGE_SITE = "бутстрап пройден, дальше site"
STAGE_LOST = "дверь потеряна: ключ не читается"
STAGE_DELETING = "удаление не завершено"


def tree_state(private: Path) -> tuple[bool, list[str]]:
    """Готово ли частное дерево. Возвращает список недостающего."""
    wanted = [
        private / ".sops.yaml",
        private / "inventories" / "known_hosts",
        private / "inventories" / "first-entry.sops.yml",
        private / "inventories" / "group_vars" / "all" / "ssh_keys.yml",
    ]
    missing = [str(path) for path in wanted if not path.is_file()]
    return not missing, missing


def warnings_of(fleet: str, private: Path) -> list[tuple[str, str]]:
    """Что мешает работать и чем это чинится.

    Правило блока: сюда попадает только то, на что есть команда. Жалоба без
    команды — не предупреждение, а шум, и человеку с ней делать нечего.
    """
    found: list[tuple[str, str]] = []

    age = age_key_of(fleet)
    if not age.is_file():
        found.append((f"нет age-ключа {age}", "wizard/keys.sh"))
    key = fleet_key_of(fleet)
    if not key.is_file():
        found.append((f"нет ключа парка {key}", "wizard/keys.sh"))

    if shutil.which("sops") is None:
        found.append(("sops не найден в PATH", "wizard/deps.sh"))
    for name in ("yaml", "pexpect"):
        if importlib.util.find_spec(name) is None:
            found.append((f"нет python-зависимости {name}", "wizard/deps.sh"))

    _, missing = tree_state(private)
    for path in missing:
        found.append((f"нет файла {path}", "wizard/private_tree.py"))

    return found


def host_vars(module, private: Path, node: str) -> dict:
    inventory = private / "inventories" / "nodes" / f"{node}.yml"
    data, reasons = module._load_yaml(inventory, str(inventory))
    if reasons:
        return {}
    try:
        host = data["all"]["hosts"][node]
    except (KeyError, TypeError):
        return {}
    return host if isinstance(host, dict) else {}


def known_host_recorded(module, private: Path, host: dict, node: str,
                        role: str, port: int | None = None) -> bool:
    address = host.get("ansible_host")
    if port is None:
        port = host.get("bootstrap_ssh_port", host.get("ansible_port"))
    if not isinstance(address, str) or not isinstance(port, int):
        return False
    token = module._known_host_token(address, port)
    known = private / "inventories" / "known_hosts"
    return any(token in module._known_host_fields(line)[0]
               for line in module._owned_known_hosts(known, node, role))


def stage_of(module, private: Path, node: str) -> str:
    """Стадия выводится из фактов на диске, а не из чьей-то памяти."""
    if marker_path(private, node).exists():
        return STAGE_DELETING
    entry_file = private / "inventories" / "first-entry.sops.yml"
    if entry_file.is_file() and map_has_node(entry_file, node):
        return STAGE_FIRST_CONTACT

    host = host_vars(module, private, node)
    if known_host_recorded(module, private, host, node, "steady"):
        stage = STAGE_SITE
    else:
        known = private / "inventories" / "known_hosts"
        stage = (STAGE_BOOTSTRAP if module._owned_known_hosts(known, node, "door")
                 else STAGE_NO_DOOR)
    if stage == STAGE_NO_DOOR:
        return stage
    key = host.get("ansible_ssh_private_key_file")
    if isinstance(key, str):
        path = Path(os.path.expanduser(key))
        if not (path.is_file() and os.access(path, os.R_OK)):
            return STAGE_LOST
    return stage


def next_action(node: str, stage: str, host: dict, private: Path | None = None,
                module=None):
    """Что делать дальше: человеку — строкой, визарду — отложенной командой.

    Команда собирается не здесь, а по нажатию Run: у бутстрапа она требует
    ответа про дверь, и спрашивать его до того, как человек выбрал «запускать»,
    значило бы задавать вопрос впустую.
    """
    if stage == STAGE_FIRST_CONTACT:
        return [(
            f"make first-contact NODE={node}",
            lambda: ["make", "first-contact", f"NODE={node}"],
        )]

    if stage == STAGE_BOOTSTRAP:
        # Пользователь приходит из профиля, а порт — из принятой двери ноды.
        hosters, _ = load_hosters()
        door = door_of(hosters.get(str(host.get("node_location", "")), {}))
        user = door.get("user", "root")
        default_port = door.get("port", 22)
        module = module or first_contact_module()
        private = private or private_dir()
        known = private / "inventories" / "known_hosts"
        address = host.get("ansible_host")
        ports = []
        for line in module._owned_known_hosts(known, node, "door"):
            for token in module._known_host_fields(line)[0]:
                if token == address:
                    ports.append(22)
                elif isinstance(address, str) and (match := re.fullmatch(
                        r"\[" + re.escape(address) + r"\]:(\d+)", token)):
                    ports.append(int(match.group(1)))
        ports = list(dict.fromkeys(ports))
        default = f"{user}@{ports[0] if ports else default_port}"

        def build() -> list[str]:
            answer = default if len(ports) == 1 else ask(
                f"дверь, которую оставил first-contact для {node}", default)
            return [sys.executable, str(WIZARD / "bootstrap.py"), node, "--from", answer]

        shown = default if len(ports) == 1 else "<user>@<port>"
        return [(f"wizard/bootstrap.py {node} --from {shown}", build)]

    if stage == STAGE_SITE:
        return [
            (f"make site NODE={node}", lambda: ["make", "site", f"NODE={node}"]),
            (f"make export NODE={node}", lambda: ["make", "export", f"NODE={node}"]),
        ]

    return []


def run(command: list[str]) -> int:
    result = subprocess.run(command, cwd=str(ROOT), check=False)
    return result.returncode


EDITOR_REMOVE_ENTRY = """#!/usr/bin/env python3
from pathlib import Path
import sys
here = Path(__file__).parent
target = Path(sys.argv[1])
node = (here / "node").read_text(encoding="utf-8")
sys.path.insert(0, (here / "module_dir").read_text(encoding="utf-8"))
from first_contact_text import remove_entry
target.write_text(remove_entry(target.read_text(encoding="utf-8"), node), encoding="utf-8")
"""


def delete_server(private: Path, node: str) -> str | None:
    """Finish a local deletion, retaining its marker after a failed write."""
    try:
        return _delete_server(private, node)
    except OSError as error:
        return f"локальный шаг удаления не выполнен: {type(error).__name__}"


def _delete_server(private: Path, node: str) -> str | None:
    """Remove only local records owned by node. Return a field name on failure."""
    from secrets_lock import secrets_lock

    inventory = private / "inventories" / "nodes" / f"{node}.yml"
    try:
        marker = read_marker(private, node)
    except (OSError, ValueError):
        return "отметка удаления не разбирается"
    if not inventory.is_file() and marker is None:
        return "инвентарь ноды отсутствует"
    if marker is None:
        try:
            data = yaml.safe_load(inventory.read_text(encoding="utf-8"))
            address = data["all"]["hosts"][node]["ansible_host"]
            if not isinstance(address, str):
                return "ansible_host не задан"
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
            return "инвентарь ноды не разбирается"

    config = private / ".sops.yaml"
    secrets_file = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    prefix = "node_" + node.replace("-", "_") + "_"
    other_nodes = [path.stem for path in inventory.parent.glob("*.yml") if path != inventory]
    protected = tuple("node_" + name.replace("-", "_") + "_" for name in other_nodes)
    if any(name == node or name.replace("-", "_") == node.replace("-", "_")
           for name in other_nodes):
        return "префикс секретов совпадает с другой нодой"

    if marker is None:
        # All read-only preconditions precede the first write. A crash at any
        # later step keeps this node visibly unavailable for creation or fill.
        write_marker(private, node, address)

    # clients/<node>-<client>.*: чужая нода с именем «<node>-…» свои файлы
    # называет тем же префиксом, поэтому их исключаем явно.
    foreign = tuple(f"{name}-" for name in other_nodes if name.startswith(f"{node}-"))
    for path in (private.parent / "clients").glob(f"{node}-*"):
        if path.name.startswith(foreign):
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    contact = first_contact_module()
    known = private / "inventories" / "known_hosts"
    if known.is_file():
        lines = known.read_text(encoding="utf-8").splitlines()
        retained = [line for line in lines if not (
            (owner := contact._known_host_owner(line)) and owner[0] == node)]
        if len(retained) != len(lines):
            contact._write_known_host_lines(known, retained)

    entry_file = private / "inventories" / "first-entry.sops.yml"
    if entry_file.is_file() and map_has_node(entry_file, node):
        reason = first_contact_module()._run_sops_editor(
            config, entry_file, EDITOR_REMOVE_ENTRY,
            {"node": node, "module_dir": str(ROOT / "scripts")},
        )
        if reason:
            return "first-entry.sops.yml не обновлён"

    inventory.unlink(missing_ok=True)
    if secrets_file.exists():
        with secrets_lock(secrets_file):
            result = subprocess.run(["sops", "--decrypt", str(secrets_file)],
                                    capture_output=True, text=True, check=False)
            if result.returncode:
                return "secrets.sops.yml не расшифрован"
            try:
                values = yaml.safe_load(result.stdout)
            except yaml.YAMLError:
                return "secrets.sops.yml не разбирается"
            if not isinstance(values, dict):
                return "secrets.sops.yml не является словарём"
            removed = False
            for key in list(values):
                if isinstance(key, str) and key.startswith(prefix) and not key.startswith(protected):
                    del values[key]
                    removed = True
            if not removed:
                marker_path(private, node).unlink()
                return None
            if values:
                with tempfile.TemporaryDirectory(prefix="vpn-fleet-delete-") as directory:
                    plain = Path(directory) / "plain.yml"
                    plain.write_text(yaml.safe_dump(values, allow_unicode=True), encoding="utf-8")
                    plain.chmod(0o600)
                    handle, name = tempfile.mkstemp(dir=secrets_file.parent,
                                                    prefix=f".{secrets_file.name}.")
                    os.close(handle)
                    pending = Path(name)
                    try:
                        encrypted = subprocess.run(
                            ["sops", "--config", str(config), "--encrypt", "--input-type", "yaml",
                             "--output-type", "yaml", "--filename-override", str(secrets_file),
                             "--output", str(pending), str(plain)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                        )
                        if encrypted.returncode:
                            return "secrets.sops.yml не зашифрован"
                        pending.chmod(0o600)
                        os.replace(pending, secrets_file)
                    finally:
                        pending.unlink(missing_ok=True)
            else:
                secrets_file.unlink()

    marker_path(private, node).unlink()
    return None


def delete_plan(private: Path, node: str) -> list[str]:
    """Describe the local files before asking for confirmation."""
    inventory = private / "inventories" / "nodes" / f"{node}.yml"
    secrets_file = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    entry_file = private / "inventories" / "first-entry.sops.yml"
    others = [path.stem for path in inventory.parent.glob("*.yml") if path != inventory]
    foreign = tuple(f"{name}-" for name in others if name.startswith(f"{node}-"))
    clients = sorted(path for path in (private.parent / "clients").glob(f"{node}-*")
                     if not path.name.startswith(foreign))
    lines = [
        f"отметка удаления: {marker_path(private, node)} (имя и адрес)",
        f"клиенты: {len(clients)} файл(ов) в {private.parent / 'clients'}",
        "known_hosts: удаляются только строки с метками этой ноды",
        f"первый вход: запись {node} в {entry_file}",
        f"инвентарь: {inventory}",
        f"секреты: все поля node_{node.replace('-', '_')}_* в {secrets_file}",
        "отметка удаления: снять после очистки остатков",
    ]
    contact = first_contact_module()
    try:
        marker = read_marker(private, node)
    except (OSError, ValueError):
        lines.append("отметка удаления не разбирается — исправить руками")
        return lines
    host = host_vars(contact, private, node) if marker is None else {}
    address = marker["address"] if marker is not None else host.get("ansible_host")
    known = private / "inventories" / "known_hosts"
    if isinstance(address, str) and known.is_file():
        count = sum(1 for line in known.read_text(encoding="utf-8").splitlines()
                    if (fields := contact._known_host_fields(line))
                    and contact._known_host_owner(line) is None
                    and any(token == address or token.startswith(f"[{address}]:")
                            for token in fields[0]))
        if count:
            lines.append(f"known_hosts: {count} строк без владельца на адресе {address} "
                         "остаются — удалить руками, если они этой ноды")
    return lines


def create_server(module, private: Path, fleet: str) -> str:
    """Пункт Create server: сначала хостер, потом сервер у него.

    Возвращает 'quit', если человек вышел совсем, иначе 'back' — в MENU.
    """
    hosters, reason = load_hosters()
    if reason:
        say(f"каталог хостеров недоступен: {reason}")
        return "back"

    while True:
        action, name, profile = choose_hoster(hosters, hoster_counts(private))
        if action == "quit":
            return "quit"
        if action == "back":
            return "back"

        print_route(name, profile)
        picked = menu(
            f"HOSTER {name.upper()}:",
            ["Create server here", "Back", "Quit"],
        )
        if picked == 3:
            return "quit"
        if picked == 2:
            continue
        new_server(module, private, fleet, (name, profile))
        return "back"


def server_menu(module, private: Path, node: str) -> str:
    """Один сервер: его стадия, следующий шаг и дозаполнение."""
    stage = stage_of(module, private, node)
    host = {} if stage == STAGE_DELETING else host_vars(module, private, node)
    actions = next_action(node, stage, host, private, module)

    print()
    print(f"SERVER {node}:")
    print(f"  стадия      {stage}")
    hint = ("повтори Delete server" if stage == STAGE_DELETING else
            "Fill in missing data" if stage == STAGE_NO_DOOR else
            "руками: ключ из инвентаря не читается")
    print(f"  следующий   {', '.join(action[0] for action in actions) if actions else hint}")

    items = [f"Run: {label}" for label, _ in actions]
    if stage != STAGE_DELETING:
        items.append("Fill in missing data")
    items += ["Delete server", "Back", "Quit"]
    picked = menu("SERVER MENU:", items)

    offset = len(actions)
    if picked <= offset:
        run(actions[picked - 1][1]())
        return "back"
    if stage != STAGE_DELETING and picked == offset + 1:
        fill_server(module, private, node)
        return "back"
    delete_index = offset + (1 if stage == STAGE_DELETING else 2)
    if picked == delete_index:
        print("DELETE PLAN:")
        for line in delete_plan(private, node):
            print(f"  {line}")
        if ask_yes(f"удалить сервер {node}?", default=False):
            reason = delete_server(private, node)
            say(f"удаление: {reason or 'готово'}")
        else:
            say("ничего не удалено")
        return "back"
    if picked == delete_index + 1:
        return "back"
    return "quit"


def servers_menu(module, private: Path) -> str:
    while True:
        nodes = sorted({path.stem for path in (private / "inventories" / "nodes").glob("*.yml")}
                       | marked_nodes(private))
        if not nodes:
            say("серверов пока нет — заводи первый через Create server")
            return "back"

        items = [f"{node} — {stage_of(module, private, node)}" for node in nodes]
        items += ["Back", "Quit"]
        picked = menu("SERVERS:", items)
        if picked == len(nodes) + 1:
            return "back"
        if picked == len(nodes) + 2:
            return "quit"

        if server_menu(module, private, nodes[picked - 1]) == "quit":
            return "quit"


def main() -> int:
    set_step("wizard")
    try:
        require_tty()
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    try:
        fleet = resolve_fleet()
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        print("Начни с шага 1: wizard/keys.sh", file=sys.stderr)
        return 1

    # До любого sops: ключ установки побеждает окружение. Иначе переменная
    # рабочей станции уводит sops на чужой ключ, и падает это не здесь, а
    # посреди записи в карту первого входа.
    displaced = use_fleet_age_key(fleet)

    module = first_contact_module()
    private = private_dir()

    print()
    print("KEYS:")
    print(f"  установка  {fleet}")
    print(f"  age        {age_key_of(fleet)}")
    print(f"  ssh        {fleet_key_of(fleet)}")
    if displaced:
        print(f"  вытеснено  SOPS_AGE_KEY_FILE={displaced} (ключ другого проекта)")

    problems = warnings_of(fleet, private)
    if problems:
        print()
        print("WARNINGS:")
        for index, (what, fix) in enumerate(problems, 1):
            print(f"  {index}) {what}")
            print(f"     поправить: {fix}")

    while True:
        picked = menu("MENU:", ["Create server", "Servers", "Quit"])
        if picked == 3:
            return 0

        # Дерево и ключи проверяются заново перед каждым заходом: человек мог
        # поправить названное в WARNINGS в соседнем окне, не выходя отсюда.
        problems = warnings_of(fleet, private)
        if problems:
            say("сначала поправь то, что названо в WARNINGS:")
            for what, fix in problems:
                print(f"  {what} — {fix}")
            continue

        outcome = (
            create_server(module, private, fleet)
            if picked == 1
            else servers_menu(module, private)
        )
        if outcome == "quit":
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
