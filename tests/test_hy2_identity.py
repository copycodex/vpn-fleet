"""Synthetic SOPS coverage for the wizard's Hysteria2 identity."""

import base64
import hashlib
import json
import os
from pathlib import Path
from scripts.secrets_lock import secrets_lock
import re
import shutil
import subprocess
import sys
import threading
import time

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wizard"))
import server  # noqa: E402


@pytest.fixture
def private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    private = tmp_path / "private"
    nodes = private / "inventories" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "ci.yml").write_text(
        "all:\n  hosts:\n    ci:\n      node_name: ci\n"
        "      ansible_host: 192.0.2.70\n"
        f"      ansible_ssh_private_key_file: {tmp_path / 'ssh-key'}\n"
        "      singbox_hy2_sni: vpn-test.invalid\n"
        "      singbox_client_names:\n",
        encoding="utf-8",
    )
    key = tmp_path / "test-age.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True
    ).stdout.strip()
    (private / ".sops.yaml").write_text(
        "creation_rules:\n"
        "  - path_regex: inventories/group_vars/all/secrets\\.sops\\.yml$\n"
        f"    age: {recipient}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    monkeypatch.setattr(server, "report", lambda *_: 0)
    (tmp_path / "ssh-key").write_text("synthetic\n", encoding="utf-8")
    (private / "inventories" / "first-entry.sops.yml").write_text(
        "first_entry:\n  ci:\n    user: root\n    port: 22\n    method: key\n",
        encoding="utf-8",
    )
    (private / "inventories" / "known_hosts").write_text(
        "192.0.2.70 ssh-ed25519 synthetic\n", encoding="utf-8",
    )
    (private / "inventories" / "group_vars" / "all").mkdir(parents=True)
    (private / "inventories" / "group_vars" / "all" / "ssh_keys.yml").write_text(
        "ssh_authorized_keys_devops:\n  - name: synthetic\n    key: synthetic\n",
        encoding="utf-8",
    )
    return private


def decrypted(path: Path) -> dict:
    result = subprocess.run(
        ["sops", "--decrypt", str(path)], check=True, capture_output=True, text=True
    )
    return yaml.safe_load(result.stdout)


def gate(private: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROOT / "scripts" / "check-private-inputs.sh"), "ci"],
        env={**os.environ, "VPN_FLEET_PRIVATE_DIR": str(private)},
        capture_output=True, text=True,
    )


def test_wizard_generates_hy2_identity_once(private: Path) -> None:
    target = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    module = server.first_contact_module()
    assert server.finish(module, private, "ci", target, private / ".sops.yaml",
                         ["ci-client"]) == 0
    values = decrypted(target)
    password = values["node_ci_hy2_password"]
    cert = values["node_ci_hy2_certificate"]
    key = values["node_ci_hy2_key"]
    pin = values["node_ci_hy2_certificate_public_key_sha256"]
    assert len(password) >= 32
    assert password != values["node_ci_clients"][0]["uuid"]
    assert re.fullmatch(r"[A-Za-z0-9+/]{43}=", pin)
    assert "BEGIN " + "EC PRIVATE KEY" in key or "BEGIN " + "PRIVATE KEY" in key
    details = subprocess.run(
        ["openssl", "x509", "-noout", "-subject", "-text"],
        input=cert, check=True, capture_output=True, text=True,
    ).stdout
    assert re.search(r"subject=CN\s*=\s*vpn-test\.invalid", details)
    assert "prime256v1" in details
    public = subprocess.run(
        ["openssl", "x509", "-pubkey", "-noout"], input=cert,
        check=True, capture_output=True, text=True,
    ).stdout
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "DER"], input=public.encode(),
        check=True, capture_output=True,
    ).stdout
    assert pin == base64.b64encode(hashlib.sha256(der).digest()).decode()
    before = target.read_bytes()
    assert server.finish(module, private, "ci", target, private / ".sops.yaml", []) == 0
    assert target.read_bytes() == before

    result = gate(private)
    assert result.returncode == 0, result.stderr


