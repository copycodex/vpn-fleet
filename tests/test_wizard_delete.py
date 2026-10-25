"""Deleting one synthetic node leaves every other node intact."""

from pathlib import Path
from scripts.secrets_lock import secrets_lock
import os
import subprocess
import sys

import yaml
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wizard"))
import wizard  # noqa: E402
import server as wizard_server  # noqa: E402
from deletion_state import marker_path, write_marker  # noqa: E402


def synthetic_two_nodes(tmp_path: Path, monkeypatch):
    private = tmp_path / "private"
    inventory = private / "inventories"
    nodes = inventory / "nodes"
    nodes.mkdir(parents=True)
    for node, address in (("one", "192.0.2.41"), ("two", "192.0.2.42")):
        (nodes / f"{node}.yml").write_text(
            f"all:\n  hosts:\n    {node}:\n      ansible_host: {address}\n")
    known = inventory / "known_hosts"
    known.write_text("192.0.2.41 ssh-ed25519 AAAA vpn-fleet:one:door\n"
                     "192.0.2.42 ssh-ed25519 BBBB vpn-fleet:two:door\n")
    key = tmp_path / "synthetic-age.txt"
    subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, check=True)
    recipient = subprocess.run(["age-keygen", "-y", str(key)], capture_output=True,
                               text=True, check=True).stdout.strip()
    config = private / ".sops.yaml"
    config.write_text("creation_rules:\n  - path_regex: secrets\\.sops\\.yml$\n"
                      f"    age: {recipient}\n"
                      "  - path_regex: first-entry\\.sops\\.yml$\n"
                      f"    age: {recipient}\n")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    secrets = inventory / "group_vars" / "all" / "secrets.sops.yml"
    secrets.parent.mkdir(parents=True)
    secrets.write_text("node_one_reality_private_key: one-reality\n"
                       "node_one_hy2_password: one-hy2\n"
                       "node_two_reality_private_key: two-reality\n"
                       "node_two_hy2_password: two-hy2\n")
    entry = inventory / "first-entry.sops.yml"
    entry.write_text("first_entry:\n    one:\n        method: key\n"
                     "    two:\n        method: key\n")
    for path in (secrets, entry):
        with secrets_lock(path):
            subprocess.run(["sops", "--config", str(config), "-e", "-i", str(path)],
                           capture_output=True, check=True)
    clients = tmp_path / "clients"
    clients.mkdir()
    (clients / "one-phone.json").write_text("one")
    (clients / "two-phone.json").write_text("two")
    return private, nodes, known, secrets, entry, clients


def decrypted(path: Path) -> dict:
    result = subprocess.run(["sops", "--decrypt", str(path)], capture_output=True,
                            text=True, check=True)
    return yaml.safe_load(result.stdout)


@pytest.mark.parametrize("step", ["clients", "known_hosts", "first_entry",
                                      "inventory", "secrets"])
