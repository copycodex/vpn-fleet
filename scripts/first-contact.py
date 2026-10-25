#!/usr/bin/env python3
"""Inspect or open a node's first-entry door.

Without --apply this command deliberately performs no network operations.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import string
import subprocess
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secrets_lock import secrets_lock

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

try:
    import pexpect
except ImportError:
    pexpect = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent.parent
ENTRY_FIELDS = {"user", "port", "method", "password"}
ENC_RE = re.compile(r"^ENC\[.+\]$")
PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*()-_=+[]{}:,.?"


class UniqueKeyLoader(yaml.SafeLoader if yaml is not None else object):
    """Safe YAML loader which also rejects ambiguous duplicate keys."""


def _construct_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.YAMLError("duplicate key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


if yaml is not None:
    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
    )


@dataclass(frozen=True)
class DoorResult:
    state: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class NetworkDoor:
    node_name: str
    host: str
    user: str
    port: int
    method: str | None
    entry_found: bool
    key_path: Path


def _load_yaml(path: Path, label: str) -> tuple[Any | None, list[str]]:
    if yaml is None:
        return None, [
            "не хватает зависимости PyYAML: установи зависимости Python "
            "из requirements-ci.txt"
        ]
    try:
        with path.open(encoding="utf-8") as stream:
            return yaml.load(stream, Loader=UniqueKeyLoader), []
    except Exception:  # noqa: BLE001 - the diagnostic must redact parser details
        # Parser exceptions can contain the offending scalar.  Never echo them:
        # the scalar may be the provider password.
        return None, [f"{label}: невалидный YAML или файл не читается"]


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _inventory_key(node: str, private_dir: Path) -> tuple[str | None, bool, list[str]]:
    inventory = private_dir / "inventories" / "nodes" / f"{node}.yml"
    label = _display(inventory)
    if not inventory.is_file():
        return None, False, [f"нет инвентаря {label}"]

    data, reasons = _load_yaml(inventory, label)
    if reasons:
        return None, False, reasons
    if not isinstance(data, dict) or not isinstance(data.get("all"), dict):
        return None, False, [f"{label}: ожидается Ansible YAML mapping all.hosts"]
    hosts = data["all"].get("hosts")
    if not isinstance(hosts, dict):
        return None, False, [f"{label}: ожидается Ansible YAML mapping all.hosts"]
    host = hosts.get(node)
    if not isinstance(host, dict):
        return None, False, [f"{label}: в all.hosts нет mapping для {node}"]

    node_name = host.get("node_name")
    if not isinstance(node_name, str) or not node_name.strip():
        return None, False, [f"{label}: нет node_name"]
    node_name = node_name.strip()

    declared = host.get("ansible_ssh_private_key_file")
    if declared is None or declared == "":
        return node_name, False, []
    if not isinstance(declared, str):
        return node_name, False, [
            f"{label}: поле ansible_ssh_private_key_file должно быть строкой"
        ]
    key_path = Path(os.path.expanduser(declared.strip()))
    readable = key_path.is_file() and os.access(key_path, os.R_OK)
    return node_name, readable, []


def _entry_for_node(
    node_name: str, private_dir: Path
) -> tuple[bool, dict[str, Any] | None, str, list[str]]:
    entry_file = private_dir / "inventories" / "first-entry.sops.yml"
    label = _display(entry_file)
    if not entry_file.exists():
        return False, None, label, []
    if not entry_file.is_file():
        return False, None, label, [f"{label}: карта первого входа не является файлом"]

    data, reasons = _load_yaml(entry_file, label)
    if reasons:
        return False, None, label, reasons
    if not isinstance(data, dict) or "first_entry" not in data:
        return False, None, label, [f"{label}: поле first_entry должно быть YAML mapping"]

    entries = data["first_entry"]
    # Both spellings are accepted for an exhausted map.  Chunk 4 should write
    # the explicit `first_entry: {}` form after deleting the final entry.
    if entries is None:
        entries = {}
    if not isinstance(entries, dict):
        return False, None, label, [f"{label}: поле first_entry должно быть YAML mapping"]

    if node_name not in entries:
        return False, None, label, []
    entry = entries[node_name]
    if not isinstance(entry, dict):
        return True, None, label, [
            f"{label}: {node_name} — запись должна быть YAML mapping, "
            "пустая запись не равна отсутствующей"
        ]

    unknown = set(entry) - ENTRY_FIELDS
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        return True, None, label, [
            f"{label}: {node_name} — неизвестные поля схемы: {fields}"
        ]
    return True, entry, label, []


def inspect_door(node: str, private_dir: Path) -> DoorResult:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", node):
        return DoorResult(None, ("NODE: недопустимое имя инвентаря",))
    node_name, has_key, reasons = _inventory_key(node, private_dir)
    if reasons or node_name is None:
        return DoorResult(None, tuple(reasons))

    entry_found, entry, entry_label, reasons = _entry_for_node(node_name, private_dir)
    if reasons:
        return DoorResult(None, tuple(reasons))

    inventory_label = _display(
        private_dir / "inventories" / "nodes" / f"{node}.yml"
    )
    if not entry_found:
        if has_key:
            return DoorResult(
                "читаемый ключ парка объявлен (claim; вход по сети не проверялся)", ()
            )
        return DoorResult(
            None,
            (
                f"{inventory_label}: {node_name} нечем открыть — нет ни записи "
                f"в first_entry ({entry_label}), ни читаемого ключа: "
                "ansible_ssh_private_key_file",
            ),
        )

    # A found entry is guaranteed to be a mapping by _entry_for_node.
    assert entry is not None
    validation: list[str] = []
    user = entry.get("user")
    port = entry.get("port")
    method = entry.get("method")

    if not isinstance(user, str) or not user.strip():
        validation.append(
            f"{entry_label}: {node_name} — не заполнено поле user: "
            "неизвестно, под кем открывается дверь"
        )
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        validation.append(
            f"{entry_label}: {node_name} — не заполнено или невалидно поле port"
        )

    if method in (None, ""):
        validation.append(
            f"{entry_label}: {node_name} — не заполнено поле method "
            "(password|key): неизвестно, чем открывается дверь"
        )
    elif method == "password":
        password = entry.get("password")
        if password in (None, ""):
            validation.append(
                f"{entry_label}: {node_name} — method: password, а поле password "
                f"пусто; владелец вписывает его: make secrets-edit FILE={entry_label}"
            )
        elif not isinstance(password, str) or not ENC_RE.fullmatch(password):
            validation.append(
                f"{entry_label}: {node_name} — поле password лежит открытым "
                "текстом, а обязано быть шифротекстом sops"
            )
    elif method == "key":
        if "password" in entry:
            validation.append(
                f"{entry_label}: {node_name} — method: key, поле password лишнее"
            )
        if not has_key:
            validation.append(
                f"{entry_label}: {node_name} нечем открыть — method: key, но "
                f"ansible_ssh_private_key_file в {inventory_label} не читается "
                "с этой машины"
            )
    else:
        validation.append(
            f"{entry_label}: {node_name} — поле method несёт неизвестное "
            "значение (допустимо password|key)"
        )

    if validation:
        return DoorResult(None, tuple(validation))
    if method == "password":
        return DoorResult("пароль из зашифрованной карты первого входа", ())
    return DoorResult(
        "ключ парка объявлен и читается (claim; вход по сети не проверялся)", ()
    )


def _network_door(node: str, private_dir: Path) -> tuple[NetworkDoor | None, str | None]:
    inventory = private_dir / "inventories" / "nodes" / f"{node}.yml"
    data, reasons = _load_yaml(inventory, _display(inventory))
    if reasons:
        return None, reasons[0]
    try:
        host_vars = data["all"]["hosts"][node]
        node_name = host_vars["node_name"]
        host = host_vars["ansible_host"]
        key_value = host_vars["ansible_ssh_private_key_file"]
    except (KeyError, TypeError):
        return None, f"{_display(inventory)}: не заполнены поля сетевого входа"
    if not all(isinstance(value, str) and value.strip() for value in (node_name, host, key_value)):
        return None, f"{_display(inventory)}: невалидны поля сетевого входа"
    key_path = Path(os.path.expanduser(key_value.strip()))
    if not key_path.is_file() or not os.access(key_path, os.R_OK):
        return None, f"{_display(inventory)}: ansible_ssh_private_key_file не читается"

    found, entry, entry_label, entry_reasons = _entry_for_node(node_name.strip(), private_dir)
    if entry_reasons:
        return None, entry_reasons[0]
    if found:
        assert entry is not None
        user = entry.get("user")
        port = entry.get("port")
        method = entry.get("method")
    else:
        user = host_vars.get("ansible_user")
        port = host_vars.get("ansible_port")
        method = None
    if not isinstance(user, str) or not user.strip():
        return None, f"{entry_label}: не заполнено поле user для сетевого входа"
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None, f"{entry_label}: невалидно поле port для сетевого входа"
    return NetworkDoor(
        node_name=node_name.strip(),
        host=host.strip(),
        user=user.strip(),
        port=port,
        method=method if isinstance(method, str) else None,
        entry_found=found,
        key_path=key_path,
    ), None


def _known_host_token(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _known_host_label(node: str, role: str) -> str:
    return f"vpn-fleet:{node}:{role}"


def _known_host_fields(line: str) -> tuple[list[str], str, str, str] | None:
    """Return tokens, key type, key body and exact comment of a valid row."""
    fields = line.split()
    if not fields or fields[0].startswith("#"):
        return None
    offset = 1 if fields[0].startswith("@") else 0
    if len(fields) < offset + 3:
        return None
    return fields[offset].split(","), fields[offset + 1], fields[offset + 2], " ".join(fields[offset + 3:])


def _known_host_owner(line: str) -> tuple[str, str] | None:
    """Return the node and role only for an exact fleet owner comment."""
    fields = _known_host_fields(line)
    if fields is None:
        return None
    match = re.fullmatch(r"vpn-fleet:([A-Za-z0-9_-]+):(door|steady)", fields[3])
    return (match.group(1), match.group(2)) if match else None


def _owned_known_hosts(known_hosts: Path, node: str, role: str | None = None) -> list[str]:
    try:
        lines = known_hosts.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [line for line in lines if (owner := _known_host_owner(line))
            and owner[0] == node and (role is None or owner[1] == role)]


def _label_known_host_line(line: str, node: str, role: str) -> str:
    fields = _known_host_fields(line)
    if fields is None:
        raise ValueError("invalid known_hosts row")
    offset = 1 if line.split()[0].startswith("@") else 0
    return " ".join(line.split()[:offset + 3]) + " " + _known_host_label(node, role)


def _write_known_host_lines(known_hosts: Path, lines: list[str]) -> None:
    """Write a complete, already validated known_hosts image."""
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{known_hosts.name}.", dir=known_hosts.parent)
    pending = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write("\n".join(lines) + ("\n" if lines else ""))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, known_hosts)
    finally:
        pending.unlink(missing_ok=True)


def _ensure_known_host(door: NetworkDoor, known_hosts: Path) -> str | None:
    token = _known_host_token(door.host, door.port)
    if any(token in _known_host_fields(line)[0]
           for line in _owned_known_hosts(known_hosts, door.node_name, "door")):
        return None

    command = ["ssh-keyscan", "-T", "10"]
    if door.port != 22:
        command.extend(["-p", str(door.port)])
    command.append(door.host)
    scan = subprocess.run(command, text=True, capture_output=True, check=False)
    lines = [line for line in scan.stdout.splitlines()
             if (fields := _known_host_fields(line)) and token in fields[0]]
    if scan.returncode != 0 or not lines:
        return "ssh-keyscan не получил host key; known_hosts не изменён"
    existing = known_hosts.read_text(encoding="utf-8").splitlines() if known_hosts.exists() else []
    foreign = [line for line in existing
               if (fields := _known_host_fields(line)) and token in fields[0]]
    scanned_keys = {(_known_host_fields(line)[1], _known_host_fields(line)[2]) for line in lines}
    for line in foreign:
        fields = _known_host_fields(line)
        if (fields[1], fields[2]) not in scanned_keys:
            parsed_owner = _known_host_owner(line)
            owner = parsed_owner[0] if parsed_owner else "неизвестен"
            return f"на этом адресе и порту принят другой ключ (владелец: {owner}); known_hosts не изменён"
    if foreign and {(_known_host_fields(line)[1], _known_host_fields(line)[2]) for line in foreign} != scanned_keys:
        return "на этом адресе и порту принят другой ключ (владелец: неизвестен); known_hosts не изменён"
    _write_known_host_lines(known_hosts, existing + [_label_known_host_line(line, door.node_name, "door") for line in lines])
    print("first-contact: ключ принят без сверки с консолью (TOFU)")
    return None


def _ssh_base(door: NetworkDoor, known_hosts: Path) -> list[str]:
    return [
        "ssh",
        "-o", "IdentitiesOnly=yes",
        "-o", f"IdentityFile={door.key_path}",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=15",
        "-p", str(door.port),
    ]


def _key_opens(door: NetworkDoor, known_hosts: Path) -> bool:
    command = _ssh_base(door, known_hosts)
    command[1:1] = ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey"]
    command.extend([f"{door.user}@{door.host}", "true"])
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _sops_extract(config: Path, path: Path, selector: str) -> tuple[str | None, str | None]:
    result = subprocess.run(
        ["sops", "--config", str(config), "--decrypt", "--extract", selector, str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None, f"{_display(path)}: sops не смог расшифровать нужное поле"
    return result.stdout.rstrip("\n"), None


def _new_password(length: int = 32) -> str:
    chosen = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*()-_=+[]{}:,.?"),
    ]
    chosen.extend(secrets.choice(PASSWORD_ALPHABET) for _ in range(length - len(chosen)))
    secrets.SystemRandom().shuffle(chosen)
    return "".join(chosen)


def _spawn(command: list[str]):
    assert pexpect is not None
    return pexpect.spawn(command[0], command[1:], encoding="utf-8", timeout=25)


def _said(child: Any, *secrets: str) -> str:
    """Хвост того, что сказала нода, — для сообщения об отказе.

    Без него отказ диалога выглядит как «завершился до запроса нового пароля»
    без единой подсказки, чем именно он завершился, и разбирать нечего. Пароли
    вычищаются: при выключенном эхе их в потоке нет, но полагаться на это,
    печатая текст в консоль, нельзя.
    """
    text = (child.before or "") + (child.after if isinstance(child.after, str) else "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<пароль>")
    lines = [line.strip() for line in text.replace("\r", "\n").splitlines()]
    lines = [line for line in lines if line and not line.startswith("passwd\x00")]
    tail = " / ".join(lines[-3:])
    return f" — нода сказала: {tail}" if tail else ""


def _login_password(child: Any, password: str) -> str | None:
    patterns = [
        re.compile(r"(?i)(?:^|\n).*password:\s*$"),
        re.compile(r"(?i)REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed"),
        re.compile(r"(?i)Permission denied"),
        pexpect.EOF,
        pexpect.TIMEOUT,
    ]
    matched = child.expect(patterns)
    if matched == 0:
        child.sendline(password)
        return None
    if matched == 1:
        return "host key не совпал с known_hosts; пароль не передан"
    if matched == 2:
        return "сервер отказал во входе до передачи пароля"
    if matched == 3:
        closed = (child.before or "").lower()
        if "host identification has changed" in closed or "host key verification failed" in closed:
            return "host key не совпал с known_hosts; пароль не передан"
        if "permission denied" in closed:
            return "сервер отказал во входе; пароль не принят"
        return "ssh закрыл соединение до запроса пароля"
    return "ssh не запросил пароль вовремя"


# Сколько ждать ответа ноды, прежде чем решить, что строка не засчитана.
ANSWER_WAIT = 8


def _answer(child: Any, text: str, patterns: list) -> int:
    """Ответ УДАЛЁННОМУ диалогу PAM: сначала \n, при молчании — дожать \r.

    Каким символом удалённый терминал заканчивает строку, зависит от образа.
    Одни терминалы засчитывают только \r, а \n оставляют в буфере до таймаута;
    другие сразу принимают \n и молчат после \r. Молчание длится без
    единого байта, поэтому отличить «не засчитал» от «думает» можно только
    ожиданием.

    Если строка не засчитана, набранное лежит в буфере ноды, и одиночный \r
    её закрывает. Засчитанную строку он не трогает: \r шлётся, только пока
    нода молчит. Худший исход — неверный пароль, и PAM откажет, ничего не
    сменив.
    """
    child.send(text + "\n")
    matched = child.expect(patterns, timeout=ANSWER_WAIT)
    if patterns[matched] is pexpect.TIMEOUT:
        child.send("\r")
        matched = child.expect(patterns)
    return matched


def _change_password(
    door: NetworkDoor, known_hosts: Path, current: str, new: str, *, force_tty: bool = True
) -> str | None:
    if not force_tty:
        return "PTY обязателен для смены пароля; соединение не начато"
    command = _ssh_base(door, known_hosts)
    command.extend([
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        "-o", "NumberOfPasswordPrompts=1",
    ])
    command.append("-tt" if force_tty else "-T")
    command.extend([f"{door.user}@{door.host}", "passwd"])
    child = _spawn(command)
    reason = _login_password(child, current)
    if reason:
        child.close(force=True)
        return reason

    prompt_patterns = [
        re.compile(r"(?i)(?:current|old)(?: UNIX)? password:\s*$"),
        re.compile(r"(?i)(?:new password|enter new UNIX password):\s*$"),
        re.compile(r"(?i)Permission denied|authentication failure|incorrect password"),
        re.compile(r"(?i)pseudo-terminal|not a tty|terminal is required"),
        pexpect.EOF,
        pexpect.TIMEOUT,
    ]
    matched = child.expect(prompt_patterns)
    if matched == 0:
        matched = _answer(child, current, prompt_patterns[1:])
        if matched == 1:
            said = _said(child, current, new)
            child.close(force=True)
            return "текущий пароль не принят; повторных попыток не было" + said
        if matched == 2:
            child.close(force=True)
            return "PTY обязателен для смены пароля"
        if matched >= 3:
            said = _said(child, current, new)
            child.close(force=True)
            return "диалог смены пароля завершился до запроса нового пароля" + said
    elif matched == 1:
        pass
    elif matched == 2:
        said = _said(child, current, new)
        child.close(force=True)
        return "пароль первого входа не принят; повторных попыток не было" + said
    elif matched == 3:
        child.close(force=True)
        return "PTY обязателен для смены пароля"
    else:
        said = _said(child, current, new)
        child.close(force=True)
        return "диалог смены пароля не начался; требуется PTY" + said

    next_patterns = [
        re.compile(r"(?i)(?:retype|repeat).*password:\s*$"),
        re.compile(r"(?i)BAD PASSWORD|password.*(?:too short|quality|dictionary|simple)"),
        pexpect.EOF,
        pexpect.TIMEOUT,
    ]
    matched = _answer(child, new, next_patterns)
    if matched == 1:
        child.close(force=True)
        return "pam_pwquality отказал в новом пароле; подбор не выполнялся"
    if matched != 0:
        said = _said(child, current, new)
        child.close(force=True)
        return "диалог смены пароля оборвался до подтверждения" + said
    final_patterns = [
        re.compile(r"(?i)password updated successfully|all authentication tokens updated successfully"),
        re.compile(r"(?i)BAD PASSWORD|password.*(?:unchanged|failure|error)"),
        pexpect.EOF,
        pexpect.TIMEOUT,
    ]
    matched = _answer(child, new, final_patterns)
    if matched == 1:
        said = _said(child, current, new)
        child.close(force=True)
        return "passwd отказал в смене пароля" + said
    if matched == 3:
        child.close(force=True)
        return "passwd не завершился вовремя"
    if matched != 0:
        child.close(force=True)
        return "passwd завершился без явного сообщения об успехе; исход неясен"
    # A success banner alone is insufficient: the remote command must exit 0.
    ended = child.expect([pexpect.EOF, pexpect.TIMEOUT])
    if ended != 0:
        child.close(force=True)
        return "passwd сообщил об успехе, но не завершился вовремя; исход неясен"
    child.close()
    if child.exitstatus != 0:
        return "passwd сообщил об успехе, но завершился с ошибкой; исход неясен"
    return None


def _run_sops_editor(config: Path, target: Path, editor_body: str, files: dict[str, str]) -> str | None:
    with tempfile.TemporaryDirectory(prefix="vpn-fleet-sops-") as temp_name:
        temp = Path(temp_name)
        os.chmod(temp, 0o700)
        editor = temp / "editor.py"
        editor.write_text(editor_body, encoding="utf-8")
        editor.chmod(0o700)
        for name, value in files.items():
            item = temp / name
            item.write_text(value, encoding="utf-8")
            item.chmod(0o600)
        environment = os.environ.copy()
        environment["EDITOR"] = str(editor)
        environment.pop("SOPS_EDITOR", None)
        # Lock the whole decrypt/edit/encrypt transaction, not just its write.
        with secrets_lock(target):
            result = subprocess.run(
                ["sops", "--config", str(config), str(target)],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
    if result.returncode != 0:
        lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
        reason = lines[-1] if lines else "причина не названа"
        return f"{_display(target)}: sops не сохранил изменение — {reason}"
    return None


def _store_console_password(
    door: NetworkDoor, private_dir: Path, config: Path, password: str
) -> str | None:
    target = private_dir / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    key = f"node_{door.node_name.replace('-', '_')}_console_password"
    editor = """#!/usr/bin/env python3
