from __future__ import annotations

import json
from pathlib import Path

import pytest

from autofix.runtime import bundle


def _fake_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, list[list[str]]]:
    model_blob = tmp_path / "source-model.gguf"
    model_blob.write_bytes(b"model-weights")
    calls: list[list[str]] = []

    monkeypatch.setattr(bundle, "find_executable", lambda name: f"/{name}")

    def fake_run(argv: list[str], *, timeout: float) -> str:
        calls.append(argv)
        if argv[1:3] == ["show", "--modelfile"]:
            return f'# generated\nFROM "{model_blob}"\nPARAMETER temperature 0\n'
        if argv[1:4] == ["image", "save", "--output"]:
            Path(argv[4]).write_bytes(b"docker-archive")
        if argv[1:4] == ["-m", "pip", "wheel"]:
            wheel_dir = Path(argv[argv.index("--wheel-dir") + 1])
            (wheel_dir / "autofix_cli-0.12.0-py3-none-any.whl").write_bytes(b"wheel")
        return "ok"

    monkeypatch.setattr(bundle, "_run", fake_run)
    return model_blob, calls


def test_create_and_verify_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, calls = _fake_runtime(monkeypatch, tmp_path)
    config = tmp_path / "autofix.yml"
    config.write_text("version: 1\n", encoding="utf-8")
    output = tmp_path / "portable"

    created = bundle.create_bundle(
        output,
        docker_image="autofix-python:3.11",
        ollama_model="qwen3:0.6b",
        config_path=config,
        include_cli=False,
    )
    verified = bundle.verify_bundle(output)

    assert created.total_size == verified.total_size
    assert (output / "ollama" / "model.gguf").read_bytes() == b"model-weights"
    assert "FROM ./model.gguf" in (output / "ollama" / "Modelfile").read_text(encoding="utf-8")
    assert any(call[1:3] == ["image", "save"] for call in calls)


def test_verify_rejects_tampered_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_runtime(monkeypatch, tmp_path)
    config = tmp_path / "autofix.yml"
    config.write_text("version: 1\n", encoding="utf-8")
    output = tmp_path / "portable"
    bundle.create_bundle(
        output, docker_image="demo:1", ollama_model="demo:1", config_path=config, include_cli=False
    )
    (output / "ollama" / "model.gguf").write_bytes(b"changed")

    with pytest.raises(bundle.BundleError, match="checksum mismatch"):
        bundle.verify_bundle(output)


def test_v2_bundle_contains_bootstrap_installer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, calls = _fake_runtime(monkeypatch, tmp_path)
    config = tmp_path / "autofix.yml"
    config.write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    output = tmp_path / "portable"

    bundle.create_bundle(
        output,
        docker_image="demo:1",
        ollama_model="demo:1",
        config_path=config,
        project_root=tmp_path,
    )
    report = bundle.verify_bundle(output)
    manifest = json.loads((output / "bundle-manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == 2
    assert (output / "install.ps1").is_file()
    assert any(item.path.startswith("wheelhouse/") for item in report.files)
    wheel_call = next(call for call in calls if call[1:4] == ["-m", "pip", "wheel"])
    assert "--no-build-isolation" in wheel_call


def test_v2_bundle_includes_available_documentation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_runtime(monkeypatch, tmp_path)
    config = tmp_path / "autofix.yml"
    config.write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("# Changes\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "USAGE_ZH.md").write_text("# 使用\n", encoding="utf-8")
    (docs / "ARCHITECTURE_ZH.md").write_text("# 架构\n", encoding="utf-8")
    (docs / "MULTILANG_VALIDATION_ZH.md").write_text("# 多语言验证\n", encoding="utf-8")
    output = tmp_path / "portable"

    bundle.create_bundle(
        output,
        docker_image="demo:1",
        ollama_model="demo:1",
        config_path=config,
        project_root=tmp_path,
    )
    bundle.verify_bundle(output)

    assert (output / "docs" / "README.md").read_text(encoding="utf-8") == "# Demo\n"
    assert (output / "docs" / "CHANGELOG.md").read_text(encoding="utf-8") == "# Changes\n"
    assert (output / "docs" / "USAGE_ZH.md").read_text(encoding="utf-8") == "# 使用\n"
    assert (output / "docs" / "ARCHITECTURE_ZH.md").read_text(encoding="utf-8") == "# 架构\n"
    assert (output / "docs" / "MULTILANG_VALIDATION_ZH.md").read_text(encoding="utf-8") == "# 多语言验证\n"


def test_verify_rejects_path_traversal(tmp_path: Path) -> None:
    root = tmp_path / "portable"
    root.mkdir()
    manifest = {
        "format": "autofix-runtime-bundle",
        "version": 1,
        "docker_image": "demo:1",
        "ollama_model": "demo:1",
        "files": [{"path": "../outside", "size": 0, "sha256": "0" * 64}],
    }
    (root / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(bundle.BundleError, match="Unsafe bundle path"):
        bundle.verify_bundle(root)


def test_install_verifies_then_imports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, calls = _fake_runtime(monkeypatch, tmp_path)
    config = tmp_path / "autofix.yml"
    config.write_text("version: 1\n", encoding="utf-8")
    output = tmp_path / "portable"
    bundle.create_bundle(
        output, docker_image="demo:1", ollama_model="demo:1", config_path=config, include_cli=False
    )
    calls.clear()

    bundle.install_bundle(output)

    assert calls[0][1:3] == ["image", "load"]
    assert calls[1][1:3] == ["create", "demo:1"]
