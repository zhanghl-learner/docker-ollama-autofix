from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from autofix.runtime import offline


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"models": [{"name": "qwen3:0.6b"}]}


def test_offline_report_ready(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "autofix.yml").write_text(
        """version: 1
build: {command: python -m pytest}
runtime:
  docker_image: demo:latest
  ollama_model: qwen3:0.6b
  offline: true
  dependencies_bundled: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(offline, "find_executable", lambda _: "docker")
    monkeypatch.setattr(
        offline.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="29.0", stderr=""),
    )
    monkeypatch.setattr(offline.httpx, "get", lambda *args, **kwargs: FakeResponse())

    report = offline.check_offline_readiness(tmp_path)

    assert report.ready
    assert all(item.ready for item in report.checks)


def test_offline_report_fails_without_manifest(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    report = offline.check_offline_readiness(tmp_path)

    assert not report.ready
    assert any(item.name == "offline-policy" and not item.ready for item in report.checks)


def test_offline_report_uses_configured_build_system(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    (tmp_path / "autofix.yml").write_text(
        """version: 1
build: {system: go}
runtime:
  docker_image: demo:latest
  ollama_model: qwen3:0.6b
  offline: true
  dependencies_bundled: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(offline, "find_executable", lambda _: "docker")
    monkeypatch.setattr(
        offline.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="29.0", stderr=""),
    )
    monkeypatch.setattr(offline.httpx, "get", lambda *args, **kwargs: FakeResponse())

    report = offline.check_offline_readiness(tmp_path)

    build_plan = next(item for item in report.checks if item.name == "build-plan")
    assert report.ready
    assert build_plan.details == "go"


def test_offline_report_describes_configured_build_matrix(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "autofix.yml").write_text(
        """version: 1
build:
  commands: [python -m compileall -q ., python main.py]
runtime:
  docker_image: demo:latest
  ollama_model: qwen3:0.6b
  offline: true
  dependencies_bundled: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(offline, "find_executable", lambda _: "docker")
    monkeypatch.setattr(
        offline.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="29.0", stderr=""),
    )
    monkeypatch.setattr(offline.httpx, "get", lambda *args, **kwargs: FakeResponse())

    report = offline.check_offline_readiness(tmp_path)

    build_plan = next(item for item in report.checks if item.name == "build-plan")
    assert report.ready
    assert build_plan.details == "2 configured steps"

