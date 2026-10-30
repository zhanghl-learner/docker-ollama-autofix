"""Preview and differentially validate JavaScript/fast-check candidates."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autofix.agent.plans import load_repair_plan
from autofix.agent.textio import read_utf8_exact, write_utf8_exact
from autofix.builders import BuildResult, run_build
from autofix.models import BuildStep
from autofix.property_testing import (
    MAX_CANDIDATE_BYTES,
    PropertyCheckError,
    PropertyRunEvidence,
    _apply_fixed_changes,
    _copy_ignore,
    _run_evidence,
    _validate_plan_policy,
)
from autofix.runtime.tools import find_executable


BANNED_JS_MODULES = {
    "child_process",
    "cluster",
    "dgram",
    "fs",
    "http",
    "https",
    "net",
    "tls",
    "worker_threads",
}
MAX_DEPENDENCY_FILES = 10_000
MAX_DEPENDENCY_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class JavaScriptPropertyCandidate:
    """A bounded JavaScript/fast-check candidate awaiting isolated execution."""

    path: Path
    source: str
    sha256: str
    test_names: tuple[str, ...]
    target_path: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize candidate identity without duplicating executable source."""

        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": len(self.source.encode("utf-8")),
            "framework": "fast-check",
            "test_names": list(self.test_names),
            "target_path": self.target_path,
        }