import json
from pathlib import Path
import re
import sys

target = Path(sys.argv[1])
value = Path(__file__).with_name("value").read_text(encoding="utf-8")
key = Path(__file__).with_name("key").read_text(encoding="utf-8")
text = target.read_text(encoding="utf-8")
pattern = re.compile(r"^" + re.escape(key) + r":[^\\n]*$", re.MULTILINE)
updated, count = pattern.subn(key + ": " + json.dumps(value), text)
if count != 1:
    raise SystemExit(2)
target.write_text(updated, encoding="utf-8")
"""
    return _run_sops_editor(config, target, editor, {"value": password, "key": key})


def _store_entry_password(
    door: NetworkDoor, private_dir: Path, config: Path, password: str
) -> str | None:
    target = private_dir / "inventories" / "first-entry.sops.yml"
    editor = """#!/usr/bin/env python3
from pathlib import Path
import sys

target = Path(sys.argv[1])
node = Path(__file__).with_name("node").read_text(encoding="utf-8")
value = Path(__file__).with_name("value").read_text(encoding="utf-8")
module_dir = Path(__file__).with_name("module_dir").read_text(encoding="utf-8")
sys.path.insert(0, module_dir)
from first_contact_text import replace_password
target.write_text(replace_password(target.read_text(encoding="utf-8"), node, value), encoding="utf-8")
"""
    return _run_sops_editor(
        config,
        target,
        editor,
        {
            "node": door.node_name,
            "value": password,
            "module_dir": str(ROOT / "scripts"),
        },
    )


def _store_pending_password(
    door: NetworkDoor, private_dir: Path, config: Path, password: str
) -> str | None:
    target = private_dir / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    key = f"node_{door.node_name.replace('-', '_')}_console_password_pending"
    editor = """#!/usr/bin/env python3
