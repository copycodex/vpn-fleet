"""Local facts for wizard server stages; no network calls."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wizard"))
import wizard  # noqa: E402


class Contact:
    _contact = wizard.first_contact_module()
    _known_host_fields = staticmethod(_contact._known_host_fields)
    _owned_known_hosts = staticmethod(_contact._owned_known_hosts)
    @staticmethod
    def _load_yaml(path, _label):
        import yaml
        return yaml.safe_load(path.read_text()), []

    @staticmethod
    def _known_host_token(host, port):
        return host if port == 22 else f"[{host}]:{port}"


def test_stage_uses_entry_and_both_known_host_ports(tmp_path: Path, monkeypatch):
    private = tmp_path / "private"
    inventory = private / "inventories"
    (inventory / "nodes").mkdir(parents=True)
    key = tmp_path / "key"
    key.write_text("synthetic")
    (inventory / "nodes" / "ci.yml").write_text(
        "all:\n  hosts:\n    ci:\n      ansible_host: 192.0.2.33\n"
        "      bootstrap_ssh_port: 2424\n      node_location: ci\n"
        f"      ansible_ssh_private_key_file: {key}\n"
    )
    known = inventory / "known_hosts"
    known.write_text("")
    monkeypatch.setattr(wizard, "load_hosters", lambda: ({"ci": {"door": {"port": 22}}}, None))
    assert wizard.stage_of(Contact, private, "ci") == wizard.STAGE_NO_DOOR
    (inventory / "first-entry.sops.yml").write_text("first_entry:\n  ci:\n    method: key\n")
    assert wizard.stage_of(Contact, private, "ci") == wizard.STAGE_FIRST_CONTACT
    (inventory / "first-entry.sops.yml").unlink()
    known.write_text("192.0.2.33 ssh-ed25519 c3ludGhldGlj vpn-fleet:ci:door\n")
    assert wizard.stage_of(Contact, private, "ci") == wizard.STAGE_BOOTSTRAP
    known.write_text("[192.0.2.33]:2222 ssh-ed25519 c3ludGhldGlj vpn-fleet:ci:door\n")
    assert wizard.stage_of(Contact, private, "ci") == wizard.STAGE_BOOTSTRAP
    known.write_text("[192.0.2.33]:2424 ssh-ed25519 c3ludGhldGlj vpn-fleet:ci:steady\n")
    assert wizard.stage_of(Contact, private, "ci") == wizard.STAGE_SITE


def test_shared_address_and_unowned_rows_do_not_give_another_node_a_stage(tmp_path):
    private = tmp_path / "private"
    nodes = private / "inventories" / "nodes"
    nodes.mkdir(parents=True)
    key = tmp_path / "key"
    key.write_text("synthetic")
    for node in ("one", "one-b"):
        (nodes / f"{node}.yml").write_text(
            f"all:\n  hosts:\n    {node}:\n      ansible_host: 192.0.2.33\n"
            f"      bootstrap_ssh_port: 2424\n      ansible_ssh_private_key_file: {key}\n")
    known = private / "inventories" / "known_hosts"
    known.write_text("[192.0.2.33]:2222 ssh-ed25519 AAAA vpn-fleet:one:door\n"
                     "192.0.2.33 ssh-ed25519 BBBB\n")
    assert wizard.stage_of(Contact, private, "one") == wizard.STAGE_BOOTSTRAP
    assert wizard.stage_of(Contact, private, "one-b") == wizard.STAGE_NO_DOOR
    known.write_text("192.0.2.33 ssh-ed25519 BBBB\n")
    assert wizard.stage_of(Contact, private, "one") == wizard.STAGE_NO_DOOR


def test_site_stage_offers_repeat_and_export():
    actions = wizard.next_action("ci", wizard.STAGE_SITE, {})
    assert [name for name, _ in actions] == ["make site NODE=ci", "make export NODE=ci"]
    assert [build() for _, build in actions] == [
        ["make", "site", "NODE=ci"], ["make", "export", "NODE=ci"]]


def test_bootstrap_action_uses_owned_door_port_and_asks_only_for_ambiguity(tmp_path, monkeypatch):
    known = tmp_path / "inventories" / "known_hosts"
    known.parent.mkdir(parents=True)
    host = {"ansible_host": "192.0.2.33", "node_location": "ci"}
    monkeypatch.setattr(wizard, "load_hosters", lambda: ({"ci": {"door": {"user": "root", "port": 22}}}, None))
    calls = []
    monkeypatch.setattr(wizard, "ask", lambda _prompt, default: calls.append(default) or default)
    known.write_text("[192.0.2.33]:2222 ssh-ed25519 AAAA vpn-fleet:ci:door\n")
    actions = wizard.next_action("ci", wizard.STAGE_BOOTSTRAP, host, tmp_path, Contact)
    assert actions[0][0].endswith("--from root@2222")
    assert actions[0][1]()[-1] == "root@2222"
    assert calls == []
    known.write_text(known.read_text() + "[192.0.2.33]:2323 ssh-ed25519 BBBB vpn-fleet:ci:door\n")
    wizard.next_action("ci", wizard.STAGE_BOOTSTRAP, host, tmp_path, Contact)[0][1]()
    assert calls == ["root@2222"]
    known.write_text("")
    wizard.next_action("ci", wizard.STAGE_BOOTSTRAP, host, tmp_path, Contact)[0][1]()
    assert calls == ["root@2222", "root@22"]