@dataclass(frozen=True)
class JavaScriptPropertyCheckResult:
    """Preview or differential evidence for one fast-check candidate."""

    root: Path
    plan_path: Path
    plan_diff_sha256: str | None
    candidate: JavaScriptPropertyCandidate
    command: BuildStep
    run_requested: bool
    verdict: str
    baseline_runs: tuple[PropertyRunEvidence, ...] = ()
    fixed_runs: tuple[PropertyRunEvidence, ...] = ()
    fixed_verification: tuple[PropertyRunEvidence, ...] = ()
    report_path: Path | None = None

    @property
    def passed(self) -> bool:
        """Return whether differential and plan verification fully passed."""

        return self.verdict == "counterexample-confirmed"

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JavaScript property-check JSON envelope."""

        return {
            "schema": "autofix-javascript-property-check",
            "version": 1,
            "root": str(self.root),
            "plan_path": str(self.plan_path),
            "plan_diff_sha256": self.plan_diff_sha256,
            "candidate": self.candidate.to_dict(),
            "command": self.command.to_dict(),
            "run_requested": self.run_requested,
            "verdict": self.verdict,
            "passed": self.passed,
            "baseline_runs": [item.to_dict() for item in self.baseline_runs],
            "fixed_runs": [item.to_dict() for item in self.fixed_runs],
            "fixed_verification": [
                item.to_dict() for item in self.fixed_verification
            ],
            "report_path": str(self.report_path) if self.report_path else None,
        }


def inspect_javascript_property_candidate(
    path: Path | str,
) -> JavaScriptPropertyCandidate:
    """Apply conservative text-level checks without executing JavaScript."""

    candidate_path = Path(path).expanduser().resolve()
    if not candidate_path.is_file() or candidate_path.is_symlink():
        raise PropertyCheckError("JavaScript property candidate must be a regular file")
    if candidate_path.suffix.lower() not in {".js", ".mjs"}:
        raise PropertyCheckError("JavaScript property candidate must be .js or .mjs")
    try:
        source = read_utf8_exact(candidate_path)
    except UnicodeDecodeError as exc:
        raise PropertyCheckError("JavaScript property candidate must be UTF-8") from exc
    size = len(source.encode("utf-8"))
    if size == 0 or size > MAX_CANDIDATE_BYTES:
        raise PropertyCheckError(
            f"JavaScript property candidate must contain 1-{MAX_CANDIDATE_BYTES} bytes"
        )

    imported_specifiers = {
        match.group(1).removeprefix("node:")
        for match in re.finditer(
            r"(?:from\s+|require\(\s*)['\"]([^'\"]+)['\"]", source
        )
    }
    imported_roots = {specifier.split("/", 1)[0] for specifier in imported_specifiers}
    forbidden = sorted(imported_roots & BANNED_JS_MODULES)
    forbidden_patterns = {
        "dynamic import": r"\bimport\s*\(",
        "eval": r"\beval\s*\(",
        "Function constructor": r"\bnew\s+Function\b",
        "fetch": r"\bfetch\s*\(",
        "process capability": r"\bprocess\s*\.",
    }
    capabilities = [
        label for label, pattern in forbidden_patterns.items() if re.search(pattern, source)
    ]
    if forbidden or capabilities:
        detail = ", ".join((*forbidden, *capabilities))
        raise PropertyCheckError(
            f"JavaScript property candidate uses forbidden capability: {detail}"
        )
    fast_check_binding = re.search(
        r"(?:import\s+(?:\*\s+as\s+)?fc\s+from\s+['\"]fast-check['\"]"
        r"|(?:const|let)\s+fc\s*=\s*require\(\s*['\"]fast-check['\"]\s*\))",
        source,
    )
    node_test_binding = re.search(
        r"import\s+(?:\{\s*test\s*\}|test)\s+from\s+['\"]node:test['\"]",
        source,
    )
    if not fast_check_binding:
        raise PropertyCheckError("Candidate must bind fc from the fast-check package")
    if not node_test_binding:
        raise PropertyCheckError("Candidate must bind test from node:test")
    if not re.search(r"\bfc\s*\.\s*assert\s*\(", source) or not re.search(
        r"\bfc\s*\.\s*property\s*\(", source
    ):
        raise PropertyCheckError("Candidate must call fc.assert(fc.property(...))")
    test_names = tuple(
        match.group(1)
        for match in re.finditer(
            r"\btest\s*\(\s*['\"]([^'\"]+)['\"]", source
        )
    )
    if not test_names:
        raise PropertyCheckError("Candidate must define at least one node:test test(...)")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    target = f".autofix_property_candidates/test_{digest[:12]}.mjs"
    return JavaScriptPropertyCandidate(
        candidate_path, source, digest, test_names, target
    )


def _declared_fast_check_packages(root: Path) -> tuple[tuple[str, Path], ...]:
    """Resolve a bounded, installed fast-check dependency closure without npm."""

    manifest = root / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PropertyCheckError("JavaScript property execution requires package.json") from exc
    declared: dict[str, Any] = {}
    for field in ("dependencies", "devDependencies"):
        value = data.get(field)
        if isinstance(value, dict):
            declared.update(value)
    if "fast-check" not in declared:
        raise PropertyCheckError(
            "JavaScript property execution requires fast-check declared in package.json"
        )

    node_modules = root / "node_modules"
    pending = ["fast-check"]
    resolved: dict[str, Path] = {}
    total_files = 0
    total_bytes = 0
    while pending:
        name = pending.pop()
        if name in resolved:
            continue
        parts = name.split("/")
        if (
            not name
            or ".." in parts
            or any(not part for part in parts)
            or (name.startswith("@") and len(parts) != 2)
            or (not name.startswith("@") and len(parts) != 1)
        ):
            raise PropertyCheckError(f"fast-check has an unsafe dependency name: {name}")
        package_root = node_modules.joinpath(*parts)
        if not package_root.is_dir() or package_root.is_symlink():
            raise PropertyCheckError(
                f"JavaScript property execution requires installed package: {name}"
            )
        package_files = list(package_root.rglob("*"))
        if any(item.is_symlink() for item in package_files):
            raise PropertyCheckError(
                f"Installed JavaScript property dependency contains a symlink: {name}"
            )
        files = [item for item in package_files if item.is_file()]
        total_files += len(files)
        total_bytes += sum(item.stat().st_size for item in files)
        if total_files > MAX_DEPENDENCY_FILES or total_bytes > MAX_DEPENDENCY_BYTES:
            raise PropertyCheckError("fast-check dependency closure exceeds safety limits")
        package_manifest = package_root / "package.json"
        try:
            package_data = json.loads(package_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PropertyCheckError(
                f"Installed JavaScript package has invalid metadata: {name}"
            ) from exc
        dependencies = package_data.get("dependencies", {})
        if not isinstance(dependencies, dict):
            raise PropertyCheckError(
                f"Installed JavaScript package has invalid dependencies: {name}"
            )
        pending.extend(dependency for dependency in dependencies if dependency not in resolved)
        resolved[name] = package_root
    return tuple(sorted(resolved.items()))


def _install_dependencies(
    root: Path, packages: tuple[tuple[str, Path], ...]
) -> None:
    """Copy only the prevalidated fast-check closure into an isolated project."""

    for name, source in packages:
        destination = root / "node_modules" / Path(*name.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)


def _save_result(
    result: JavaScriptPropertyCheckResult,
) -> JavaScriptPropertyCheckResult:
    """Persist one JavaScript differential report under the audit directory."""

    report_dir = result.root / ".autofix" / "javascript-property-checks"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    report_path = report_dir / f"{stamp}.json"
    saved = replace(result, report_path=report_path)
    report_path.write_text(
        json.dumps(saved.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return saved


def check_javascript_property_candidate(
    root: Path | str,
    candidate_path: Path | str,
    plan_path: Path | str,
    *,
    run: bool = False,
    timeout_seconds: float = 300,
    save_report: bool = True,
    build_runner: Callable[..., BuildResult] = run_build,
) -> JavaScriptPropertyCheckResult:
    """Preview or run fast-check evidence in baseline and fixed copies."""

    project_root = Path(root).expanduser().resolve()
    candidate = inspect_javascript_property_candidate(candidate_path)
    plan, changes = load_repair_plan(plan_path, project_root)
    verification_steps = _validate_plan_policy(plan)
    node = find_executable("node") if run else "node"
    if run and node is None:
        raise PropertyCheckError("JavaScript property execution requires node in PATH")
    command = BuildStep(
        "Run isolated fast-check candidate",
        (str(node), "--test", "--test-isolation=none", candidate.target_path),
    )
    result = JavaScriptPropertyCheckResult(
        project_root,
        plan.path,
        plan.diff_sha256,
        candidate,
        command,
        run,
        "preview" if not run else "pending",
    )
    if not run:
        return result
    packages = _declared_fast_check_packages(project_root)

    with tempfile.TemporaryDirectory(prefix="autofix-js-property-") as temporary:
        temporary_root = Path(temporary)
        baseline_root = temporary_root / "baseline"
        fixed_root = temporary_root / "fixed"
        shutil.copytree(project_root, baseline_root, ignore=_copy_ignore)
        shutil.copytree(project_root, fixed_root, ignore=_copy_ignore)
        for isolated_root in (baseline_root, fixed_root):
            target = isolated_root / candidate.target_path
            target.parent.mkdir(parents=True, exist_ok=True)
            write_utf8_exact(target, candidate.source)
            _install_dependencies(isolated_root, packages)
        _apply_fixed_changes(fixed_root, changes)

        baseline_runs = tuple(
            _run_evidence(
                baseline_root,
                command,
                phase="baseline-candidate",
                attempt=attempt,
                timeout_seconds=timeout_seconds,
                build_runner=build_runner,
            )
            for attempt in (1, 2)
        )
        if any(item.succeeded for item in baseline_runs):
            result = replace(
                result,
                verdict="baseline-not-failing",
                baseline_runs=baseline_runs,
            )
        elif len({item.failure_signature for item in baseline_runs}) != 1:
            result = replace(
                result,
                verdict="baseline-unstable",
                baseline_runs=baseline_runs,
            )
        else:
            fixed_runs = tuple(
                _run_evidence(
                    fixed_root,
                    command,
                    phase="fixed-candidate",
                    attempt=attempt,
                    timeout_seconds=timeout_seconds,
                    build_runner=build_runner,
                )
                for attempt in (1, 2)
            )
            if not all(item.succeeded for item in fixed_runs):
                result = replace(
                    result,
                    verdict="fixed-candidate-failed",
                    baseline_runs=baseline_runs,
                    fixed_runs=fixed_runs,
                )
            else:
                verification: list[PropertyRunEvidence] = []
                for index, step in enumerate(verification_steps, 1):
                    evidence = _run_evidence(
                        fixed_root,
                        step,
                        phase="fixed-plan-verification",
                        attempt=index,
                        timeout_seconds=timeout_seconds,
                        build_runner=build_runner,
                    )
                    verification.append(evidence)
                    if not evidence.succeeded:
                        break
                verdict = (
                    "counterexample-confirmed"
                    if verification and all(item.succeeded for item in verification)
                    else "fixed-verification-failed"
                )
                result = replace(
                    result,
                    verdict=verdict,
                    baseline_runs=baseline_runs,
                    fixed_runs=fixed_runs,
                    fixed_verification=tuple(verification),
                )
    return _save_result(result) if save_report else result
