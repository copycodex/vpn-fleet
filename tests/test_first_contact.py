from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from scripts.secrets_lock import secrets_lock
import shutil
import subprocess
import sys

import pytest
import yaml

from scripts.first_contact_text import (
    FirstEntryEditError,
    insert_entry,
    remove_entry,
    replace_password,
)


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "first-contact.py"
NODE = "test-node"


def _load_first_contact_module():
    spec = importlib.util.spec_from_file_location("first_contact_test_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(
    tmp_path: Path,
    *,
    entry: str | None,
    key_declared: bool,
    key_readable: bool = True,
) -> tuple[Path, dict[str, str]]:
    private = tmp_path / "private"
    nodes = private / "inventories" / "nodes"
    nodes.mkdir(parents=True)

    inventory = ["all:", "  hosts:", f"    {NODE}:", f"      node_name: {NODE}"]
    if key_declared:
        key = tmp_path / "park-key"
        if key_readable:
            key.write_text("synthetic key\n", encoding="utf-8")
        inventory.append(f"      ansible_ssh_private_key_file: {key}")
    (nodes / f"{NODE}.yml").write_text("\n".join(inventory) + "\n", encoding="utf-8")

    if entry is not None:
        document = "first_entry: {}\n" if entry == "" else "first_entry:\n" + entry
        (private / "inventories" / "first-entry.sops.yml").write_text(
            document, encoding="utf-8"
        )

    env = os.environ.copy()
    env["VPN_FLEET_PRIVATE_DIR"] = str(private)
    env.pop("SOPS_AGE_KEY_FILE", None)
    return private, env


def _run(
    env: dict[str, str], *, node: str = NODE, no_site_packages: bool = False
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable]
    if no_site_packages:
        command.append("-S")
    command.extend([str(SCRIPT), node])
    return subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _entry(method: str, password_line: str = "") -> str:
    return (
        f"  {NODE}:\n"
        "    user: root\n"
        "    port: 22\n"
        f"    method: {method}\n"
        f"{password_line}"
    )


def test_a_key_entry_and_readable_key_is_a_local_claim(tmp_path: Path) -> None:
    _, env = _fixture(tmp_path, entry=_entry("key"), key_declared=True)
    result = _run(env)
    assert result.returncode == 0
    assert "дверь" in result.stdout
    assert "claim; вход по сети не проверялся" in result.stdout


def test_b_no_entry_and_readable_key_is_a_local_claim(tmp_path: Path) -> None:
    _, env = _fixture(tmp_path, entry="", key_declared=True)
    result = _run(env)
    assert result.returncode == 0
    assert "читаемый ключ парка объявлен" in result.stdout
    assert "claim; вход по сети не проверялся" in result.stdout


def test_c_no_entry_and_no_key_has_no_door(tmp_path: Path) -> None:
    _, env = _fixture(tmp_path, entry="", key_declared=False)
    result = _run(env)
    assert result.returncode != 0
    assert "нечем открыть" in result.stderr


def test_d_password_entry_requires_sops_ciphertext(tmp_path: Path) -> None:
    ciphertext = "    password: ENC[AES256_GCM,data:synthetic,tag:synthetic,type:str]\n"
    _, env = _fixture(
        tmp_path,
        entry=_entry("password", ciphertext),
        key_declared=False,
    )
    result = _run(env)
    assert result.returncode == 0
    assert "пароль из зашифрованной карты" in result.stdout
    assert "synthetic" not in result.stdout


def test_e_plaintext_password_is_rejected_without_echoing_it(tmp_path: Path) -> None:
    secret = "provider-secret-marker"
    _, env = _fixture(
        tmp_path,
        entry=_entry("password", f"    password: {secret}\n"),
        key_declared=False,
    )
    result = _run(env)
    assert result.returncode != 0
    assert "открытым текстом" in result.stderr
    assert secret not in result.stdout + result.stderr


def test_f_empty_method_names_the_field(tmp_path: Path) -> None:
    _, env = _fixture(tmp_path, entry=_entry(""), key_declared=False)
    result = _run(env)
    assert result.returncode != 0
    assert "поле method" in result.stderr