def test_partial_hy2_identity_is_rejected_without_rewrite(private: Path) -> None:
    target = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    target.write_text("node_ci_hy2_password: synthetic\n", encoding="utf-8")
    with secrets_lock(target):
        subprocess.run(
            ["sops", "--config", str(private / ".sops.yaml"), "-e", "-i", str(target)],
            check=True, capture_output=True,
        )
    before = target.read_bytes()
    assert server.finish(server.first_contact_module(), private, "ci", target,
                         private / ".sops.yaml", []) == 1
    assert target.read_bytes() == before


def test_existing_sops_file_gains_complete_identity(private: Path) -> None:
    target = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    target.write_text(
        "node_ci_clients: []\n"
        "node_ci_console_password: \"\"\n",
        encoding="utf-8",
    )
    with secrets_lock(target):
        subprocess.run(
            ["sops", "--config", str(private / ".sops.yaml"), "-e", "-i", str(target)],
            check=True, capture_output=True,
        )
    red = gate(private)
    assert red.returncode == 1
    assert red.stderr.count("в секретах нет node_ci_hy2_") == 4
    assert red.stderr.count("MISSING:") >= 4
    assert server.finish(server.first_contact_module(), private, "ci", target,
                         private / ".sops.yaml", []) == 0
    values = decrypted(target)
    assert all("node_ci_" + name in values for name in server.HY2_NAMES)
    before = target.read_bytes()
    assert server.finish(server.first_contact_module(), private, "ci", target,
                         private / ".sops.yaml", []) == 0
    assert target.read_bytes() == before


def test_reality_identity_is_generated_and_valid(private: Path) -> None:
    target = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    assert server.finish(server.first_contact_module(), private, "ci", target,
                         private / ".sops.yaml", []) == 0
    values = decrypted(target)
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("reality_check", ROOT / "scripts" / "check-reality-keypair.py")
    checker = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(checker)
    private_key = values["node_ci_reality_private_key"]
    public_key = values["node_ci_reality_public_key"]
    assert checker.x25519_public(checker.decode_key(private_key)) == checker.decode_key(public_key)
    checked = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check-reality-keypair.py")],
        input=json.dumps({"private_key": private_key, "public_key": public_key}),
        capture_output=True, text=True, check=True,
    )
    assert checked.stdout.strip() == "Reality key pair check: ok"
    assert re.fullmatch(r"[0-9a-f]{16}", values["node_ci_reality_short_id"])
    before = target.read_bytes()
    assert server.finish(server.first_contact_module(), private, "ci", target,
                         private / ".sops.yaml", []) == 0
    assert target.read_bytes() == before


def test_partial_reality_identity_is_rejected(private: Path) -> None:
    target = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    target.write_text("node_ci_reality_private_key: synthetic\n", encoding="utf-8")
    with secrets_lock(target):
        subprocess.run(["sops", "--config", str(private / ".sops.yaml"), "-e", "-i", str(target)],
                       check=True, capture_output=True)
    before = target.read_bytes()
    assert server.finish(server.first_contact_module(), private, "ci", target,
                         private / ".sops.yaml", []) == 1
    assert target.read_bytes() == before


