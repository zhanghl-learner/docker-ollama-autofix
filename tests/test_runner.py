from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from autofix.builders import runner
from autofix.builders import run_build
from autofix.models import BuildStep


def test_successful_build_and_report(tmp_path: Path) -> None:
    step = BuildStep("success", (sys.executable, "-c", "print('ok')"))

    result = run_build(tmp_path, (step,))

    assert result.succeeded
    assert result.steps[0].stdout.strip() == "ok"
    assert result.report_path is not None and result.report_path.is_file()
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["succeeded"] is True


def test_stops_after_failure(tmp_path: Path) -> None:
    fail = BuildStep("fail", (sys.executable, "-c", "import sys; sys.exit(7)"))
    skipped = BuildStep("skipped", (sys.executable, "-c", "print('should not run')"))

    result = run_build(tmp_path, (fail, skipped), save_report=False)

    assert not result.succeeded
    assert result.exit_code == 7
    assert len(result.steps) == 1


def test_missing_executable_returns_127(tmp_path: Path) -> None:
    step = BuildStep("missing", ("definitely-not-an-autofix-command",))

    result = run_build(tmp_path, (step,), save_report=False)

    assert result.exit_code == 127


def test_resolves_relative_project_executable(tmp_path: Path) -> None:
    script = tmp_path / "tools" / "check.py"
    script.parent.mkdir()
    script.write_text("placeholder", encoding="utf-8")
    relative = str(Path("tools") / "check.py")

    argv = runner._execution_argv(
        tmp_path, BuildStep("relative", (relative, "--check"))
    )

    assert argv == [str(script.resolve()), "--check"]


@pytest.mark.skipif(sys.platform != "win32", reason="exercises Windows CreateProcess")
def test_runs_relative_project_executable_on_windows(tmp_path: Path) -> None:
    executable = tmp_path / "tools" / "cmd.exe"
    executable.parent.mkdir()
    executable.write_bytes(Path(os.environ["COMSPEC"]).read_bytes())
    step = BuildStep("relative", (str(Path("tools") / "cmd.exe"), "/c", "exit", "0"))

    result = run_build(tmp_path, (step,), save_report=False)

    assert result.succeeded


def test_build_uses_temporary_go_cache(monkeypatch, tmp_path: Path) -> None:
    observed: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        observed["gocache"] = kwargs["env"]["GOCACHE"]
        assert Path(observed["gocache"]).is_dir()
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = run_build(
        tmp_path, (BuildStep("go", ("go", "test", "./...")),), save_report=False
    )

    assert result.succeeded
    assert not Path(observed["gocache"]).exists()