def test_g_key_entry_rejects_even_empty_password_field(tmp_path: Path) -> None:
    _, env = _fixture(
        tmp_path,
        entry=_entry("key", '    password: ""\n'),
        key_declared=True,
    )
    result = _run(env)
    assert result.returncode != 0
    assert "поле password лишнее" in result.stderr


def test_h_declared_but_unreadable_key_has_no_door(tmp_path: Path) -> None:
    _, env = _fixture(
        tmp_path,
        entry=_entry("key"),
        key_declared=True,
        key_readable=False,
    )
    result = _run(env)
    assert result.returncode != 0
    assert "нечем открыть" in result.stderr
    assert "не читается с этой машины" in result.stderr


def test_missing_map_is_not_an_error_when_key_is_readable(tmp_path: Path) -> None:
    _, env = _fixture(tmp_path, entry=None, key_declared=True)
    result = _run(env)
    assert result.returncode == 0
    assert "дверь" in result.stdout




def test_present_but_empty_node_entry_is_a_schema_error(tmp_path: Path) -> None:
    _, env = _fixture(tmp_path, entry=f"  {NODE}:\n", key_declared=True)
    result = _run(env)
    assert result.returncode != 0
    assert "пустая запись не равна отсутствующей" in result.stderr


def test_null_empty_map_is_accepted_like_explicit_empty_map(tmp_path: Path) -> None:
    private, env = _fixture(tmp_path, entry=None, key_declared=True)
    (private / "inventories" / "first-entry.sops.yml").write_text(
        "first_entry:\n", encoding="utf-8"
    )
    result = _run(env)
    assert result.returncode == 0
    assert "дверь" in result.stdout


def test_missing_pyyaml_is_named_without_a_traceback(tmp_path: Path) -> None:
    _, env = _fixture(tmp_path, entry="", key_declared=True)
    result = _run(env, no_site_packages=True)
    assert result.returncode != 0
    assert "не хватает зависимости PyYAML" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_missing_pexpect_is_named_without_a_traceback(tmp_path: Path) -> None:
    _, env = _fixture(tmp_path, entry="", key_declared=True)
    isolated = tmp_path / "isolated-modules"
    shutil.copytree(Path(yaml.__file__).parent, isolated / "yaml")
    env["PYTHONPATH"] = str(isolated)
    local_result = _run(env, no_site_packages=True)
    # Local inspection does not require pexpect; only explicit network apply does.
    assert local_result.returncode == 0
    command = [sys.executable, "-S", str(SCRIPT), NODE, "--apply"]
    result = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "не хватает зависимости pexpect" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_commented_map_edits_preserve_documentation_and_explicit_empty_map() -> None:
    documentation = "".join(f"# production-shaped rule {index}\n" for index in range(88))
    source = (
        documentation
        + "first_entry:\n"
        + "    test-node:\n"
        + "        # per-node method documentation\n"
        + "        user: fresh\n"
        + "        port: 22\n"
        + "        method: password\n"
        + "        # owner fills this through SOPS\n"
        + '        password: "old synthetic"\n'
        + "sops:\n"
        + "    encrypted_regex: ^password$\n"
    )
    comments_before = [line for line in source.splitlines() if line.lstrip().startswith("#")]

    updated = replace_password(source, NODE, "new synthetic")
    assert [line for line in updated.splitlines() if line.lstrip().startswith("#")] == comments_before
    assert '        password: "new synthetic"' in updated
    assert updated.replace('        password: "new synthetic"', '        password: "old synthetic"') == source

    exhausted = remove_entry(updated, NODE)
    assert [line for line in exhausted.splitlines() if line.lstrip().startswith("#")] == comments_before
    assert "first_entry: {}\n" in exhausted
    assert "test-node:" not in exhausted
    assert "user: fresh" not in exhausted
    assert "sops:\n    encrypted_regex: ^password$\n" in exhausted