from pathlib import Path
import json
import re
import sys
target = Path(sys.argv[1])
key = Path(__file__).with_name("key").read_text(encoding="utf-8")
value = Path(__file__).with_name("value").read_text(encoding="utf-8")
text = target.read_text(encoding="utf-8")
if re.search(r"(?m)^" + re.escape(key) + r":", text):
    raise SystemExit(2)
target.write_text(text.rstrip("\\n") + "\\n" + key + ": " + json.dumps(value) + "\\n", encoding="utf-8")
"""
    return _run_sops_editor(config, target, editor, {"key": key, "value": password})


def _pending_exists(door: NetworkDoor, private_dir: Path) -> bool:
    target = private_dir / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    key = f"node_{door.node_name.replace('-', '_')}_console_password_pending"
    if not target.is_file():
        return False
    return bool(re.search(r"(?m)^" + re.escape(key) + r":", target.read_text(encoding="utf-8")))


def _read_pending_password(
    door: NetworkDoor, private_dir: Path, config: Path
) -> tuple[str | None, str | None]:
    if not _pending_exists(door, private_dir):
        return None, None
    target = private_dir / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    key = f"node_{door.node_name.replace('-', '_')}_console_password_pending"
    value, reason = _sops_extract(config, target, f'["{key}"]')
    if reason or not value:
        return None, reason or f"{door.node_name}: pending-пароль пуст"
    return value, None


def _password_opens(door: NetworkDoor, known_hosts: Path, password: str) -> bool:
    """One password attempt; an expired-account change prompt proves login too."""
    command = _ssh_base(door, known_hosts)
    command.extend([
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        "-o", "NumberOfPasswordPrompts=1",
        "-tt", f"{door.user}@{door.host}", "true",
    ])
    child = _spawn(command)
    if _login_password(child, password):
        child.close(force=True)
        return False
    matched = child.expect([
        re.compile(r"(?i)(?:current|old)(?: UNIX)? password:\s*$"),
        re.compile(r"(?i)(?:new password|enter new UNIX password):\s*$"),
        re.compile(r"(?i)you are required to change your password"),
        re.compile(r"(?i)permission denied|authentication failure"),
        pexpect.EOF,
        pexpect.TIMEOUT,
    ])
    child.close(force=True)
    return matched < 3 or (matched == 4 and child.exitstatus == 0)


def _clear_pending_password(door: NetworkDoor, private_dir: Path, config: Path) -> str | None:
    target = private_dir / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    key = f"node_{door.node_name.replace('-', '_')}_console_password_pending"
    editor = """#!/usr/bin/env python3