def test_delete_interruption_blocks_identity_and_retries(tmp_path, monkeypatch, capsys, step):
    private, nodes, known, secrets, entry, clients = synthetic_two_nodes(tmp_path, monkeypatch)
    before = decrypted(secrets)
    witness_inventory = (nodes / "two.yml").read_bytes()
    witness_client = (clients / "two-phone.json").read_bytes()
    with monkeypatch.context() as patch:
        if step in ("clients", "inventory"):
            unlink = Path.unlink

            def fail_unlink(path, *args, **kwargs):
                target = clients / "one-phone.json" if step == "clients" else nodes / "one.yml"
                if path == target:
                    raise OSError("synthetic unlink failure")
                return unlink(path, *args, **kwargs)

            patch.setattr(Path, "unlink", fail_unlink)
        elif step == "known_hosts":
            contact = wizard.first_contact_module()
            patch.setattr(contact, "_write_known_host_lines",
                          lambda *_: (_ for _ in ()).throw(OSError("synthetic write failure")))
            patch.setattr(wizard, "first_contact_module", lambda: contact)
        elif step == "first_entry":
            contact = wizard.first_contact_module()
            patch.setattr(contact, "_run_sops_editor", lambda *_: "synthetic editor failure")
            patch.setattr(wizard, "first_contact_module", lambda: contact)
        else:
            run = subprocess.run

            def fail_sops(command, *args, **kwargs):
                if command[:2] == ["sops", "--decrypt"] and command[-1] == str(secrets):
                    return subprocess.CompletedProcess(command, 1, "", "")
                return run(command, *args, **kwargs)

            patch.setattr(wizard.subprocess, "run", fail_sops)
        try:
            reason = wizard.delete_server(private, "one")
            assert reason is not None
        except OSError:
            pass

    assert marker_path(private, "one").exists()
    assert decrypted(secrets) == before
    if (nodes / "one.yml").exists():
        patch = monkeypatch
        patch.setattr(wizard, "ask_yes", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fill prompted")))
        assert wizard.fill_server(ContactForDelete, private, "one") != 0
        assert "удаление не завершено" in capsys.readouterr().err
    else:
        assert wizard.stage_of(ContactForDelete, private, "one") == wizard.STAGE_DELETING
        menus = []

        def choose(_title, items):
            menus.append(items)
            return items.index("Back") + 1

        monkeypatch.setattr(wizard, "menu", choose)
        assert wizard.servers_menu(ContactForDelete, private) == "back"
        assert any("one — удаление не завершено" in item for item in menus[0])

    assert wizard.delete_server(private, "one") is None
    assert not marker_path(private, "one").exists()
    assert not (nodes / "one.yml").exists()
    assert not (clients / "one-phone.json").exists()
    assert "vpn-fleet:one:" not in known.read_text()
    assert "one" not in decrypted(entry)["first_entry"]
    after = decrypted(secrets)
    assert all(not key.startswith("node_one_") for key in after)
    assert {key: value for key, value in after.items() if key.startswith("node_two_")} == {
        key: value for key, value in before.items() if key.startswith("node_two_")}
    assert (nodes / "two.yml").read_bytes() == witness_inventory
    assert (clients / "two-phone.json").read_bytes() == witness_client
    assert "vpn-fleet:two:door" in known.read_text()
    assert "two" in decrypted(entry)["first_entry"]


def test_marked_node_offers_only_delete_and_blocks_creation(tmp_path, monkeypatch, capsys):
    private, nodes, _, secrets, _, _ = synthetic_two_nodes(tmp_path, monkeypatch)
    write_marker(private, "one", "192.0.2.41")
    seen = []

    def choose(_title, items):
        seen.append(items)
        return items.index("Back") + 1

    monkeypatch.setattr(wizard, "menu", choose)
    assert wizard.server_menu(ContactForDelete, private, "one") == "back"
    assert seen == [["Delete server", "Back", "Quit"]]
    monkeypatch.setattr(wizard, "ask_yes", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("unexpected prompt")))
    assert wizard.fill_server(ContactForDelete, private, "one") != 0
    monkeypatch.setattr(wizard_server, "generate_reality_identity", lambda: (_ for _ in ()).throw(
        AssertionError("identity regenerated")))
    assert wizard_server.finish(ContactForDelete, private, "one", secrets,
                                private / ".sops.yaml", []) != 0
    monkeypatch.setattr(wizard_server, "print_route", lambda *_: None)
    monkeypatch.setattr(wizard_server, "route_of", lambda *_: "manual")
    monkeypatch.setattr(wizard_server, "door_of", lambda *_: {})
    monkeypatch.setattr(wizard_server, "ask", lambda *_: "one")
    assert wizard.new_server(ContactForDelete, private, "fleet", ("synthetic", {})) != 0
    assert "доведи удаление" in capsys.readouterr().err
    assert (nodes / "one.yml").exists()


def test_delete_plan_lists_safe_order(tmp_path, monkeypatch):
    private, _, _, _, _, _ = synthetic_two_nodes(tmp_path, monkeypatch)
    plan = wizard.delete_plan(private, "one")
    assert [line.split(":", 1)[0] for line in plan[:6]] == [
        "отметка удаления", "клиенты", "known_hosts", "первый вход",
        "инвентарь", "секреты"]
    assert plan[-1].startswith("отметка удаления: снять")




