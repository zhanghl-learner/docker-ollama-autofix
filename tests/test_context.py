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


def test_noncanonical_root_and_resolved_referenced_path_share_one_identity(
    tmp_path: Path,
) -> None:
    """Model a Windows 8.3/long-name mismatch without weakening containment."""

    project = tmp_path / "parent" / "project"
    project.mkdir(parents=True)
    source = project / "program.py"
    source.write_text("raise RuntimeError('broken')\n", encoding="utf-8")
    # The explicit ``..`` gives the root and referenced file different lexical
    # spellings, just as RUNNER~1 and runneradmin do on a Windows hosted runner.
    noncanonical_root = project.parent / "other" / ".." / project.name
    build = BuildResult(
        noncanonical_root,
        (
            StepResult(
                "run",
                ("python", "program.py"),
                1,
                "",
                f'{source.resolve()}:1: RuntimeError: broken\n',
                0.1,
            ),
        ),
        "2026-10-27T00:00:00+00:00",
    )

    files = collect_source_context(noncanonical_root, build)

    assert files == {"program.py": "raise RuntimeError('broken')\n"}


def test_referenced_source_outside_canonical_root_is_not_collected(tmp_path: Path) -> None:
    """Canonical path handling must not widen the project read boundary."""

    project = tmp_path / "project"
    project.mkdir()
    (project / "inside.py").write_text("value = 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    build = BuildResult(
        project,
        (
            StepResult(
                "run",
                ("python", "inside.py"),
                1,
                "",
                f'{outside.resolve()}:1: external diagnostic\n',
                0.1,
            ),
        ),
        "2026-10-27T00:00:00+00:00",
    )

    files = collect_source_context(project, build)

    assert files == {"inside.py": "value = 1\n"}
