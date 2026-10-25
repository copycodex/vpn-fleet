"""Every tracked SOPS write is an operation under the shared lock."""

from pathlib import Path
import subprocess
import sys
import uuid

from sops_operation_scan import scan


ROOT = Path(__file__).resolve().parents[1]
SECRET_NAME = "secrets.sops.yml"


def test_all_discovered_sops_writers_use_shared_lock() -> None:
    findings, _ = scan(ROOT)
    assert findings == []


def test_scan_covers_every_tracked_text_file() -> None:
    _, scanned = scan(ROOT)
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    expected = set()
    for raw in names:
        if not raw:
            continue
        name = raw.decode("utf-8", "surrogateescape")
        data = (ROOT / name).read_bytes()
        if b"\0" in data:
            continue
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        expected.add(name)
    assert scanned == expected


def test_operations_in_new_locations(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    directory = tmp_path / uuid.uuid4().hex
    directory.mkdir()
    command = "so" + "ps"
    target = "other.sops.yml"
    cases = {
        "writer.py": f'import subprocess\nsubprocess.run(["{command}", "-e", "-i", "{target}"])\n',
        "Makefile": f'write:\n\t{command} -e -i $(FILE)\n',
        "task.yml": f'- name: write\n  command:\n    argv: [{command}, --set, {target}, key, value]\n',
        "writer.sh": f'#!/bin/sh\n{command} --set {target} key value\n',
        "guide.md": f'```sh\n{command} -e -i {target}\n```\n',
        "dynamic.py": f'import subprocess\nargv = ["{command}"] + flags\nsubprocess.run(argv)\n',
        "varargs.py": f'def run(*args):\n    pass\nrun("{command}", "--encrypt", "--in-place", "{target}")\n',
    }
    for name, body in cases.items():
        (directory / name).write_text(body)
    subprocess.run(["git", "-C", str(tmp_path), "add", str(directory.name)], check=True)
    findings, _ = scan(tmp_path)
    assert {item.split(":", 1)[0] for item in findings} == {
        f"{directory.name}/{name}" for name in cases
    }

    locked = {
        "writer.py": f'from secrets_lock import secrets_lock\nimport subprocess\nwith secrets_lock("{target}"):\n    subprocess.run(["{command}", "-e", "-i", "{target}"])\n',
        "Makefile": f'write:\n\tpython3 scripts/secrets_lock.py $(FILE) -- {command} -e -i $(FILE)\n',
        "task.yml": f'- name: write\n  shell: scripts/secrets_lock.py {target} -- {command} --set {target} key value\n',
        "writer.sh": f'#!/bin/sh\nexec python3 scripts/secrets_lock.py "$TARGET" -- "$0" "$@"\n{command} --set {target} key value\n',
        "guide.md": f'```sh\npython3 scripts/secrets_lock.py {target} -- {command} -e -i {target}\n```\n',
        "dynamic.py": f'from secrets_lock import secrets_lock\nimport subprocess\nwith secrets_lock("{target}"):\n    argv = ["{command}"] + flags\n    subprocess.run(argv)\n',
        "varargs.py": f'from secrets_lock import secrets_lock\ndef run(*args):\n    pass\nwith secrets_lock("{target}"):\n    run("{command}", "--encrypt", "--in-place", "{target}")\n',
    }
    for name, body in locked.items():
        (directory / name).write_text(body)
    assert scan(tmp_path)[0] == []


def test_write_flag_takes_precedence_over_read_flag(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    command = "so" + "ps"
    source = tmp_path / "mixed.sh"
    source.write_text(f"{command} --decrypt --set other.sops.yml key value\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", source.name], check=True)
    assert scan(tmp_path)[0] == ["mixed.sh:1: sops operation"]


def test_read_operations_in_each_form(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    directory = tmp_path / uuid.uuid4().hex
    directory.mkdir()
    command = "so" + "ps"
    cases = {
        "read.py": f'import subprocess\nsubprocess.run(["{command}", "--decrypt", "file"])\n',
        "Makefile": f'read:\n\t{command} -d file > /dev/null\n',
        "task.yml": f'- command:\n    argv: [{command}, --version]\n',
        "read.sh": f'#!/bin/sh\n{command} --decrypt file\n',
        "guide.md": f'```sh\n{command} --version\n```\n',
    }
    for name, body in cases.items():
        (directory / name).write_text(body)
    subprocess.run(["git", "-C", str(tmp_path), "add", str(directory.name)], check=True)
    assert scan(tmp_path)[0] == []


def test_python_command_literals_in_any_call(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    command = "so" + "ps"
    source = tmp_path / "calls.py"
    source.write_text(
        f'from subprocess import run\nrun(["{command}", "-e", "file"])\n'
        f'import os\nos.execvp("{command}", ["{command}", "-e", "file"])\n'
        f'import pexpect\npexpect.spawn("{command} -e file")\n'
        f'def helper(argv): pass\nhelper(("{command}", "-e", "file"))\n'
        f'argv = ["{command}", "-e", "file"]\nhelper(argv)\n'
        f'commands = ("ssh", "host", "{command}")\n'
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", source.name], check=True)
    assert scan(tmp_path)[0] == sorted([
        "calls.py:2: sops operation", "calls.py:4: sops operation",
        "calls.py:6: sops operation", "calls.py:8: sops operation",
        "calls.py:10: sops operation",
    ])
    source.write_text(
        'from secrets_lock import secrets_lock\n'
        'with secrets_lock("file"):\n'
        + "".join("    " + line + "\n" for line in source.read_text().splitlines()
                  if line.startswith(("run(", "os.execvp(", "pexpect.spawn(", "helper(", "argv =")))
    )
    assert scan(tmp_path)[0] == []


def test_unparsed_python_and_yaml_are_findings(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    command = "so" + "ps"
    (tmp_path / "broken.py").write_text(f'if True print("{command}")\n')
    (tmp_path / "broken.yml").write_text(f'command: [{command}\n')
    subprocess.run(["git", "-C", str(tmp_path), "add", "broken.py", "broken.yml"], check=True)
    assert scan(tmp_path)[0] == [
        "broken.py:1: не разобран", "broken.yml:1: не разобран",
    ]


def test_shell_self_lock_only_covers_later_calls(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    command = "so" + "ps"
    source = tmp_path / "writer.sh"
    source.write_text(
        f'{command} -e file\n'
        'exec python3 scripts/secrets_lock.py "$TARGET" -- "$0" "$@"\n'
        f'{command} -e file\n'
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", source.name], check=True)
    assert scan(tmp_path)[0] == ["writer.sh:1: sops operation"]


def test_shell_wrappers_leave_sops_in_command_position(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    command = "so" + "ps"
    wrappers = [
        "sudo -u root", "env -i FLAG=1", "command -p", "xargs -0 -n 1",
        "nohup", "time -p", "timeout -s TERM 5", "nice -n 5",
        "sudo -E env FLAG=1 timeout 5",
    ]
    source = tmp_path / "wrapped.sh"
    source.write_text("".join(f"{prefix} {command} -e file\n" for prefix in wrappers))
    subprocess.run(["git", "-C", str(tmp_path), "add", source.name], check=True)
    assert scan(tmp_path)[0] == [
        f"wrapped.sh:{line}: sops operation" for line in range(1, len(wrappers) + 1)
    ]
    source.write_text(f"command -v {command}\ncommand -V {command}\n")
    assert scan(tmp_path)[0] == []


def test_python_and_shell_transactions_serialize(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from secrets_lock import LOCKS_ENV, secrets_lock
    import os

    child_env = os.environ.copy()
    child_env.pop(LOCKS_ENV, None)
    target = tmp_path / SECRET_NAME
    marker = tmp_path / "entered"
    with secrets_lock(target):
        child = subprocess.Popen([
            sys.executable, str(ROOT / "scripts" / "secrets_lock.py"), str(target),
            "--", sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ], env=child_env)
        try:
            assert child.wait(timeout=0.2) is None
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("second writer entered while lock was held")
        assert not marker.exists()
    assert child.wait(timeout=5) == 0
    assert marker.exists()


def test_make_secrets_edit_locks_selected_file(tmp_path: Path) -> None:
    import os

    from scripts.secrets_lock import lock_path_for

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "sops"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import fcntl, os, sys\n"
        "from pathlib import Path\n"
        "from scripts.secrets_lock import lock_path_for\n"
        "target = Path(sys.argv[-1])\n"
        "with lock_path_for(target).open('a+b') as lock:\n"
        "    try:\n"
        "        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    except BlockingIOError:\n"
        "        state = 'held'\n"
        "    else:\n"
        "        state = 'free'\n"
        "Path(os.environ['FAKE_SOPS_RESULT']).write_text(state)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    key = tmp_path / "synthetic-age-key"
    key.write_text("synthetic")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["PYTHONPATH"] = str(ROOT)
    for make_arg, target in (
        ("FILE", tmp_path / "other.sops.yml"),
        ("SECRETS_FILE", tmp_path / SECRET_NAME),
    ):
        result = tmp_path / f"{make_arg}.result"
        env["FAKE_SOPS_RESULT"] = str(result)
        args = ["make", "--no-print-directory", "secrets-edit",
                f"SOPS_AGE_KEY_FILE={key}", f"{make_arg}={target}"]
        completed = subprocess.run(args, cwd=ROOT, env=env, check=True,
                                   capture_output=True, text=True, timeout=5)
        assert completed.returncode == 0
        assert result.read_text() == "held"
        assert lock_path_for(target).exists()


def test_nested_lock_same_target_finishes_and_other_target_waits(tmp_path: Path) -> None:
    import os
    import time

    from scripts.secrets_lock import LOCKS_ENV, secrets_lock

    script = ROOT / "scripts" / "secrets_lock.py"
    same = tmp_path / "same.sops.yml"
    other = tmp_path / "other.sops.yml"
    marker = tmp_path / "entered"
    subprocess.run([
        sys.executable, str(script), str(same), "--",
        sys.executable, str(script), str(same), "--",
        sys.executable, "-c", "pass",
    ], check=True, timeout=5, capture_output=True)

    independent_env = os.environ.copy()
    independent_env.pop(LOCKS_ENV, None)
    with secrets_lock(other):
        child = subprocess.Popen([
            sys.executable, str(script), str(same), "--",
            sys.executable, str(script), str(other), "--",
            sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ], env=independent_env)
        try:
            time.sleep(0.2)
            assert child.poll() is None
            assert not marker.exists()
        finally:
            if child.poll() is not None:
                child.wait()
    assert child.wait(timeout=5) == 0
    assert marker.exists()