@pytest.mark.parametrize("ignore_file, ignored", [
    (ROOT / ".gitignore", not (ROOT / "publication/overlay/.gitignore").exists()),
    *([(ROOT / "publication/overlay/.gitignore", True)]
      if (ROOT / "publication/overlay/.gitignore").exists() else []),
])
def test_deletion_marker_repository_status(tmp_path: Path, ignore_file: Path, ignored: bool):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_bytes(ignore_file.read_bytes())
    write_marker(repo / "private", "one", "192.0.2.41")
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain",
                             "--untracked-files=all"], capture_output=True, text=True,
                            check=True)
    marker = "private/.wizard-deletions/one.json"
    assert (f"?? {marker}" in status.stdout.splitlines()) is not ignored
    check = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", marker],
                           check=False)
    assert (check.returncode == 0) is ignored


def test_delete_prefix_collision_does_not_mark_node(tmp_path: Path):
    private = tmp_path / "private"
    nodes = private / "inventories" / "nodes"
    nodes.mkdir(parents=True)
    for node in ("one-two", "one_two"):
        (nodes / f"{node}.yml").write_text(
            f"all:\n  hosts:\n    {node}:\n      ansible_host: 192.0.2.41\n")

    assert wizard.delete_server(private, "one-two") == (
        "префикс секретов совпадает с другой нодой")
    assert not marker_path(private, "one-two").exists()
    assert wizard.stage_of(wizard.first_contact_module(), private, "one-two") != (
        wizard.STAGE_DELETING)
    assert (nodes / "one-two.yml").exists()


@pytest.mark.parametrize("failure", ["invalid_json", "read_error"])
def test_delete_plan_reports_broken_marker(tmp_path: Path, monkeypatch, failure):
    private = tmp_path / "private"
    marker = marker_path(private, "one")
    marker.parent.mkdir(parents=True)
    marker.write_text("{")
    if failure == "read_error":
        monkeypatch.setattr(wizard, "read_marker", lambda *_: (_ for _ in ()).throw(
            OSError("synthetic read failure")))
    assert "отметка удаления не разбирается — исправить руками" in wizard.delete_plan(
        private, "one")


def test_delete_server_preserves_shared_address_and_other_secrets(tmp_path: Path, monkeypatch):
    private = tmp_path / "private"
    inventory = private / "inventories"
    (inventory / "nodes").mkdir(parents=True)
    secrets = inventory / "group_vars" / "all" / "secrets.sops.yml"
    secrets.parent.mkdir(parents=True)
    for node, address in (("one", "192.0.2.31"), ("two", "192.0.2.31"),
                          ("three", "192.0.2.32")):
        (inventory / "nodes" / f"{node}.yml").write_text(
            f"all:\n  hosts:\n    {node}:\n      ansible_host: {address}\n")
    known = inventory / "known_hosts"
    known.write_text("192.0.2.31 ssh-ed25519 c3ludGhldGlj vpn-fleet:one:door\n"
                     "[192.0.2.31]:2424 ssh-ed25519 c3ludGhldGlj vpn-fleet:two:steady\n"
                     "192.0.2.31,192.0.2.32 ssh-ed25519 c3ludGhldGlj\n"
                     "192.0.2.32 ssh-ed25519 c3ludGhldGlj vpn-fleet:three:door\n")
    assert any("строк без владельца" in line for line in wizard.delete_plan(private, "one"))
    key = tmp_path / "age.txt"
    subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, check=True)
    recipient = subprocess.run(["age-keygen", "-y", str(key)], capture_output=True,
                               text=True, check=True).stdout.strip()
    config = private / ".sops.yaml"
    config.write_text("creation_rules:\n  - path_regex: secrets\\.sops\\.yml$\n"
                      f"    age: {recipient}\n"
                      "  - path_regex: first-entry\\.sops\\.yml$\n"
                      f"    age: {recipient}\n")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    secrets.write_text("node_one_reality_private_key: synthetic\n"
                       "node_two_reality_private_key: other\n"
                       "node_three_reality_private_key: third\n")
    entry = inventory / "first-entry.sops.yml"
    entry.write_text("first_entry:\n    one:\n        method: key\n"
                     "    two:\n        method: key\n"
                     "    three:\n        method: key\n")
    for path in (secrets, entry):
        with secrets_lock(path):
            subprocess.run(["sops", "--config", str(config), "-e", "-i", str(path)],
                           capture_output=True, check=True)
    clients = tmp_path / "clients"
    clients.mkdir()
    (clients / "one-client.json").write_text("synthetic")
    (clients / "two-client.json").write_text("synthetic")

    assert wizard.delete_server(private, "one") is None
    assert not (inventory / "nodes" / "one.yml").exists()
    assert (inventory / "nodes" / "two.yml").exists()
    assert known.read_text().count("192.0.2.31") == 2
    assert "vpn-fleet:one:door" not in known.read_text()
    assert "vpn-fleet:two:steady" in known.read_text()
    assert not (clients / "one-client.json").exists()
    assert (clients / "two-client.json").exists()
    result = subprocess.run(["sops", "--decrypt", str(secrets)], capture_output=True,
                            text=True, check=True)
    values = yaml.safe_load(result.stdout)
    assert "node_one_reality_private_key" not in values
    assert values["node_two_reality_private_key"] == "other"
    assert values["node_three_reality_private_key"] == "third"
    result = subprocess.run(["sops", "--decrypt", str(entry)], capture_output=True,
                            text=True, check=True)
    assert "one" not in yaml.safe_load(result.stdout)["first_entry"]
    assert "two" in yaml.safe_load(result.stdout)["first_entry"]

    # Ownerless rows remain even after the final node at this address is deleted.
    assert wizard.delete_server(private, "two") is None
    assert "vpn-fleet:two:steady" not in known.read_text()
    assert "192.0.2.31,192.0.2.32" in known.read_text()
    assert "192.0.2.32" in known.read_text()
    assert secrets.exists()
    assert wizard.delete_server(private, "three") is None
    assert not secrets.exists()