def test_known_hosts_uses_and_finds_plain_22_and_bracketed_custom_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_first_contact_module()
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("# production-shaped known_hosts documentation\n", encoding="utf-8")
    key_path = tmp_path / "fleet-key"
    key_path.write_text("synthetic private key placeholder\n", encoding="utf-8")
    original_run = subprocess.run
    scans: list[int] = []

    def fake_run(command, *args, **kwargs):
        if command[0] != "ssh-keyscan":
            return original_run(command, *args, **kwargs)
        port = 22
        if "-p" in command:
            port = int(command[command.index("-p") + 1])
        scans.append(port)
        token = "stand.invalid" if port == 22 else f"[stand.invalid]:{port}"
        key_blob = "AAAA" + "C3NzaC1lZDI1NTE5" + "AAAAISYNTHETIC"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{token} ssh-ed25519 {key_blob}\n",
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    for port, token in ((22, "stand.invalid"), (2424, "[stand.invalid]:2424")):
        door = module.NetworkDoor(NODE, "stand.invalid", "fresh", port, "password", True, key_path)
        assert module._known_host_token(door.host, door.port) == token
        assert module._ensure_known_host(door, known_hosts) is None
        assert module._ensure_known_host(door, known_hosts) is None

    lines = [line for line in known_hosts.read_text(encoding="utf-8").splitlines() if line]
    assert lines[0] == "# production-shaped known_hosts documentation"
    assert [line.split()[0] for line in lines[1:]] == ["stand.invalid", "[stand.invalid]:2424"]
    assert all(line.endswith(f"vpn-fleet:{NODE}:door") for line in lines[1:])
    assert scans == [22, 2424]


def test_known_hosts_owned_rows_compare_foreign_keys_and_ssh_accepts_label(tmp_path, monkeypatch):
    module = _load_first_contact_module()
    known = tmp_path / "known_hosts"
    host_key = tmp_path / "fixture_host_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
        check=True,
        capture_output=True,
    )
    key = host_key.with_suffix(".pub").read_text(encoding="utf-8").split()[1]
    token = "[192.0.2.7]:2222"
    door = module.NetworkDoor("one", "192.0.2.7", "root", 2222, "key", False, tmp_path / "key")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    scan = fake_bin / "ssh-keyscan"
    scan.write_text(f"#!/bin/sh\nprintf '%s\\n' '{token} ssh-ed25519 {key}'\n")
    scan.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    known.write_text(f"{token} ssh-ed25519 {key} vpn-fleet:two:door\n")
    assert module._ensure_known_host(door, known) is None
    assert len(module._owned_known_hosts(known, "one", "door")) == 1
    assert len(module._owned_known_hosts(known, "two", "door")) == 1
    assert subprocess.run(["ssh-keygen", "-F", token, "-f", str(known)],
                          capture_output=True).returncode == 0
    assert subprocess.run(["ssh-keygen", "-l", "-f", str(known)],
                          capture_output=True).returncode == 0
    before = known.read_bytes()
    known.write_text(f"{token} ssh-ed25519 DIFFERENT vpn-fleet:two:door\n")
    before = known.read_bytes()
    assert "другой ключ" in module._ensure_known_host(door, known)
    assert known.read_bytes() == before


