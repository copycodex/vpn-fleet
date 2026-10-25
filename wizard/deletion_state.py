"""Local, untracked state for an interrupted server deletion."""

import json
import os
from pathlib import Path
import tempfile


def marker_path(private: Path, node: str) -> Path:
    return private / ".wizard-deletions" / f"{node}.json"


def marked_nodes(private: Path) -> set[str]:
    return {path.stem for path in (private / ".wizard-deletions").glob("*.json")}


def read_marker(private: Path, node: str) -> dict | None:
    path = marker_path(private, node)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("node") != node or not isinstance(
        data.get("address"), str
    ):
        raise ValueError("invalid deletion marker")
    return data


def write_marker(private: Path, node: str, address: str) -> None:
    path = marker_path(private, node)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{node}.", dir=path.parent)
    pending = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"node": node, "address": address}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)
