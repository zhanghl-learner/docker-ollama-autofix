from __future__ import annotations

from pathlib import Path

from autofix.agent.context import (
    collect_source_context,
    collect_source_context_with_metadata,
)
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


def test_python_imported_production_precedes_referenced_test_and_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "unrelated.py").write_text("unused = True\n", encoding="utf-8")
    (tmp_path / "pricing.py").write_text(
        "def total(value): return value\n", encoding="utf-8"
    )
    test = tmp_path / "test_pricing.py"
    test.write_text(
        "from pricing import total\nassert total(3) == 4\n", encoding="utf-8"
    )
    build = BuildResult(
        tmp_path,
        (
            StepResult(
                "test",
                ("pytest",),
                1,
                "test_pricing.py:2: AssertionError\n",
                "",
                0.1,
            ),
        ),
        "2026-10-29T00:00:00+00:00",
    )

    context = collect_source_context_with_metadata(tmp_path, build, max_files=2)

    assert list(context.files) == ["pricing.py", "test_pricing.py"]
    assert [item.reason for item in context.selections] == [
        "local-import:test_pricing.py",
        "build-output",
    ]
    assert context.selections[0].is_test is False
    assert context.selections[1].is_test is True


def test_javascript_relative_import_is_ranked_ahead_of_unrelated_source(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    tests = tmp_path / "tests"
    src.mkdir()
    tests.mkdir()
    (src / "unrelated.js").write_text("export const unused = 1;\n", encoding="utf-8")
    (src / "pricing.js").write_text("export const total = x => x;\n", encoding="utf-8")
    (tests / "pricing.test.js").write_text(
        "import { total } from '../src/pricing.js';\nexpect(total(3)).toBe(4);\n",
        encoding="utf-8",
    )
    build = BuildResult(
        tmp_path,
        (
            StepResult(
                "test",
                ("npm", "test"),
                1,
                "tests/pricing.test.js:2 expected 4\n",
                "",
                0.1,
            ),
        ),
        "2026-10-29T00:00:00+00:00",
    )

    context = collect_source_context_with_metadata(tmp_path, build, max_files=2)

    assert list(context.files) == ["src/pricing.js", "tests/pricing.test.js"]
    assert context.selections[0].reason == "local-import:tests/pricing.test.js"


def test_unique_python_module_suffix_resolves_src_layout(tmp_path: Path) -> None:
    package = tmp_path / "src" / "order_service"
    tests = tmp_path / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    (package / "pricing.py").write_text(
        "def line_total(value): return value\n", encoding="utf-8"
    )
    (tests / "test_pricing.py").write_text(
        "from order_service.pricing import line_total\nassert line_total(3) == 4\n",
        encoding="utf-8",
    )
    build = BuildResult(
        tmp_path,
        (
            StepResult(
                "test",
                ("pytest",),
                1,
                "tests/test_pricing.py:2: AssertionError\n",
                "",
                0.1,
            ),
        ),
        "2026-10-29T00:00:00+00:00",
    )

    context = collect_source_context_with_metadata(tmp_path, build, max_files=2)

    assert list(context.files) == [
        "src/order_service/pricing.py",
        "tests/test_pricing.py",
    ]
    assert context.selections[0].reason == "local-import:tests/test_pricing.py"


def test_python_package_reexport_traces_to_implementation(tmp_path: Path) -> None:
    package = tmp_path / "src" / "order_service"
    tests = tmp_path / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    (package / "__init__.py").write_text(
        "from .pricing import line_total\n", encoding="utf-8"
    )
    (package / "pricing.py").write_text(
        "def line_total(value): return value\n", encoding="utf-8"
    )
    (tests / "test_pricing.py").write_text(
        "from order_service import line_total\nassert line_total(3) == 4\n",
        encoding="utf-8",
    )
    build = BuildResult(
        tmp_path,
        (
            StepResult(
                "test",
                ("pytest",),
                1,
                "tests/test_pricing.py:2: AssertionError\n",
                "",
                0.1,
            ),
        ),
        "2026-10-29T00:00:00+00:00",
    )

    context = collect_source_context_with_metadata(tmp_path, build, max_files=3)

    assert list(context.files) == [
        "src/order_service/__init__.py",
        "src/order_service/pricing.py",
        "tests/test_pricing.py",
    ]
    assert context.selections[1].reason == (
        "local-import:src/order_service/__init__.py"
    )


def test_ambiguous_python_module_suffix_is_not_guessed(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    for prefix in ("src", "vendor"):
        package = tmp_path / prefix / "order_service"
        package.mkdir(parents=True)
        (package / "pricing.py").write_text("value = 1\n", encoding="utf-8")
    (tests / "test_pricing.py").write_text(
        "from order_service.pricing import value\nassert value == 2\n",
        encoding="utf-8",
    )
    build = BuildResult(
        tmp_path,
        (
            StepResult(
                "test",
                ("pytest",),
                1,
                "tests/test_pricing.py:2: AssertionError\n",
                "",
                0.1,
            ),
        ),
        "2026-10-29T00:00:00+00:00",
    )

    context = collect_source_context_with_metadata(tmp_path, build)

    reasons = {item.path: item.reason for item in context.selections}
    assert reasons["src/order_service/pricing.py"] == "project-source-fallback"
    assert reasons["vendor/order_service/pricing.py"] == "project-source-fallback"


def test_context_metadata_records_clipping_without_file_content(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("value = 1\n" * 20, encoding="utf-8")
    build = BuildResult(
        tmp_path,
        (StepResult("run", ("python",), 1, "large.py:1 failed\n", "", 0.1),),
        "2026-10-29T00:00:00+00:00",
    )

    context = collect_source_context_with_metadata(
        tmp_path, build, max_file_chars=12, max_total_chars=12
    )

    assert len(context.files["large.py"]) == 12
    assert context.selections[0].to_dict() == {
        "path": "large.py",
        "reason": "build-output",
        "is_test": False,
        "chars": 12,
        "truncated": True,
    }


def test_ignored_dependency_tree_is_not_selected(tmp_path: Path) -> None:
    dependencies = tmp_path / "node_modules" / "dependency"
    dependencies.mkdir(parents=True)
    (dependencies / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("throw new Error('broken');\n", encoding="utf-8")
    build = BuildResult(
        tmp_path,
        (StepResult("test", ("npm", "test"), 1, "app.js:1 broken\n", "", 0.1),),
        "2026-10-29T00:00:00+00:00",
    )

    context = collect_source_context_with_metadata(tmp_path, build)

    assert list(context.files) == ["app.js"]