def test_known_hosts_write_failure_preserves_existing_bytes(tmp_path, monkeypatch):
    module = _load_first_contact_module()
    known = tmp_path / "known_hosts"
    original = b"# existing fleet keys\r\nstand.invalid ssh-ed25519 AAAA\r\n"
    known.write_bytes(original)

    def fail_replace(*_args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        module._write_known_host_lines(known, ["new.invalid ssh-ed25519 BBBB"])

    assert known.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == ["known_hosts"]


def test_known_host_owner_requires_exact_valid_label():
    module = _load_first_contact_module()
    row = "stand.invalid ssh-ed25519 AAAA"
    assert module._known_host_owner(row + " vpn-fleet:one:door") == ("one", "door")
    assert module._known_host_owner(row + " vpn-fleet:one-b:steady") == ("one-b", "steady")
    assert module._known_host_owner(row + " vpn-fleet:one:door extra") is None
    assert module._known_host_owner(row + " vpn-fleet:one:other") is None
    assert module._known_host_owner("# " + row + " vpn-fleet:one:door") is None


def test_bootstrap_records_node_label_and_uses_owned_door_rows():
    playbook = (ROOT / "playbooks" / "bootstrap.yml").read_text()
    assert "--owned-known-hosts" in playbook
    assert "--known-hosts-file" in playbook
    assert "vpn-fleet:{{ inventory_hostname }}:steady" in playbook
    assert "vpn-fleet:' ~ inventory_hostname ~ ':door$" in playbook


def test_reasons_only_remains_local_even_without_network_commands(tmp_path: Path) -> None:
    _, env = _fixture(tmp_path, entry="", key_declared=True)
    env["PATH"] = ""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), NODE, "--reasons-only"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""




def test_insert_entry_leaves_the_neighbour_untouched() -> None:
    source = (
        "# map documentation\n"
        "first_entry:\n"
        "    first-node:\n"
        "        # provider handed it over as root\n"
        "        user: root\n"
        "        port: 22\n"
        "        method: password\n"
        "        password: ENC[AES256_GCM,data:synthetic,type:str]\n"
        "sops:\n"
        "    version: 3.9.1\n"
    )

    updated = insert_entry(source, "second-node", user="ubuntu", port=2222, method="key")

    assert "        password: ENC[AES256_GCM,data:synthetic,type:str]\n" in updated
    assert "        # provider handed it over as root\n" in updated
    parsed = yaml.safe_load(updated)
    assert parsed["first_entry"]["first-node"]["user"] == "root"
    assert parsed["first_entry"]["second-node"] == {
        "user": "ubuntu",
        "port": 2222,
        "method": "key",
    }
    assert "password" not in parsed["first_entry"]["second-node"]


def test_insert_entry_refuses_to_shadow_an_existing_record() -> None:
    source = "first_entry:\n    node:\n        user: root\n        port: 22\n        method: key\n"
    with pytest.raises(FirstEntryEditError, match="already exists"):
        insert_entry(source, "node", user="root", port=22, method="key")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user": "root", "port": 22, "method": "sudo"},
        {"user": "root", "port": 0, "method": "key"},
        {"user": "root", "port": True, "method": "key"},
    ],
)
def test_insert_entry_validates_the_schema(kwargs: dict) -> None:
    with pytest.raises(FirstEntryEditError):
        insert_entry("first_entry: {}\n", "node", **kwargs)


def test_insert_entry_rejects_a_name_that_is_not_a_node() -> None:
    with pytest.raises(FirstEntryEditError, match="node name"):
        insert_entry("first_entry: {}\n", "../etc/passwd", user="root", port=22, method="key")


@pytest.mark.parametrize(
    ("final_match", "ending", "exitstatus", "message"),
    [(2, None, None, "без явного сообщения"),
     (3, None, None, "не завершился вовремя"),
     (0, 0, 1, "завершился с ошибкой")],
)
def test_password_change_requires_banner_and_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    final_match: int, ending: int | None, exitstatus: int | None, message: str
) -> None:
    module = _load_first_contact_module()

    class Child:
        before = ""
        after = ""
        def __init__(self):
            # A silent final answer is nudged with \r once, so TIMEOUT comes twice.
            final = [final_match, final_match] if final_match == 3 else [final_match]
            self.matches = iter([0, 1, 0, *final] + ([] if ending is None else [ending]))
            self.exitstatus = exitstatus
        def expect(self, _patterns, timeout=None):
            return next(self.matches)
        def sendline(self, _text):
            pass
        def send(self, _text):
            pass
        def close(self, force=False):
            pass

    monkeypatch.setattr(module, "_spawn", lambda _command: Child())
    door = module.NetworkDoor(NODE, "stand.invalid", "root", 22, "password", True, tmp_path / "key")
    assert message in module._change_password(door, tmp_path / "known_hosts", "old synthetic", "new synthetic")