from pathlib import Path
import re
import sys
target = Path(sys.argv[1])
key = Path(__file__).with_name("key").read_text(encoding="utf-8")
text = target.read_text(encoding="utf-8")
target.write_text(re.sub(r"(?m)^" + re.escape(key) + r":[^\\n]*\\n?", "", text), encoding="utf-8")
"""
    return _run_sops_editor(config, target, editor, {"key": key})


def _install_public_key(
    door: NetworkDoor, known_hosts: Path, password: str, public_key: str
) -> str | None:
    command = _ssh_base(door, known_hosts)
    command.extend([
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        "-o", "NumberOfPasswordPrompts=1",
        "-T",
        f"{door.user}@{door.host}",
        "sh -s",
    ])
    child = _spawn(command)
    reason = _login_password(child, password)
    if reason:
        child.close(force=True)
        return "новый пароль не открыл повторное соединение"
    encoded = base64.b64encode(public_key.strip().encode()).decode("ascii")
    script = (
        "set -eu\numask 077\nmkdir -p \"$HOME/.ssh\"\nchmod 700 \"$HOME/.ssh\"\n"
        "touch \"$HOME/.ssh/authorized_keys\"\nchmod 600 \"$HOME/.ssh/authorized_keys\"\n"
        f"key=$(printf %s {shlex.quote(encoded)} | base64 -d)\n"
        "grep -qxF \"$key\" \"$HOME/.ssh/authorized_keys\" || "
        "printf '%s\\n' \"$key\" >> \"$HOME/.ssh/authorized_keys\"\n"
    )
    child.send(script)
    child.sendeof()
    matched = child.expect([pexpect.EOF, pexpect.TIMEOUT])
    child.close()
    if matched != 0 or child.exitstatus not in (0, None):
        return "публичный ключ не удалось установить"
    return None


def _remove_first_entry(door: NetworkDoor, private_dir: Path, config: Path) -> str | None:
    target = private_dir / "inventories" / "first-entry.sops.yml"
    editor = """#!/usr/bin/env python3
