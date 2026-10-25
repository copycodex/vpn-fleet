"""Lossless text edits for the partially encrypted first-entry YAML map."""

from __future__ import annotations

import json
import re


class FirstEntryEditError(ValueError):
    pass


def _indent(line: str) -> int:
    prefix = line[: len(line) - len(line.lstrip(" "))]
    if "\t" in prefix:
        raise FirstEntryEditError("tabs are not allowed in YAML indentation")
    return len(prefix)


def _content(line: str) -> str:
    return line.rstrip("\r\n")


def _is_layout(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _locate(text: str, node: str) -> tuple[list[str], int, int, int, int]:
    lines = text.splitlines(keepends=True)
    root = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"[ ]*first_entry:[ ]*(?:\{\}[ ]*)?(?:#.*)?", _content(line))
        ),
        None,
    )
    if root is None:
        raise FirstEntryEditError("first_entry mapping not found")
    root_indent = _indent(lines[root])
    region_end = len(lines)
    for index in range(root + 1, len(lines)):
        if not _is_layout(lines[index]) and _indent(lines[index]) <= root_indent:
            region_end = index
            break

    node_pattern = re.compile(rf"[ ]*{re.escape(node)}:[ ]*(?:#.*)?")
    header = next(
        (
            index
            for index in range(root + 1, region_end)
            if _indent(lines[index]) > root_indent
            and node_pattern.fullmatch(_content(lines[index]))
        ),
        None,
    )
    if header is None:
        raise FirstEntryEditError("node mapping not found")
    node_indent = _indent(lines[header])
    block_end = region_end
    for index in range(header + 1, region_end):
        if not _is_layout(lines[index]) and _indent(lines[index]) <= node_indent:
            block_end = index
            break
    return lines, root, header, block_end, region_end


def replace_password(text: str, node: str, password: str) -> str:
    lines, _, header, block_end, _ = _locate(text, node)
    password_line = None
    for index in range(header + 1, block_end):
        if re.match(r"[ ]+password:[ ]*", _content(lines[index])):
            if password_line is not None:
                raise FirstEntryEditError("duplicate password field")
            password_line = index
    if password_line is None:
        raise FirstEntryEditError("password field not found")
    newline = "\r\n" if lines[password_line].endswith("\r\n") else "\n"
    prefix = re.match(r"([ ]+password:)[ ]*", _content(lines[password_line]))
    assert prefix is not None
    lines[password_line] = f"{prefix.group(1)} {json.dumps(password)}{newline}"
    return "".join(lines)


def remove_entry(text: str, node: str) -> str:
    lines, root, header, block_end, region_end = _locate(text, node)
    node_indent = _indent(lines[header])
    other_entry = any(
        index != header
        and not _is_layout(lines[index])
        and _indent(lines[index]) == node_indent
        and re.fullmatch(r"[ ]*[A-Za-z0-9_-]+:[ ]*(?:#.*)?", _content(lines[index]))
        for index in range(root + 1, region_end)
    )

    # Documentation comments and blank layout lines survive even when they are
    # located inside the removed mapping. Only YAML data lines are deleted.
    for index in range(header, block_end):
        if not _is_layout(lines[index]):
            lines[index] = ""

    if not other_entry:
        original = lines[root]
        newline = "\r\n" if original.endswith("\r\n") else "\n"
        body = _content(original)
        comment = ""
        if "#" in body:
            before, after = body.split("#", 1)
            spacing = before[len(before.rstrip(" ")) :]
            comment = f"{spacing}#{after}"
        lines[root] = " " * _indent(original) + "first_entry: {}" + comment + newline
    return "".join(lines)


def insert_entry(
    text: str,
    node: str,
    *,
    user: str,
    port: int,
    method: str,
    password: str = "",
) -> str:
    """Add a node record to the map without touching anything already there.

    The map is documentation as much as data: comments, blank lines and the
    sops block outlive every edit, so the record is spliced in as text right
    after the `first_entry:` header instead of reserialising the document.
    """
    if method not in ("password", "key"):
        raise FirstEntryEditError("method must be password or key")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", node):
        raise FirstEntryEditError("node name must be [A-Za-z0-9_-]+")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise FirstEntryEditError("port must be an integer in 1..65535")

    try:
        _locate(text, node)
    except FirstEntryEditError as error:
        if str(error) != "node mapping not found":
            raise
    else:
        raise FirstEntryEditError("node mapping already exists")

    lines = text.splitlines(keepends=True)
    root = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"[ ]*first_entry:[ ]*(?:\{\}[ ]*)?(?:#.*)?", _content(line))
        ),
        None,
    )
    if root is None:
        raise FirstEntryEditError("first_entry mapping not found")

    root_indent = _indent(lines[root])
    newline = "\r\n" if lines[root].endswith("\r\n") else "\n"

    # Step of indentation is taken from the neighbours, so a hand-written map
    # keeps its own shape; 4 is the shape of the production file.
    region_end = len(lines)
    for index in range(root + 1, len(lines)):
        if not _is_layout(lines[index]) and _indent(lines[index]) <= root_indent:
            region_end = index
            break
    step = next(
        (
            _indent(lines[index]) - root_indent
            for index in range(root + 1, region_end)
            if not _is_layout(lines[index])
            and _indent(lines[index]) > root_indent
            and re.fullmatch(r"[ ]*[A-Za-z0-9_-]+:[ ]*(?:#.*)?", _content(lines[index]))
        ),
        4,
    )

    # `first_entry: {}` is the exhausted form: it has to open up again before a
    # record can live under it. A trailing comment on that line is kept.
    body = _content(lines[root])
    if "{}" in body:
        comment = ""
        if "#" in body:
            before, after = body.split("#", 1)
            spacing = before[len(before.rstrip(" ")) :]
            comment = f"{spacing}#{after}"
        lines[root] = " " * root_indent + "first_entry:" + comment + newline

    entry_indent = " " * (root_indent + step)
    field_indent = " " * (root_indent + 2 * step)
    record = [
        f"{entry_indent}{node}:{newline}",
        f"{field_indent}user: {json.dumps(user)}{newline}",
        f"{field_indent}port: {port}{newline}",
        f"{field_indent}method: {json.dumps(method)}{newline}",
    ]
    if method == "password":
        record.append(f"{field_indent}password: {json.dumps(password)}{newline}")
    lines[root + 1 : root + 1] = record
    return "".join(lines)