@pytest.mark.parametrize(
    ("current_replies", "expected_sends"),
    [
        # Terminal accepting \n at once; \r is never sent.
        ([0], ["old synthetic\n", "new synthetic\n", "new synthetic\n"]),
        # Terminal silent after \n until the wait ends; \r closes the line.
        ([4, 0], ["old synthetic\n", "\r", "new synthetic\n", "new synthetic\n"]),
    ],
)
def test_remote_answer_ends_with_newline_then_nudges_with_cr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    current_replies: list[int], expected_sends: list[str]
) -> None:
    module = _load_first_contact_module()
    sent: list[str] = []

    class Child:
        before = ""
        after = ""
        exitstatus = 0
        def __init__(self):
            # login, "Current password", reply(s) to current, retype, banner, EOF
            self.matches = iter([0, 0, *current_replies, 0, 0, 0])
        def expect(self, _patterns, timeout=None):
            return next(self.matches)
        def sendline(self, _text):
            pass
        def send(self, text):
            sent.append(text)
        def close(self, force=False):
            pass

    monkeypatch.setattr(module, "_spawn", lambda _command: Child())
    door = module.NetworkDoor(NODE, "stand.invalid", "ubuntu", 22, "password", True, tmp_path / "key")
    assert module._change_password(door, tmp_path / "known_hosts", "old synthetic", "new synthetic") is None
    assert sent == expected_sends


def test_pending_password_is_written_before_remote_change_even_on_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_first_contact_module()
    private, _ = _fixture(
        tmp_path,
        entry=_entry("password", "    password: ENC[AES256_GCM,data:synthetic,tag:synthetic,type:str]\n"),
        key_declared=True,
    )
    door = module.NetworkDoor(NODE, "stand.invalid", "root", 22, "password", True, tmp_path / "key")
    events = []
    monkeypatch.setattr(module, "inspect_door", lambda *_: module.DoorResult("ready", ()))
    monkeypatch.setattr(module, "_network_door", lambda *_: (door, None))
    monkeypatch.setattr(module, "_ensure_known_host", lambda *_: None)
    monkeypatch.setattr(module, "_key_opens", lambda *_: False)
    monkeypatch.setattr(module.shutil, "which", lambda _: "/synthetic/tool")
    monkeypatch.setattr(module, "_sops_extract", lambda *_: ("old synthetic", None))
    monkeypatch.setattr(module, "_new_password", lambda: "new synthetic")
    monkeypatch.setattr(module, "_store_pending_password", lambda *args: events.append(("pending", args[-1])))
    def crash(*_args):
        events.append(("remote", None))
        raise RuntimeError("simulated crash after remote change")
    monkeypatch.setattr(module, "_change_password", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        module._apply(NODE, private)
    assert events == [("pending", "new synthetic"), ("remote", None)]


def test_pending_converges_when_key_already_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_first_contact_module()
    private, _ = _fixture(
        tmp_path,
        entry=_entry("password", "    password: ENC[AES256_GCM,data:synthetic,tag:synthetic,type:str]\n"),
        key_declared=True,
    )
    door = module.NetworkDoor(NODE, "stand.invalid", "root", 22, "password", True, tmp_path / "key")
    monkeypatch.setattr(module, "inspect_door", lambda *_: module.DoorResult("ready", ()))
    monkeypatch.setattr(module, "_network_door", lambda *_: (door, None))
    monkeypatch.setattr(module, "_ensure_known_host", lambda *_: None)
    monkeypatch.setattr(module, "_key_opens", lambda *_: True)
    monkeypatch.setattr(module, "_read_pending_password", lambda *_: ("new synthetic", None))
    monkeypatch.setattr(module.shutil, "which", lambda _: "/synthetic/tool")
    actions = []
    monkeypatch.setattr(module, "_store_console_password", lambda *args: actions.append("console"))
    monkeypatch.setattr(module, "_clear_pending_password", lambda *args: actions.append("clear"))
    monkeypatch.setattr(module, "_remove_first_entry", lambda *args: actions.append("entry"))
    assert module._apply(NODE, private) == 0
    assert actions == ["console", "clear", "entry"]
    assert "pending" in capsys.readouterr().out


@pytest.mark.skipif(not shutil.which("age-keygen") or not shutil.which("sops"),
                    reason="synthetic SOPS encryption needs age-keygen and sops")
def test_pending_password_is_encrypted_in_existing_full_sops_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_first_contact_module()
    private = tmp_path / "private"
    target = private / "inventories/group_vars/all/secrets.sops.yml"
    target.parent.mkdir(parents=True)
    key_file = tmp_path / "synthetic-age-key"
    subprocess.run(["age-keygen", "-o", str(key_file)], capture_output=True, check=True)
    recipient = subprocess.check_output(["age-keygen", "-y", str(key_file)], text=True).strip()
    config = private / ".sops.yaml"
    config.write_text(
        "creation_rules:\n  - path_regex: .*secrets\\.sops\\.yml$\n"
        f"    age: {recipient}\n", encoding="utf-8"
    )
    target.write_text('node_test_node_console_password: "old synthetic"\n', encoding="utf-8")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key_file))
    with secrets_lock(target):
        encrypted = subprocess.run(
            ["sops", "--encrypt", "--config", str(config), str(target)],
            text=True, capture_output=True, check=True,
        )
    target.write_text(encrypted.stdout, encoding="utf-8")
    door = module.NetworkDoor(NODE, "stand.invalid", "root", 22, "password", True, tmp_path / "key")
    assert module._store_pending_password(door, private, config, "new synthetic") is None
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["node_test_node_console_password_pending"].startswith("ENC[")
    assert module._pending_exists(door, private)
    pending = subprocess.run(
        ["sops", "--config", str(config), "--decrypt", "--extract",
         '["node_test_node_console_password_pending"]', str(target)],
        text=True, capture_output=True, check=True,
    )
    assert pending.stdout.strip() == "new synthetic"
    assert module._clear_pending_password(door, private, config) is None
    assert not module._pending_exists(door, private)


