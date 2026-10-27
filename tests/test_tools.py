from __future__ import annotations

from pathlib import Path

from autofix.runtime import tools


def test_find_executable_prefers_path(monkeypatch) -> None:
    monkeypatch.setattr(tools.shutil, "which", lambda name: f"C:/bin/{name}.exe")

    assert tools.find_executable("docker") == "C:/bin/docker.exe"


def test_find_executable_discovers_per_user_docker(monkeypatch, tmp_path: Path) -> None:
    executable = (
        tmp_path / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    monkeypatch.setattr(tools.shutil, "which", lambda _: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(tools, "_is_windows", lambda: True)

    assert tools.find_executable("docker") == str(executable)