def test_wizard_keeps_generated_secrets_out_of_proc_argv(
    private: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    original = server.generate_hy2_identity
    markers: list[bytes] = []
    armed = threading.Event()
    done = threading.Event()
    findings: list[str] = []
    samples = [0]

    def capture(sni: str) -> dict[str, str]:
        values = original(sni)
        markers.extend([
            values["hy2_password"].encode(),
            values["hy2_key"].splitlines()[1].encode(),
        ])
        armed.set()
        return values

    def observe() -> None:
        armed.wait(timeout=10)
        while not done.is_set():
            for path in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    args = path.read_bytes()
                except (OSError, PermissionError):
                    continue
                samples[0] += 1
                if any(marker in args for marker in markers):
                    findings.append("generated secret appeared in argv")
            time.sleep(0.001)

    monkeypatch.setattr(server, "generate_hy2_identity", capture)
    watcher = threading.Thread(target=observe, daemon=True)
    watcher.start()
    try:
        assert server.finish(server.first_contact_module(), private, "ci", target,
                             private / ".sops.yaml", ["ci-client"]) == 0
    finally:
        done.set()
        watcher.join(timeout=5)
    assert samples[0] > 0
    assert not findings


def test_generated_identity_connects_and_rejects_foreign_cert(private: Path) -> None:
    if not shutil.which("sing-box") or not shutil.which("curl"):
        pytest.skip("local sing-box/curl binaries unavailable")
    target = private / "inventories" / "group_vars" / "all" / "secrets.sops.yml"
    assert server.finish(server.first_contact_module(), private, "ci", target,
                         private / ".sops.yaml", []) == 0
    values = decrypted(target)
    work = private.parent / "hy2-runtime"
    work.mkdir(mode=0o700)
    (work / "hy2.crt").write_text(values["node_ci_hy2_certificate"], encoding="ascii")
    (work / "hy2.key").write_text(values["node_ci_hy2_key"], encoding="ascii")
    (work / "hy2.key").chmod(0o600)
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROOT / "roles" / "singbox" / "templates")
    )
    env.filters["bool"] = bool
    rendered = env.get_template("config.json.j2").render(
        ansible_managed="synthetic test", singbox_log_level="error",
        singbox_vless_security="reality", singbox_reality_server="xray",
        singbox_hy2_enabled=True, singbox_hy2_port=28443,
        singbox_hy2_up_mbps=10, singbox_hy2_down_mbps=10,
        node_hy2_password=values["node_ci_hy2_password"],
        singbox_config_dir=str(work), singbox_sni_routing_enabled=False,
    )
    server_config = work / "server.json"
    server_config.write_text(rendered, encoding="utf-8")
    client_config = work / "client.json"
    client_config.write_text(
        json.dumps({
            "log": {"level": "error"},
            "inbounds": [{"type": "mixed", "tag": "proxy-in", "listen": "127.0.0.1",
                          "listen_port": 19080}],
            "outbounds": [{
                "type": "hysteria2", "tag": "hy2", "server": "127.0.0.1",
                "server_port": 28443, "password": values["node_ci_hy2_password"],
                "tls": {"enabled": True, "server_name": "vpn-test.invalid",
                        "certificate_public_key_sha256": [
                            values["node_ci_hy2_certificate_public_key_sha256"]],
                        "alpn": ["h3"]},
            }],
            "route": {"final": "hy2"},
        }), encoding="utf-8",
    )
    for config in (server_config, client_config):
        assert subprocess.run(["sing-box", "check", "-c", str(config)],
                              capture_output=True).returncode == 0

    processes = [
        subprocess.Popen([sys.executable, "-m", "http.server", "29081", "--bind",
                          "127.0.0.1"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL),
        subprocess.Popen(["sing-box", "run", "-c", str(server_config)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen(["sing-box", "run", "-c", str(client_config)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    try:
        connected = False
        for _ in range(30):
            result = subprocess.run(
                ["curl", "--fail", "--silent", "--max-time", "2",
                 "--socks5-hostname", "127.0.0.1:19080", "http://127.0.0.1:29081/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                connected = True
                break
            time.sleep(0.2)
        assert connected
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    foreign = server.generate_hy2_identity("vpn-test.invalid")
    foreign_dir = work / "foreign"
    foreign_dir.mkdir(mode=0o700)
    (foreign_dir / "hy2.crt").write_text(foreign["hy2_certificate"], encoding="ascii")
    (foreign_dir / "hy2.key").write_text(foreign["hy2_key"], encoding="ascii")
    (foreign_dir / "hy2.key").chmod(0o600)
    foreign_config = foreign_dir / "server.json"
    foreign_config.write_text(
        rendered.replace(str(work / "hy2.crt"), str(foreign_dir / "hy2.crt"))
                .replace(str(work / "hy2.key"), str(foreign_dir / "hy2.key")),
        encoding="utf-8",
    )
    rejected = subprocess.run(
        [str(ROOT / "tests" / "hy2-reject-foreign-certificate.sh"),
         "sing-box", str(foreign_config), str(client_config), str(work)],
        capture_output=True, text=True,
    )
    assert rejected.returncode == 0