from pathlib import Path
import sys

target = Path(sys.argv[1])
node = Path(__file__).with_name("node").read_text(encoding="utf-8")
module_dir = Path(__file__).with_name("module_dir").read_text(encoding="utf-8")
sys.path.insert(0, module_dir)
from first_contact_text import remove_entry
target.write_text(remove_entry(target.read_text(encoding="utf-8"), node), encoding="utf-8")
"""
    return _run_sops_editor(
        config,
        target,
        editor,
        {"node": door.node_name, "module_dir": str(ROOT / "scripts")},
    )


def _apply(node: str, private_dir: Path) -> int:
    if pexpect is None:
        print("ERROR: не хватает зависимости pexpect; установи requirements-ci.txt", file=sys.stderr)
        return 1
    for command in ("ssh", "ssh-keyscan", "ssh-keygen", "sops"):
        if not shutil.which(command):
            print(f"ERROR: не найдена обязательная команда {command}", file=sys.stderr)
            return 1
    local = inspect_door(node, private_dir)
    if local.reasons:
        for reason in local.reasons:
            print(f"ERROR: {reason}", file=sys.stderr)
        return 1
    door, reason = _network_door(node, private_dir)
    if reason or door is None:
        print(f"ERROR: {reason}", file=sys.stderr)
        return 1

    known_hosts = private_dir / "inventories" / "known_hosts"
    reason = _ensure_known_host(door, known_hosts)
    if reason:
        print(f"ERROR: {reason}", file=sys.stderr)
        return 1
    config = private_dir / ".sops.yaml"
    pending, reason = _read_pending_password(door, private_dir, config)
    if reason:
        print(f"ERROR: {reason}", file=sys.stderr)
        return 1
    if _key_opens(door, known_hosts):
        if pending:
            reason = _store_console_password(door, private_dir, config, pending)
            if reason:
                print(f"ERROR: {reason}", file=sys.stderr)
                return 1
            reason = _clear_pending_password(door, private_dir, config)
            if reason:
                print(f"ERROR: {reason}", file=sys.stderr)
                return 1
            print("first-contact: фактический пароль pending (доступ ключом подтверждён)")
        if door.entry_found:
            reason = _remove_first_entry(door, private_dir, config)
            if reason:
                print(f"ERROR: {reason}", file=sys.stderr)
                return 1
            print("first-contact: ключ уже открывает ноду; запись first_entry погашена")
        else:
            print("first-contact: уже сделано — ключ открывает ноду")
        return 0
    if not door.entry_found and not pending:
        print("ERROR: запись first_entry отсутствует, а ключ ноду не открывает", file=sys.stderr)
        return 1
    if door.entry_found and door.method != "password":
        print("ERROR: method: key запрещает попытку парольного входа", file=sys.stderr)
        return 1

    provider = None
    if door.entry_found:
        entry_file = private_dir / "inventories" / "first-entry.sops.yml"
        provider, reason = _sops_extract(
            config, entry_file, f'["first_entry"]["{door.node_name}"]["password"]'
        )
        if reason or not provider:
            print(f"ERROR: {reason or 'password расшифрован пустым'}", file=sys.stderr)
            return 1
    if pending and _password_opens(door, known_hosts, pending):
        new = pending
        print("first-contact: фактический пароль pending")
    else:
        if provider is None:
            print(f"ERROR: {door.node_name}: pending не принят, provider отсутствует; SOPS сохранён", file=sys.stderr)
            return 1
        new = pending or _new_password()
        if pending is None:
            reason = _store_pending_password(door, private_dir, config, new)
            if reason:
                print(f"ERROR: {reason}", file=sys.stderr)
                return 1
        reason = _change_password(door, known_hosts, provider, new)
        if reason:
            cause = ("ни один известный пароль не подтверждён или смена не завершилась"
                     if pending else "смена пароля первого входа не завершилась")
            print(f"ERROR: {door.node_name}: {cause} — {reason}; SOPS сохранён", file=sys.stderr)
            return 1
        print("first-contact: фактический пароль provider; сменён на pending"
              if pending else "first-contact: пароль первого входа сменён")
    reason = _store_console_password(door, private_dir, config, new)
    if reason:
        print(f"ERROR: {reason}", file=sys.stderr)
        return 1
    public_path = Path(f"{door.key_path}.pub")
    try:
        public_key = public_path.read_text(encoding="utf-8").strip()
    except OSError:
        print("ERROR: публичный ключ парка не читается", file=sys.stderr)
        return 1
    reason = _install_public_key(door, known_hosts, new, public_key)
    if reason:
        print(f"ERROR: {reason}", file=sys.stderr)
        return 1
    if not _key_opens(door, known_hosts):
        print("ERROR: отдельное соединение ключом не подтвердило доступ", file=sys.stderr)
        return 1
    reason = _clear_pending_password(door, private_dir, config)
    if reason:
        print(f"ERROR: {reason}", file=sys.stderr)
        return 1
    if door.entry_found:
        reason = _remove_first_entry(door, private_dir, config)
        if reason:
            print(f"ERROR: {reason}", file=sys.stderr)
            return 1
    print("first-contact: готово — ключ установлен и доказан отдельным соединением")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("node", help="inventory name without .yml")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="open the node over SSH and converge the first-contact invariant",
    )
    parser.add_argument(
        "--reasons-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--owned-known-hosts", choices=("door", "steady"), help=argparse.SUPPRESS)
    parser.add_argument("--known-hosts-file", type=Path, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    private_dir = Path(
        os.environ.get("VPN_FLEET_PRIVATE_DIR", str(ROOT / "private"))
    )
    if args.owned_known_hosts:
        known = args.known_hosts_file or private_dir / "inventories" / "known_hosts"
        print("\n".join(_owned_known_hosts(known, args.node, args.owned_known_hosts)))
        return 0
    if args.apply:
        try:
            return _apply(args.node, private_dir)
        except Exception:  # never expose command arguments or secret-bearing buffers
            print("ERROR: first-contact аварийно остановлен; значения скрыты", file=sys.stderr)
            return 1
    result = inspect_door(args.node, private_dir)
    if result.reasons:
        prefix = "" if args.reasons_only else "ERROR: "
        for reason in result.reasons:
            print(f"{prefix}{reason}", file=sys.stderr)
        return 1
    if not args.reasons_only:
        print(f"first-contact: {args.node}: дверь — {result.state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
