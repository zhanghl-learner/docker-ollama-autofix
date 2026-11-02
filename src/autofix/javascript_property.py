"""Preview and differentially validate JavaScript/fast-check candidates."""

from __future__ import annotations

import hashlib
import difflib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autofix.agent.plans import load_repair_plan
from autofix.agent.patches import is_test_path
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
class JavaScriptDependencyPackage:
    """One installed package admitted into the isolated dependency closure."""

    name: str
    version: str
    relative_path: str
    source_path: Path
    file_count: int
    size_bytes: int
    package_json_sha256: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize reproducibility evidence without exposing absolute paths."""

        return {
            "name": self.name,
            "version": self.version,
            "relative_path": self.relative_path,
            "file_count": self.file_count,
            "size_bytes": self.size_bytes,
            "package_json_sha256": self.package_json_sha256,
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
    dependency_packages: tuple[JavaScriptDependencyPackage, ...] = ()
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
            "version": 2,
            "root": str(self.root),
            "plan_path": str(self.plan_path),
            "plan_diff_sha256": self.plan_diff_sha256,
            "candidate": self.candidate.to_dict(),
            "command": self.command.to_dict(),
            "run_requested": self.run_requested,
            "verdict": self.verdict,
            "passed": self.passed,
            "dependency_packages": [
                package.to_dict() for package in self.dependency_packages
            ],
            "baseline_runs": [item.to_dict() for item in self.baseline_runs],
            "fixed_runs": [item.to_dict() for item in self.fixed_runs],
            "fixed_verification": [
                item.to_dict() for item in self.fixed_verification
            ],
            "report_path": str(self.report_path) if self.report_path else None,
        }


@dataclass(frozen=True)
class JavaScriptPropertyPromotionResult:
    """An unapplied JavaScript regression-test patch with bound evidence."""

    root: Path
    report_path: Path
    report_sha256: str
    candidate_sha256: str
    plan_path: Path
    target_path: str
    dependency_packages: tuple[JavaScriptDependencyPackage, ...]
    patch_path: Path
    patch_sha256: str
    metadata_path: Path

    def to_dict(self) -> dict[str, Any]:
        """Serialize the immutable identities required before future apply."""

        return {
            "schema": "autofix-javascript-property-promotion",
            "version": 1,
            "root": str(self.root),
            "report_path": str(self.report_path),
            "report_sha256": self.report_sha256,
            "candidate_sha256": self.candidate_sha256,
            "plan_path": str(self.plan_path),
            "target_path": self.target_path,
            "dependency_packages": [
                package.to_dict() for package in self.dependency_packages
            ],
            "patch_path": str(self.patch_path),
            "patch_sha256": self.patch_sha256,
            "metadata_path": str(self.metadata_path),
            "applied": False,
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


def _safe_package_parts(name: str) -> tuple[str, ...]:
    """Return validated npm package path parts, including scoped names."""

    parts = tuple(name.split("/"))
    if (
        not name
        or ".." in parts
        or any(not part or part in {".", "node_modules"} for part in parts)
        or (name.startswith("@") and len(parts) != 2)
        or (not name.startswith("@") and len(parts) != 1)
    ):
        raise PropertyCheckError(f"fast-check has an unsafe dependency name: {name}")
    return parts


def _resolve_installed_package(
    root: Path, requester: Path | None, name: str
) -> Path | None:
    """Resolve a package from a requester's nested or project-hoisted modules."""

    parts = _safe_package_parts(name)
    candidates = []
    if requester is not None:
        candidates.append(requester / "node_modules" / Path(*parts))
    candidates.append(root / "node_modules" / Path(*parts))
    for candidate in candidates:
        if candidate.is_dir():
            resolved = candidate.resolve()
            try:
                resolved.relative_to((root / "node_modules").resolve())
            except ValueError as exc:
                raise PropertyCheckError(
                    f"Installed JavaScript package escapes node_modules: {name}"
                ) from exc
            return candidate
    return None