def test_delete_menu_cancellation_changes_nothing(tmp_path: Path, monkeypatch):
    private = tmp_path / "private"
    inventory = private / "inventories" / "nodes" / "one.yml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("synthetic")
    monkeypatch.setattr(wizard, "stage_of", lambda *_: wizard.STAGE_NO_DOOR)
    monkeypatch.setattr(wizard, "host_vars", lambda *_: {})
    monkeypatch.setattr(wizard, "menu", lambda _title, items: items.index("Delete server") + 1)
    monkeypatch.setattr(wizard, "ask_yes", lambda *_args, **_kwargs: False)
    assert wizard.server_menu(ContactForDelete, private, "one") == "back"
    assert inventory.read_text() == "synthetic"


class ContactForDelete:
    pass


def test_delete_server_keeps_clients_of_a_node_with_longer_name(tmp_path: Path):
    private = tmp_path / "private"
    nodes = private / "inventories" / "nodes"
    nodes.mkdir(parents=True)
    for node, address in (("one", "192.0.2.41"), ("one-b", "192.0.2.42")):
        (nodes / f"{node}.yml").write_text(
            f"all:\n  hosts:\n    {node}:\n      ansible_host: {address}\n")
    clients = tmp_path / "clients"
    clients.mkdir()
    (clients / "one-phone.json").write_text("{}")
    (clients / "one-b-phone.json").write_text("{}")

    assert wizard.delete_server(private, "one") is None

    assert not (clients / "one-phone.json").exists()
    assert (clients / "one-b-phone.json").exists()
    assert (nodes / "one-b.yml").exists()


def test_delete_server_failed_known_hosts_replace_preserves_existing_bytes(tmp_path, monkeypatch):
    private = tmp_path / "private"
    nodes = private / "inventories" / "nodes"
    nodes.mkdir(parents=True)
    inventory = nodes / "one.yml"
    inventory.write_text(
        "all:\n  hosts:\n    one:\n      ansible_host: 192.0.2.41\n")
    known = private / "inventories" / "known_hosts"
    original = (b"192.0.2.41 ssh-ed25519 AAAA vpn-fleet:one:door\r\n"
                b"192.0.2.42 ssh-ed25519 BBBB vpn-fleet:two:door\r\n")
    known.write_bytes(original)

    original_replace = os.replace

    def fail_replace(source, target):
        if Path(target) == known:
            raise OSError("synthetic replace failure")
        original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_replace)
    assert "OSError" in wizard.delete_server(private, "one")

    assert known.read_bytes() == original
    assert inventory.exists()
    assert marker_path(private, "one").exists()
