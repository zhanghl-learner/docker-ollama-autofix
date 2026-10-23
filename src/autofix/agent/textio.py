from __future__ import annotations

from pathlib import Path


def read_utf8_exact(path: Path) -> str:
    """Read UTF-8 text without universal-newline translation."""

    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def write_utf8_exact(path: Path, content: str) -> None:
    """Write UTF-8 text without translating newline characters."""

    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(content)
