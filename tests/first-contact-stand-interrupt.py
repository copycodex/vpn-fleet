"""sitecustomize hook: crash before and after each SOPS or SSH effect."""
import atexit
import os
import subprocess
import pexpect

count = 0
limit = int(os.environ.get("STAND_K", "0"))
counter_file = os.environ.get("STAND_COUNTER_FILE")


def effect(kind, phase):
    global count
    count += 1
    if counter_file:
        with open(counter_file, "w", encoding="ascii") as out:
            out.write(f"{count}|{kind}|{phase}\n")
    if count == limit:
        print(f"stand interrupt: effect={count} kind={kind} phase={phase}", flush=True)
        os._exit(97)


original_run = subprocess.run


def run(*args, **kwargs):
    command = args[0] if args else kwargs.get("args", [])
    name = os.path.basename(str(command[0])) if isinstance(command, (list, tuple)) and command else ""
    kind = ("sops-write" if name == "sops" and "--decrypt" not in command and "--encrypt" not in command
            else name if name in ("ssh", "ssh-keyscan") else None)
    if kind:
        effect(kind, "before")
    result = original_run(*args, **kwargs)
    if kind:
        effect(kind, "after")
    return result


subprocess.run = run
original_spawn = pexpect.spawn


def spawn(*args, **kwargs):
    is_ssh = bool(args and os.path.basename(str(args[0])) == "ssh")
    if is_ssh:
        effect("ssh-session", "before")
    child = original_spawn(*args, **kwargs)
    if is_ssh:
        original_close = child.close
        closed = False

        def close(*close_args, **close_kwargs):
            nonlocal closed
            result = original_close(*close_args, **close_kwargs)
            if not closed:
                closed = True
                effect("ssh-session", "after")
            return result

        child.close = close
    return child


pexpect.spawn = spawn
atexit.register(lambda: print(f"stand effects: {count}", flush=True))
