"""Locate executables through PATH and known Windows installation folders."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _is_windows() -> bool:
    """Return whether Windows-only executable fallback paths should be used."""

    return os.name == "nt"


def _windows_candidates(name: str) -> tuple[Path, ...]:
    local = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates: list[Path] = []
    if name.lower() == "docker":
        if local:
            local_path = Path(local)
            candidates.extend(
                (
                    local_path / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe",
                    local_path / "Docker" / "resources" / "bin" / "docker.exe",
                )
            )
        candidates.append(
            Path(program_files) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
        )
    elif name.lower() == "ollama" and local:
        candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
    return tuple(candidates)


def find_executable(name: str) -> str | None:
    """Resolve a tool from PATH or common per-user Windows install locations."""

    found = shutil.which(name)
    if found:
        return found
    if _is_windows():
        for candidate in _windows_candidates(name):
            if candidate.is_file():
                return str(candidate)
    return None