def _declared_fast_check_packages(
    root: Path,
) -> tuple[JavaScriptDependencyPackage, ...]:
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

    pending: list[tuple[str, Path | None, bool]] = [("fast-check", None, False)]
    resolved: dict[str, JavaScriptDependencyPackage] = {}
    total_files = 0
    total_bytes = 0
    while pending:
        name, requester, optional = pending.pop()
        package_root = _resolve_installed_package(root, requester, name)
        if package_root is None:
            if optional:
                continue
            raise PropertyCheckError(
                f"JavaScript property execution requires installed package: {name}"
            )
        relative_path = package_root.relative_to(root).as_posix()
        if relative_path in resolved:
            continue
        if package_root.is_symlink():
            raise PropertyCheckError(
                f"Installed JavaScript property dependency contains a symlink: {name}"
            )
        package_files = [
            item
            for item in package_root.rglob("*")
            if "node_modules" not in item.relative_to(package_root).parts
        ]
        if any(item.is_symlink() for item in package_files):
            raise PropertyCheckError(
                f"Installed JavaScript property dependency contains a symlink: {name}"
            )
        files = [item for item in package_files if item.is_file()]
        package_bytes = sum(item.stat().st_size for item in files)
        total_files += len(files)
        total_bytes += package_bytes
        if total_files > MAX_DEPENDENCY_FILES or total_bytes > MAX_DEPENDENCY_BYTES:
            raise PropertyCheckError("fast-check dependency closure exceeds safety limits")
        package_manifest = package_root / "package.json"
        try:
            package_manifest_bytes = package_manifest.read_bytes()
            package_data = json.loads(package_manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PropertyCheckError(
                f"Installed JavaScript package has invalid metadata: {name}"
            ) from exc
        dependencies = package_data.get("dependencies", {})
        optional_dependencies = package_data.get("optionalDependencies", {})
        if not isinstance(dependencies, dict) or not isinstance(
            optional_dependencies, dict
        ):
            raise PropertyCheckError(
                f"Installed JavaScript package has invalid dependencies: {name}"
            )
        package_name = package_data.get("name")
        version = package_data.get("version")
        if package_name != name or not isinstance(version, str) or not version:
            raise PropertyCheckError(
                f"Installed JavaScript package identity does not match: {name}"
            )
        resolved[relative_path] = JavaScriptDependencyPackage(
            name,
            version,
            relative_path,
            package_root,
            len(files),
            package_bytes,
            hashlib.sha256(package_manifest_bytes).hexdigest(),
        )
        pending.extend((dependency, package_root, False) for dependency in dependencies)
        pending.extend(
            (dependency, package_root, True)
            for dependency in optional_dependencies
            if dependency not in dependencies
        )
    return tuple(resolved[path] for path in sorted(resolved))


def _install_dependencies(
    root: Path, packages: tuple[JavaScriptDependencyPackage, ...]
) -> None:
    """Copy only the prevalidated fast-check closure into an isolated project."""

    for package in packages:
        destination = root / package.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            package.source_path,
            destination,
            ignore=shutil.ignore_patterns("node_modules"),
        )


