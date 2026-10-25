"""Synthetic wizard persistence failure and recovery tests."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wizard"))
import server  # noqa: E402
from common import first_contact_module  # noqa: E402


class FakeContact:
    def __init__(self, reason=None):
        self.reason = reason
        self.calls = []

    def _run_sops_editor(self, *_args):
        self.calls.append("sops")
        return self.reason

    def _entry_for_node(self, *_args):
        return False, None, "synthetic map", []


def test_inventory_failure_stops_before_sops(tmp_path: Path, monkeypatch):
    contact = FakeContact("synthetic failure")
    monkeypatch.setattr(server, "write_inventory", lambda *_: (_ for _ in ()).throw(OSError("synthetic")))
    written, reason = server.write_server_records(
        contact, tmp_path, tmp_path / "config", tmp_path / "entry",
        {"node": "synthetic", "user": "root", "port": 22, "method": "key"},
        "", (tmp_path, "synthetic"),
    )
    assert written is None and reason == "инвентарь не сохранён: OSError"
    assert contact.calls == []


def test_failure_between_writes_is_resumed_from_inventory(tmp_path: Path, monkeypatch):
    node = "synthetic"
    inventory = tmp_path / "inventories" / "nodes" / f"{node}.yml"
    inventory.parent.mkdir(parents=True)
    events = []

    def write_inventory(*_args):
        inventory.write_text("all:\n  hosts:\n    synthetic: {}\n", encoding="utf-8")
        events.append("inventory")
        return True, inventory

    class Contact(FakeContact):
        def _run_sops_editor(self, *_args):
            events.append("sops")
            return "synthetic interruption" if events.count("sops") == 1 else None

    contact = Contact()
    monkeypatch.setattr(server, "write_inventory", write_inventory)
    written, reason = server.write_server_records(
        contact, tmp_path, tmp_path / "config", tmp_path / "entry",
        {"node": node, "user": "root", "port": 22, "method": "key"},
        "", (tmp_path, node),
    )
    assert written is None and reason == "synthetic interruption"
    assert events == ["inventory", "sops"]
    assert server.known_servers(tmp_path) == [node]

    monkeypatch.setattr(server, "ask_yes", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(server, "ask", lambda *_args: "root")
    monkeypatch.setattr(server, "ask_port", lambda: 22)
    monkeypatch.setattr(server, "ask_choice", lambda *_args: "key")
    monkeypatch.setattr(server, "door_state", lambda *_args: "synthetic state")
    monkeypatch.setattr(server, "finish", lambda *_args: 0)
    assert server.fill_server(contact, tmp_path, node) == 0
    assert events == ["inventory", "sops", "sops"]


def test_fill_existing_inventory_can_recreate_missing_entry(tmp_path: Path, monkeypatch):
    contact = FakeContact()
    monkeypatch.setattr(server, "ask_yes", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(server, "ask", lambda *_args: "root")
    monkeypatch.setattr(server, "ask_port", lambda: 22)
    monkeypatch.setattr(server, "ask_choice", lambda *_args: "key")
    monkeypatch.setattr(server, "door_state", lambda *_args: "synthetic state")
    monkeypatch.setattr(server, "finish", lambda *_args: 0)
    assert server.fill_server(contact, tmp_path, "synthetic") == 0
    assert contact.calls == ["sops"]


def test_claim_old_known_hosts_requires_unique_address_and_consent(tmp_path, monkeypatch):
    private = tmp_path / "private"
    nodes = private / "inventories" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "one.yml").write_text(
        "all:\n  hosts:\n    one:\n      ansible_host: 192.0.2.7\n"
        "      bootstrap_ssh_port: 2424\n      node_location: synthetic\n")
    known = private / "inventories" / "known_hosts"
    original = ("[192.0.2.7]:2222 ssh-ed25519 AAAA old-note\n"
                "[192.0.2.7]:2424 ssh-ed25519 BBBB\n"
                "[192.0.2.8]:2222 ssh-ed25519 CCCC\n")
    known.write_text(original)
    module = first_contact_module()
    monkeypatch.setattr(server, "load_hosters", lambda: ({"synthetic": {"first_entry": {"port": 2222}}}, None))
    monkeypatch.setattr(server, "ask_yes", lambda *_args, **_kwargs: False)
    assert server.claim_known_hosts(module, private, "one") is None
    assert known.read_text() == original
    (nodes / "two.yml").write_text(
        "all:\n  hosts:\n    two:\n      ansible_host: 192.0.2.7\n")
    assert "адрес общий с two" in server.claim_known_hosts(module, private, "one")
    assert known.read_text() == original
    (nodes / "two.yml").unlink()
    monkeypatch.setattr(server, "ask_yes", lambda *_args, **_kwargs: True)
    assert server.claim_known_hosts(module, private, "one") is None
    assert module._owned_known_hosts(known, "one", "door") == [
        "[192.0.2.7]:2222 ssh-ed25519 AAAA vpn-fleet:one:door"]
    assert module._owned_known_hosts(known, "one", "steady") == [
        "[192.0.2.7]:2424 ssh-ed25519 BBBB vpn-fleet:one:steady"]
    assert "[192.0.2.8]:2222 ssh-ed25519 CCCC" in known.read_text()
