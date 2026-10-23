from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autofix.runtime.tools import find_executable


class BundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class BundleFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BundleReport:
    path: Path
    docker_image: str
    ollama_model: str
    files: tuple[BundleFile, ...]

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "docker_image": self.docker_image,
            "ollama_model": self.ollama_model,
            "total_size": self.total_size,
            "files": [asdict(item) for item in self.files],
        }


def _run(argv: list[str], *, timeout: float) -> str:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError(f"Unable to run {argv[0]}: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise BundleError(f"Command failed ({' '.join(argv[:3])}): {details}")
    return result.stdout


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record(root: Path, relative: str) -> BundleFile:
    path = root / relative
    return BundleFile(relative, path.stat().st_size, _sha256(path))


def _safe_member(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise BundleError("Bundle manifest contains an invalid path")
    member = Path(relative)
    if member.is_absolute() or ".." in member.parts:
        raise BundleError(f"Unsafe bundle path: {relative}")
    resolved = (root / member).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise BundleError(f"Bundle path escapes its directory: {relative}") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise BundleError(f"Bundle file is missing or unsafe: {relative}")
    return resolved


def _model_definition(ollama: str, model: str) -> tuple[str, Path]:
    modelfile = _run([ollama, "show", "--modelfile", model], timeout=30)
    match = re.search(r"(?m)^FROM\s+(.+?)\s*$", modelfile)
    if not match:
        raise BundleError("Ollama did not report a FROM file for the model")
    raw = match.group(1).strip().strip('"')
    source = Path(raw).expanduser()
    if not source.is_file():
        raise BundleError(f"Ollama model blob was not found: {source}")
    portable = modelfile[: match.start()] + "FROM ./model.gguf" + modelfile[match.end() :]
    return portable, source


def _installer_script() -> str:
    return r'''param()
$ErrorActionPreference = "Stop"
$bundleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifestPath = Join-Path $bundleRoot "bundle-manifest.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$rootPrefix = [IO.Path]::GetFullPath($bundleRoot).TrimEnd('\') + '\'

Write-Host "Verifying bundle checksums..."
foreach ($item in $manifest.files) {
    $relative = [string]$item.path
    if ([IO.Path]::IsPathRooted($relative) -or $relative.Contains("\") -or $relative.Split('/') -contains "..") {
        throw "Unsafe bundle path: $relative"
    }
    $file = [IO.Path]::GetFullPath((Join-Path $bundleRoot ($relative.Replace('/', '\'))))
    if (-not $file.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Bundle path escapes its directory: $relative"
    }
    $info = Get-Item -LiteralPath $file
    if ($info.Length -ne [long]$item.size) { throw "Bundle size mismatch: $relative" }
    $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne [string]$item.sha256) { throw "Bundle checksum mismatch: $relative" }
}

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) { throw "python is not installed or not available in PATH" }

$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerExe = if ($dockerCommand) { $dockerCommand.Source } else { $null }
if (-not $dockerExe) {
    $dockerCandidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"),
        (Join-Path $env:LOCALAPPDATA "Docker\resources\bin\docker.exe"),
        (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe")
    )
    $dockerExe = $dockerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $dockerExe) { throw "Docker Desktop was not found" }

$ollamaCommand = Get-Command ollama -ErrorAction SilentlyContinue
$ollamaExe = if ($ollamaCommand) { $ollamaCommand.Source } else { $null }
if (-not $ollamaExe) {
    $ollamaCandidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path -LiteralPath $ollamaCandidate) { $ollamaExe = $ollamaCandidate }
}
if (-not $ollamaExe) { throw "Ollama was not found" }

& python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) { throw "This Windows wheelhouse requires Python 3.11" }

Write-Host "Installing AutoFix from the offline wheelhouse..."
& python -m pip install --upgrade --no-index --find-links (Join-Path $bundleRoot "wheelhouse") autofix-cli
if ($LASTEXITCODE -ne 0) { throw "AutoFix installation failed" }

Write-Host "Loading Docker image..."
& $dockerExe image load --input (Join-Path $bundleRoot "docker\image.tar")
if ($LASTEXITCODE -ne 0) { throw "Docker image import failed" }

Write-Host "Creating Ollama model..."
& $ollamaExe create ([string]$manifest.ollama_model) --file (Join-Path $bundleRoot "ollama\Modelfile")
if ($LASTEXITCODE -ne 0) { throw "Ollama model import failed" }

Write-Host "AutoFix offline runtime installed successfully."
Write-Host "Copy autofix.yml into the project you want to repair, then run: autofix offline-check ."
'''


def create_bundle(
    output: Path | str,
    *,
    docker_image: str,
    ollama_model: str,
    config_path: Path | str,
    project_root: Path | str | None = None,
    include_cli: bool = True,
) -> BundleReport:
    destination = Path(output).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    if destination.exists():
        raise BundleError(f"Output already exists: {destination}")
    if not config.is_file():
        raise BundleError(f"Configuration file not found: {config}")
    docker = find_executable("docker")
    ollama = find_executable("ollama")
    if not docker or not ollama:
        missing = "Docker" if not docker else "Ollama"
        raise BundleError(f"{missing} is not installed or not available in PATH")

    _run([docker, "image", "inspect", docker_image], timeout=30)
    modelfile, model_blob = _model_definition(ollama, ollama_model)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        (staging / "docker").mkdir()
        (staging / "ollama").mkdir()
        shutil.copy2(config, staging / "autofix.yml")
        (staging / "ollama" / "Modelfile").write_text(modelfile, encoding="utf-8")
        shutil.copy2(model_blob, staging / "ollama" / "model.gguf")
        _run(
            [docker, "image", "save", "--output", str(staging / "docker" / "image.tar"), docker_image],
            timeout=1800,
        )
        version = 1
        if include_cli:
            source_root = Path(project_root).expanduser().resolve() if project_root else config.parent
            if not (source_root / "pyproject.toml").is_file():
                raise BundleError(f"AutoFix pyproject.toml not found: {source_root}")
            (staging / "wheelhouse").mkdir()
            _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-build-isolation",
                    "--prefer-binary",
                    "--wheel-dir",
                    str(staging / "wheelhouse"),
                    str(source_root),
                ],
                timeout=1800,
            )
            (staging / "install.ps1").write_text(_installer_script(), encoding="utf-8")
            documentation = (
                (source_root / "README.md", "README.md"),
                (source_root / "CHANGELOG.md", "CHANGELOG.md"),
                (source_root / "docs" / "USAGE_ZH.md", "USAGE_ZH.md"),
                (source_root / "docs" / "ARCHITECTURE_ZH.md", "ARCHITECTURE_ZH.md"),
                (
                    source_root / "docs" / "MULTILANG_VALIDATION_ZH.md",
                    "MULTILANG_VALIDATION_ZH.md",
                ),
            )
            available_docs = tuple(item for item in documentation if item[0].is_file())
            if available_docs:
                docs_dir = staging / "docs"
                docs_dir.mkdir()
                for source, name in available_docs:
                    shutil.copy2(source, docs_dir / name)
            version = 2
        relative_files = tuple(
            path.relative_to(staging).as_posix()
            for path in sorted(staging.rglob("*"))
            if path.is_file()
        )
        files = tuple(_record(staging, item) for item in relative_files)
        manifest = {
            "format": "autofix-runtime-bundle",
            "version": version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "docker_image": docker_image,
            "ollama_model": ollama_model,
            "files": [asdict(item) for item in files],
        }
        (staging / "bundle-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return BundleReport(destination, docker_image, ollama_model, files)


def verify_bundle(bundle: Path | str) -> BundleReport:
    root = Path(bundle).expanduser().resolve()
    manifest_path = root / "bundle-manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"Unable to read bundle manifest: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != "autofix-runtime-bundle" or data.get("version") not in {1, 2}:
        raise BundleError("Unsupported bundle manifest")
    docker_image = data.get("docker_image")
    ollama_model = data.get("ollama_model")
    raw_files = data.get("files")
    if not isinstance(docker_image, str) or not docker_image or not isinstance(ollama_model, str) or not ollama_model:
        raise BundleError("Bundle manifest has invalid runtime names")
    if not isinstance(raw_files, list) or not raw_files:
        raise BundleError("Bundle manifest has no files")
    files: list[BundleFile] = []
    seen: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256"}:
            raise BundleError("Bundle manifest has an invalid file record")
        relative, size, expected = raw["path"], raw["size"], raw["sha256"]
        if not isinstance(relative, str) or relative in seen:
            raise BundleError("Bundle manifest contains a duplicate or invalid path")
        if not isinstance(size, int) or size < 0 or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise BundleError(f"Bundle manifest has invalid metadata for {relative}")
        path = _safe_member(root, relative)
        actual_size = path.stat().st_size
        actual_hash = _sha256(path)
        if actual_size != size or actual_hash != expected:
            raise BundleError(f"Bundle checksum mismatch: {relative}")
        seen.add(relative)
        files.append(BundleFile(relative, size, expected))
    required = {"autofix.yml", "docker/image.tar", "ollama/Modelfile", "ollama/model.gguf"}
    if data["version"] == 2:
        required.add("install.ps1")
        if not any(item.startswith("wheelhouse/") and item.endswith(".whl") for item in seen):
            raise BundleError("Bundle does not contain an offline Python wheelhouse")
    if not required.issubset(seen):
        raise BundleError("Bundle is missing required runtime files")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual != seen:
        raise BundleError("Bundle contains unlisted or missing payload files")
    return BundleReport(root, docker_image, ollama_model, tuple(files))


def install_bundle(bundle: Path | str) -> BundleReport:
    report = verify_bundle(bundle)
    docker = find_executable("docker")
    ollama = find_executable("ollama")
    if not docker or not ollama:
        missing = "Docker" if not docker else "Ollama"
        raise BundleError(f"{missing} is not installed or not available in PATH")
    _run(
        [docker, "image", "load", "--input", str(report.path / "docker" / "image.tar")],
        timeout=1800,
    )
    _run(
        [ollama, "create", report.ollama_model, "--file", str(report.path / "ollama" / "Modelfile")],
        timeout=1800,
    )
    return report