def _load_confirmed_report(
    report_path: Path, project_root: Path
) -> tuple[
    dict[str, Any],
    JavaScriptPropertyCandidate,
    Path,
    tuple[JavaScriptDependencyPackage, ...],
    str,
]:
    """Reload and authenticate a complete JavaScript differential report."""

    if not report_path.is_file() or report_path.is_symlink():
        raise PropertyCheckError("JavaScript property report must be a regular JSON file")
    report_bytes = report_path.read_bytes()
    try:
        data = json.loads(report_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PropertyCheckError(
            "JavaScript property report must contain valid UTF-8 JSON"
        ) from exc
    required = {
        "schema",
        "version",
        "root",
        "plan_path",
        "plan_diff_sha256",
        "candidate",
        "command",
        "run_requested",
        "verdict",
        "passed",
        "dependency_packages",
        "baseline_runs",
        "fixed_runs",
        "fixed_verification",
        "report_path",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise PropertyCheckError("JavaScript property report has an unsupported structure")
    if (
        data["schema"] != "autofix-javascript-property-check"
        or data["version"] != 2
        or data["verdict"] != "counterexample-confirmed"
        or data["passed"] is not True
        or data["run_requested"] is not True
    ):
        raise PropertyCheckError(
            "Only a v2 counterexample-confirmed JavaScript report can be promoted"
        )
    baseline_runs = data.get("baseline_runs")
    fixed_runs = data.get("fixed_runs")
    fixed_verification = data.get("fixed_verification")
    evidence_valid = (
        isinstance(baseline_runs, list)
        and len(baseline_runs) == 2
        and all(
            isinstance(item, dict)
            and item.get("phase") == "baseline-candidate"
            and item.get("succeeded") is False
            and isinstance(item.get("failure_signature"), str)
            for item in baseline_runs
        )
        and len({item["failure_signature"] for item in baseline_runs}) == 1
        and isinstance(fixed_runs, list)
        and len(fixed_runs) == 2
        and all(
            isinstance(item, dict)
            and item.get("phase") == "fixed-candidate"
            and item.get("succeeded") is True
            for item in fixed_runs
        )
        and isinstance(fixed_verification, list)
        and bool(fixed_verification)
        and all(
            isinstance(item, dict)
            and item.get("phase") == "fixed-plan-verification"
            and item.get("succeeded") is True
            for item in fixed_verification
        )
    )
    if not evidence_valid:
        raise PropertyCheckError(
            "JavaScript report lacks complete confirmed differential evidence"
        )
    try:
        reported_root = Path(data["root"]).expanduser().resolve()
        reported_path = Path(data["report_path"]).expanduser().resolve()
        plan_path = Path(data["plan_path"]).expanduser().resolve()
    except TypeError as exc:
        raise PropertyCheckError("JavaScript property report contains invalid paths") from exc
    if reported_root != project_root:
        raise PropertyCheckError("JavaScript property report belongs to another project")
    if reported_path != report_path:
        raise PropertyCheckError(
            "JavaScript property report path does not match its saved identity"
        )
    candidate_data = data.get("candidate")
    if not isinstance(candidate_data, dict) or not isinstance(
        candidate_data.get("path"), str
    ):
        raise PropertyCheckError("JavaScript property report has invalid candidate metadata")
    candidate = inspect_javascript_property_candidate(candidate_data["path"])
    if candidate.to_dict() != candidate_data:
        raise PropertyCheckError(
            "JavaScript property candidate changed after differential validation"
        )
    packages = _declared_fast_check_packages(project_root)
    if [package.to_dict() for package in packages] != data["dependency_packages"]:
        raise PropertyCheckError(
            "JavaScript property dependency closure changed after validation"
        )
    return (
        data,
        candidate,
        plan_path,
        packages,
        hashlib.sha256(report_bytes).hexdigest(),
    )


def _promotion_target(
    root: Path, target: str, candidate_sha256: str
) -> tuple[str, Path]:
    """Validate a new JavaScript test target without allowing replacement."""

    requested = (
        target.strip()
        or f"tests/autofix_property_{candidate_sha256[:12]}.test.mjs"
    )
    relative = Path(requested.replace("\\", "/"))
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.suffix.lower() not in {".js", ".mjs"}
    ):
        raise PropertyCheckError(
            "JavaScript promotion target must be a safe relative .js or .mjs path"
        )
    if not is_test_path(relative):
        raise PropertyCheckError(
            "JavaScript promotion target must use a recognized test path"
        )
    destination = (root / relative).resolve()
    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise PropertyCheckError(
            "JavaScript promotion target escapes the project root"
        ) from exc
    if destination.exists():
        raise PropertyCheckError(
            f"JavaScript promotion target already exists: {relative.as_posix()}"
        )
    return relative.as_posix(), destination


def _render_new_test_patch(target_path: str, source: str) -> str:
    """Render one deterministic Git new-file patch for reviewed JavaScript."""

    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    diff_lines = list(
        difflib.unified_diff(
            [],
            normalized.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=f"b/{target_path}",
        )
    )
    if diff_lines and not diff_lines[-1].endswith("\n"):
        diff_lines[-1] += "\n\\ No newline at end of file\n"
    return (
        f"diff --git a/{target_path} b/{target_path}\n"
        "new file mode 100644\n"
        + "".join(diff_lines)
    )


def promote_javascript_property_candidate(
    root: Path | str,
    report: Path | str,
    *,
    target: str = "",
) -> JavaScriptPropertyPromotionResult:
    """Create an unapplied JavaScript test patch from confirmed v2 evidence."""

    project_root = Path(root).expanduser().resolve()
    report_path = Path(report).expanduser().resolve()
    report_data, candidate, plan_path, packages, report_sha256 = (
        _load_confirmed_report(report_path, project_root)
    )
    plan, _ = load_repair_plan(plan_path, project_root)
    _validate_plan_policy(plan)
    if plan.diff_sha256 != report_data["plan_diff_sha256"]:
        raise PropertyCheckError(
            "JavaScript property report is bound to another plan diff"
        )
    target_path, _ = _promotion_target(project_root, target, candidate.sha256)
    patch = _render_new_test_patch(target_path, candidate.source)
    patch_bytes = patch.encode("utf-8")
    patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output_dir = project_root / ".autofix" / "javascript-property-patches"
    patch_path = output_dir / f"{stamp}.patch"
    metadata_path = output_dir / f"{stamp}.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    patch_created = False
    metadata_created = False
    try:
        with patch_path.open("xb") as stream:
            stream.write(patch_bytes)
        patch_created = True
        result = JavaScriptPropertyPromotionResult(
            project_root,
            report_path,
            report_sha256,
            candidate.sha256,
            plan_path,
            target_path,
            packages,
            patch_path,
            patch_sha256,
            metadata_path,
        )
        with metadata_path.open("x", encoding="utf-8", newline="") as stream:
            metadata_created = True
            stream.write(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        for created, artifact in (
            (metadata_created, metadata_path),
            (patch_created, patch_path),
        ):
            if created:
                try:
                    artifact.unlink(missing_ok=True)
                except OSError:
                    pass
        raise PropertyCheckError(
            f"Unable to write JavaScript promotion artifact: {exc}"
        ) from exc
    return result


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
    result = replace(result, dependency_packages=packages)

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
