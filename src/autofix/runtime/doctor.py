"""Inspect local tools, Docker, and the Ollama service for CLI diagnostics."""

from __future__ import annotations

import platform
import subprocess
from typing import Any

import httpx

from autofix.runtime.tools import find_executable


def _tool(name: str, version_args: tuple[str, ...] = ("--version",)) -> dict[str, Any]:
    executable = find_executable(name)
    if executable is None:
        return {"available": False, "path": None, "version": None}
    try:
        completed = subprocess.run(
            [executable, *version_args], capture_output=True, text=True, timeout=5, check=False
        )
        output = (completed.stdout or completed.stderr).strip().splitlines()
        version = output[0] if output else "available"
    except (OSError, subprocess.SubprocessError) as exc:
        version = f"unable to query: {exc}"
    return {"available": True, "path": executable, "version": version}


def _ollama_service(base_url: str) -> dict[str, Any]:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=1.5)
        response.raise_for_status()
        models = [item.get("name", "unknown") for item in response.json().get("models", [])]
        return {"available": True, "url": base_url, "models": models, "error": None}
    except (httpx.HTTPError, ValueError) as exc:
        return {"available": False, "url": base_url, "models": [], "error": str(exc)}


def check_environment(ollama_url: str = "http://127.0.0.1:11434") -> dict[str, Any]:
    """Return readiness details for every runtime component AutoFix can use."""

    return {
        "platform": platform.platform(),
        "python": _tool("python"),
        "git": _tool("git"),
        "docker": _tool("docker"),
        "podman": _tool("podman"),
        "ollama": _tool("ollama"),
        "ollama_service": _ollama_service(ollama_url),
    }

