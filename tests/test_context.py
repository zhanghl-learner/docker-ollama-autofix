from __future__ import annotations

from pathlib import Path

from autofix.agent.context import collect_source_context
from autofix.builders import BuildResult, StepResult


def test_production_sources_are_presented_before_referenced_tests(tmp_path: Path) -> None:
    (tmp_path / "calculator.py").write_text("def add(a, b): return a - b\n", encoding="utf-8")
    (tmp_path / "test_calculator.py").write_text(
        "from calculator import add\nassert add(2, 3) == 5\n", encoding="utf-8"
    )
    build = BuildResult(
        tmp_path,
        (
            StepResult(
                "test",
                ("pytest",),
                1,
                "test_calculator.py:2: AssertionError\n",
                "",
                0.1,
            ),
        ),
        "2026-10-21T00:00:00+00:00",
    )

    files = collect_source_context(tmp_path, build)

    assert list(files) == ["calculator.py", "test_calculator.py"]
