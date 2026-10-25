"""Find tracked SOPS operations which are not covered by the transaction lock."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shlex
import subprocess

import yaml


COMMAND = re.compile(r"(?<![\w./-])sops(?=\s|$)")
LOCK = re.compile(r"secrets_lock\.py[\"']?\s+(?:\"[^\"]+\"|'[^']+'|\S+)\s+--\s+sops(?:\s|$)")
SELF_LOCK = re.compile(r"exec\s+python3[^\n]*secrets_lock\.py[^\n]*--\s+\"?\$0")
FENCE = re.compile(r"^\s*(```|~~~)")
READ = {"--decrypt", "-d", "decrypt", "--version", "filestatus"}
WRITE = {"-i", "--in-place", "--encrypt", "-e", "encrypt", "--set", "set",
         "--unset", "unset", "updatekeys", "rotate", "-r", "edit"}


def tracked_text(root: Path) -> dict[str, str]:
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
    result = {}
    for raw in names:
        if not raw:
            continue
        name = raw.decode("utf-8", "surrogateescape")
        data = (root / name).read_bytes()
        if b"\0" in data:
            continue
        try:
            result[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return result


def is_read(args: list[str]) -> bool:
    return not WRITE.intersection(args) and any(arg in READ for arg in args)


def shell_args(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=True)
    except ValueError:
        return command.split()


OPTION_VALUES = {
    "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "--close-from"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "xargs": {"-n", "--max-args", "-L", "--max-lines", "-P", "--max-procs", "-I", "--replace", "-s", "--max-chars", "-d", "--delimiter"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
}
WRAPPERS = {"sudo", "env", "command", "xargs", "nohup", "time", "timeout", "nice"}


def wrapper_prefix(before: str) -> bool:
    """Whether the text before sops is only shell command wrappers."""
    tokens = shell_args(before)
    if not tokens:
        return True
    if tokens[0] in {"-", "@", "exec", "then", "do"}:
        tokens.pop(0)
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", tokens[0]):
        tokens.pop(0)
    saw_wrapper = False
    while tokens and tokens[0] in WRAPPERS:
        wrapper = tokens.pop(0)
        saw_wrapper = True
        while tokens and tokens[0].startswith("-"):
            option = tokens.pop(0)
            if wrapper == "command" and option in {"-v", "-V"}:
                return False
            if option in OPTION_VALUES.get(wrapper, set()):
                if not tokens:
                    return False
                tokens.pop(0)
        if wrapper == "env":
            while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", tokens[0]):
                tokens.pop(0)
        elif wrapper == "timeout":
            if not tokens:
                return False
            tokens.pop(0)
    return saw_wrapper and not tokens


def shell_calls(source: str, *, fenced: bool = False):
    inside = not fenced
    lines = source.splitlines()
    logical = []
    start = 1
    for lineno, line in enumerate(lines, 1):
        if fenced and FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            continue
        if not logical:
            start = lineno
        logical.append(line.rstrip().removesuffix("\\"))
        if line.rstrip().endswith("\\"):
            continue
        body = " ".join(logical)
        logical.clear()
        # Strip unquoted comments; command examples and shell prose after # do not run.
        body = re.split(r"(?<!\\)#", body, maxsplit=1)[0]
        for match in COMMAND.finditer(body):
            before = body[:match.start()]
            direct = re.search(
                r"(?:^|[;&|`]|\$\(|\bexec\s+|\bthen\s+|\bdo\s+|--\s+|^\s*[-@]\s+)\s*(?:[A-Za-z_][A-Za-z_0-9]*=\S+\s+)*$", before)
            if not direct and not wrapper_prefix(re.split(r"[;&|`]|\$\(", before)[-1]):
                continue
            tail = body[match.start():]
            args = shell_args(tail)
            yield start, args[1:], bool(LOCK.search(body))


def python_calls(source: str):
    tree = ast.parse(source)
    parents = {}
    assignments = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = parent.value

    def first_sops(value):
        if isinstance(value, (ast.List, ast.Tuple)) and value.elts:
            first = value.elts[0]
            return isinstance(first, ast.Constant) and first.value == "sops"
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            return first_sops(value.left)
        if isinstance(value, ast.Name) and value.id in assignments:
            return first_sops(assignments[value.id])
        return False

    def args_of(value):
        if isinstance(value, (ast.List, ast.Tuple)):
            return [elt.value if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                    else "<dynamic>" for elt in value.elts[1:]]
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            return args_of(value.left) + args_of(value.right)
        if isinstance(value, ast.Name) and value.id in assignments:
            return args_of(assignments[value.id])
        return ["<dynamic>"]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        args = None
        for value in node.args:
            if first_sops(value):
                args = args_of(value)
                break
        first = node.args[0]
        # run("sops", "--encrypt", ...) — command spread over positional arguments;
        # a lone "sops" argument is a name lookup (which, append, count), not a command.
        if args is None and len(node.args) > 1 and isinstance(first, ast.Constant) and first.value == "sops":
            args = [value.value if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    else "<dynamic>" for value in node.args[1:]]
        if args is None and not (isinstance(node.func, ast.Attribute) and node.func.attr == "startswith"):
            if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value.startswith("sops "):
                args = shell_args(first.value)[1:]
        if args is None:
            continue
        current = node
        locked = False
        while current in parents:
            current = parents[current]
            if isinstance(current, (ast.With, ast.AsyncWith)) and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "secrets_lock"
                for item in current.items
            ):
                locked = True
                break
        yield node.lineno, args, locked


def yaml_calls(source: str):
    root = yaml.compose(source)
    if root is None:
        return
    def walk(node):
        if isinstance(node, yaml.ScalarNode) and isinstance(node.value, str):
            for line, args, locked in shell_calls(node.value):
                yield node.start_mark.line + line, args, locked
        elif isinstance(node, yaml.SequenceNode):
            if node.value and isinstance(node.value[0], yaml.ScalarNode) and node.value[0].value == "sops":
                args = [item.value if isinstance(item, yaml.ScalarNode) else "<dynamic>"
                        for item in node.value[1:]]
                yield node.start_mark.line + 1, args, False
            else:
                for item in node.value:
                    yield from walk(item)
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                yield from walk(value)
    yield from walk(root)


def scan(root: Path) -> tuple[list[str], set[str]]:
    sources = tracked_text(root)
    findings = []
    for name, source in sources.items():
        if name.endswith(".py"):
            try:
                calls = list(python_calls(source))
            except SyntaxError:
                calls = None
        elif name.endswith((".yml", ".yaml")):
            try:
                calls = list(yaml_calls(source))
            except yaml.YAMLError:
                calls = None
        elif name.endswith(".md"):
            calls = list(shell_calls(source, fenced=True))
        else:
            calls = list(shell_calls(source))
        if calls is None:
            if "sops" in source:
                line = source.count("\n", 0, source.index("sops")) + 1
                findings.append(f"{name}:{line}: не разобран")
            continue
        self_lock = SELF_LOCK.search(source) if name.endswith(".sh") else None
        self_lock_line = source.count("\n", 0, self_lock.start()) + 1 if self_lock else None
        for line, args, locked in calls:
            if not is_read(args) and not (locked or (self_lock_line is not None and line > self_lock_line)):
                findings.append(f"{name}:{line}: sops operation")
    return sorted(findings), set(sources)
