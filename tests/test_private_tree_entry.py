"""The first-entry map works from a fresh checkout and an unrelated cwd."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("stale_sample", [False, True])
def test_first_entry_fresh_process(tmp_path: Path, stale_sample: bool):
    key = tmp_path / "age.txt"
    subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, check=True)
    recipient = subprocess.run(["age-keygen", "-y", str(key)], capture_output=True,
                               text=True, check=True).stdout.strip()
    private = tmp_path / "private"
    private.mkdir()
    config = private / ".sops.yaml"
    config.write_text("creation_rules:\n  - path_regex: first-entry[.]sops[.]yml$\n"
                      f"    age: {recipient}\n")
    target = private / "inventories" / "first-entry.sops.yml"
    if stale_sample:
        target.parent.mkdir()
        target.write_bytes((ROOT / "inventories/first-entry.sops.yml.example")
                           .read_bytes())

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["SOPS_AGE_KEY_FILE"] = str(key)
    code = ("import runpy, sys; from pathlib import Path; "
            "module = runpy.run_path(sys.argv[1], run_name='fresh_entry'); "
            "reason = module['ensure_first_entry'](Path(sys.argv[2]), "
            "Path(sys.argv[3])); assert reason is None, reason")
    subprocess.run([sys.executable, "-c", code, str(ROOT / "wizard/private_tree.py"),
                    str(private), str(config)], cwd=tmp_path, env=env,
                   capture_output=True, text=True, check=True)
    assert "sops:" in target.read_text()
    decrypted = subprocess.run(["sops", "--config", str(config), "-d", str(target)],
                               env=env, capture_output=True, text=True, check=True)
    assert "first_entry: {}" in decrypted.stdout