def test_two_sops_editors_keep_both_nodes(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sops = fake_bin / "sops"
    fake_sops.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, subprocess, sys, tempfile, time\n"
        "target = pathlib.Path(sys.argv[-1])\n"
        "with tempfile.TemporaryDirectory() as name:\n"
        "  copy = pathlib.Path(name) / 'map.yml'\n"
        "  copy.write_bytes(target.read_bytes())\n"
        "  time.sleep(0.3)\n"
        "  subprocess.run([os.environ['EDITOR'], str(copy)], check=True)\n"
        "  target.write_bytes(copy.read_bytes())\n",
        encoding="utf-8",
    )
    fake_sops.chmod(0o700)
    target = tmp_path / "map.yml"
    target.write_text("first_entry: {}\n", encoding="utf-8")
    worker = (
        "import importlib.util, sys\nfrom pathlib import Path\n"
        "spec=importlib.util.spec_from_file_location('contact', sys.argv[1])\n"
        "module=importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name]=module\n"
        "spec.loader.exec_module(module)\n"
        "editor='''#!/usr/bin/env python3\nfrom pathlib import Path\nimport sys\n"
        "p=Path(sys.argv[1]); name=Path(__file__).with_name(\"node\").read_text()\n"
        "old=p.read_text(); p.write_text(old.replace(\"first_entry: {}\", \"first_entry:\") + \"  \" + name + \": {}\\\\n\")\n'''\n"
        "error=module._run_sops_editor(Path(sys.argv[2]), Path(sys.argv[3]), editor, {'node':sys.argv[4]})\n"
        "assert error is None, error\n"
    )
    env = {**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]}
    commands = [[sys.executable, "-c", worker, str(SCRIPT), str(tmp_path / "config"), str(target), node]
                for node in ("alpha", "beta")]
    first = subprocess.Popen(commands[0], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen(commands[1], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for process in (first, second):
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
    assert set(yaml.safe_load(target.read_text())["first_entry"]) == {"alpha", "beta"}
