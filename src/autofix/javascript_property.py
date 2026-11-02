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
from autofix.property_apply import (
    ExclusiveProjectFile,
    PropertyApplyPolicyError,
    has_exact_untracked_target,
    load_applied_reviewed_plan,
    publish_json_exclusive,
)
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
from autofix.repository import (
    GitInspectionError,
    GitWorktreeError,
    create_detached_worktree,
    inspect_git_workspace,
)


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


@dataclass(frozen=True)
class JavaScriptPropertyApplyResult:
    """Preview or verified application of one promoted JavaScript test."""

    root: Path
    promotion_path: Path
    target_path: str
    candidate_sha256: str
    baseline_commit: str
    dependency_packages: tuple[JavaScriptDependencyPackage, ...]
    apply_requested: bool
    applied: bool
    rolled_back: bool
    verification_steps: tuple[BuildStep, ...]
    verification: BuildResult | None = None
    receipt_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JavaScript property-apply JSON envelope."""

        return {
            "schema": "autofix-javascript-property-apply",
            "version": 1,
            "root": str(self.root),
            "promotion_path": str(self.promotion_path),
            "target_path": self.target_path,
            "candidate_sha256": self.candidate_sha256,
            "baseline_commit": self.baseline_commit,
            "dependency_packages": [
                package.to_dict() for package in self.dependency_packages
            ],
            "apply_requested": self.apply_requested,
            "applied": self.applied,
            "rolled_back": self.rolled_back,
            "verification_steps": [
                step.to_dict() for step in self.verification_steps
            ],
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "receipt_path": str(self.receipt_path) if self.receipt_path else None,
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


def _load_promotion(
    path: Path, project_root: Path
) -> tuple[
    dict[str, Any],
    JavaScriptPropertyCandidate,
    tuple[JavaScriptDependencyPackage, ...],
    tuple[BuildStep, ...],
]:
    """Authenticate a JavaScript promotion and current repaired project state."""

    if not path.is_file() or path.is_symlink():
        raise PropertyCheckError(
            "JavaScript property promotion metadata must be a regular file"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PropertyCheckError(
            "JavaScript property promotion metadata must be valid UTF-8 JSON"
        ) from exc
    required = {
        "schema",
        "version",
        "root",
        "report_path",
        "report_sha256",
        "candidate_sha256",
        "plan_path",
        "target_path",
        "dependency_packages",
        "patch_path",
        "patch_sha256",
        "metadata_path",
        "applied",
    }
    if (
        not isinstance(data, dict)
        or set(data) != required
        or data.get("schema") != "autofix-javascript-property-promotion"
        or data.get("version") != 1
        or data.get("applied") is not False
    ):
        raise PropertyCheckError(
            "JavaScript property promotion metadata has an unsupported structure"
        )
    try:
        recorded_root = Path(data["root"]).expanduser().resolve()
        metadata_path = Path(data["metadata_path"]).expanduser().resolve()
        report_path = Path(data["report_path"]).expanduser().resolve()
        plan_path = Path(data["plan_path"]).expanduser().resolve()
        patch_path = Path(data["patch_path"]).expanduser().resolve()
    except TypeError as exc:
        raise PropertyCheckError(
            "JavaScript property promotion contains invalid paths"
        ) from exc
    if recorded_root != project_root or metadata_path != path:
        raise PropertyCheckError(
            "JavaScript property promotion belongs to another project or path"
        )
    report_data, candidate, report_plan, packages, report_sha256 = (
        _load_confirmed_report(report_path, project_root)
    )
    if report_sha256 != data["report_sha256"]:
        raise PropertyCheckError("Confirmed JavaScript report changed after promotion")
    if candidate.sha256 != data["candidate_sha256"]:
        raise PropertyCheckError("Promoted JavaScript candidate identity changed")
    if report_plan != plan_path:
        raise PropertyCheckError(
            "JavaScript promotion and report reference different plans"
        )
    if [package.to_dict() for package in packages] != data["dependency_packages"]:
        raise PropertyCheckError(
            "JavaScript promotion dependency evidence no longer matches"
        )
    if not patch_path.is_file() or patch_path.is_symlink():
        raise PropertyCheckError("Promoted JavaScript patch is missing or unsafe")
    patch_bytes = patch_path.read_bytes()
    if hashlib.sha256(patch_bytes).hexdigest() != data["patch_sha256"]:
        raise PropertyCheckError("Promoted JavaScript patch changed after review")
    target_path, _ = _promotion_target(
        project_root, data["target_path"], candidate.sha256
    )
    expected_patch = _render_new_test_patch(target_path, candidate.source).encode("utf-8")
    if patch_bytes != expected_patch:
        raise PropertyCheckError(
            "Promoted JavaScript patch does not match the confirmed candidate"
        )
    try:
        plan = load_applied_reviewed_plan(
            plan_path, project_root, label="JavaScript repair plan"
        )
    except PropertyApplyPolicyError as exc:
        message = str(exc).replace(
            "Reviewed repair is not applied exactly:",
            "Reviewed JavaScript repair is not applied exactly:",
        )
        raise PropertyCheckError(message) from exc
    if report_data["plan_diff_sha256"] != plan.data.get("diff_sha256"):
        raise PropertyCheckError(
            "JavaScript property report no longer matches the repair plan diff"
        )
    return data, candidate, packages, plan.steps


def apply_javascript_property_promotion(
    root: Path | str,
    promotion: Path | str,
    *,
    apply: bool = False,
    timeout_seconds: float = 300,
    save_receipt: bool = True,
    receipt_output: Path | str | None = None,
    build_runner: Callable[..., BuildResult] = run_build,
) -> JavaScriptPropertyApplyResult:
    """Preview or apply a JavaScript promotion after isolated verification."""

    project_root = Path(root).expanduser().resolve()
    promotion_path = Path(promotion).expanduser().resolve()
    try:
        git_status = inspect_git_workspace(project_root)
    except GitInspectionError as exc:
        raise PropertyCheckError(f"Unable to inspect Git workspace: {exc}") from exc
    if (
        not git_status.is_repository
        or git_status.head_commit is None
        or not git_status.clean
    ):
        raise PropertyCheckError(
            "JavaScript property apply requires a clean Git repository with a committed repair"
        )
    data, candidate, packages, plan_steps = _load_promotion(
        promotion_path, project_root
    )
    node = find_executable("node") if apply else "node"
    if apply and node is None:
        raise PropertyCheckError("JavaScript property apply requires node in PATH")
    property_step = BuildStep(
        "Run promoted JavaScript property regression",
        (str(node), "--test", "--test-isolation=none", data["target_path"]),
    )
    steps = (property_step, *plan_steps)
    result = JavaScriptPropertyApplyResult(
        project_root,
        promotion_path,
        data["target_path"],
        candidate.sha256,
        git_status.head_commit,
        packages,
        apply,
        False,
        False,
        steps,
    )
    if not apply:
        return result

    try:
        worktree = create_detached_worktree(git_status)
    except GitWorktreeError as exc:
        raise PropertyCheckError(
            f"Unable to create JavaScript property verification worktree: {exc}"
        ) from exc
    try:
        _install_dependencies(worktree.project_root, packages)
        isolated_target = worktree.project_root / data["target_path"]
        isolated_target.parent.mkdir(parents=True, exist_ok=True)
        write_utf8_exact(isolated_target, candidate.source)
        verification_result = build_runner(
            worktree.project_root,
            steps,
            timeout_seconds=timeout_seconds,
            save_report=False,
        )
        verification_result = replace(
            verification_result, root=project_root, report_path=None
        )
    except BaseException as exc:
        try:
            worktree.close()
        except GitWorktreeError as cleanup_error:
            exc.add_note(
                "JavaScript property worktree cleanup also failed: "
                f"{cleanup_error}"
            )
        raise
    try:
        worktree.close()
    except GitWorktreeError as exc:
        raise PropertyCheckError(
            f"Unable to clean JavaScript property worktree: {exc}"
        ) from exc

    result = replace(
        result,
        applied=verification_result.succeeded,
        rolled_back=not verification_result.succeeded,
        verification=verification_result,
    )
    if not verification_result.succeeded:
        return _save_apply_result(
            project_root,
            result,
            save_receipt=save_receipt,
            receipt_output=receipt_output,
        )

    try:
        current_status = inspect_git_workspace(project_root)
    except GitInspectionError as exc:
        raise PropertyCheckError(
            f"Unable to recheck Git workspace after JavaScript verification: {exc}"
        ) from exc
    if (
        not current_status.clean
        or current_status.head_commit != git_status.head_commit
        or current_status.repository_fingerprint != git_status.repository_fingerprint
    ):
        raise PropertyCheckError(
            "Git workspace changed during isolated JavaScript verification; "
            "the test file was not written"
        )
    refreshed_data, refreshed_candidate, refreshed_packages, refreshed_plan_steps = (
        _load_promotion(promotion_path, project_root)
    )
    if (
        refreshed_data["target_path"] != data["target_path"]
        or refreshed_candidate != candidate
        or refreshed_packages != packages
        or refreshed_plan_steps != plan_steps
    ):
        raise PropertyCheckError(
            "JavaScript promotion changed during isolated verification; "
            "the test file was not written"
        )

    destination = project_root / data["target_path"]
    try:
        with ExclusiveProjectFile(
            project_root, destination, refreshed_candidate.source
        ) as created_file:
            try:
                written_status = inspect_git_workspace(project_root)
            except GitInspectionError as exc:
                raise PropertyCheckError(
                    f"Unable to verify written JavaScript test with Git: {exc}"
                ) from exc
            if not has_exact_untracked_target(
                git_status, written_status, data["target_path"]
            ):
                raise PropertyCheckError(
                    "Git changes after JavaScript property write are not exactly the new "
                    "test file; the test file was removed"
                )
            saved_result = _save_apply_result(
                project_root,
                result,
                save_receipt=save_receipt,
                receipt_output=receipt_output,
            )
            created_file.retain()
            return saved_result
    except PropertyApplyPolicyError as exc:
        raise PropertyCheckError(str(exc)) from exc


def _save_apply_result(
    project_root: Path,
    result: JavaScriptPropertyApplyResult,
    *,
    save_receipt: bool,
    receipt_output: Path | str | None,
) -> JavaScriptPropertyApplyResult:
    """Optionally publish a JavaScript apply result without overwriting evidence."""

    if not save_receipt and receipt_output is None:
        return result
    if receipt_output is None:
        receipt_dir = project_root / ".autofix" / "javascript-property-applies"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        receipt_path = receipt_dir / f"{stamp}.json"
    else:
        receipt_path = Path(receipt_output).expanduser().resolve()
    result = replace(result, receipt_path=receipt_path)
    _save_apply_receipt(receipt_path, result)
    return result


def _save_apply_receipt(
    destination: Path, result: JavaScriptPropertyApplyResult
) -> None:
    """Atomically publish a complete JavaScript apply receipt without overwrite."""

    try:
        publish_json_exclusive(
            destination,
            result.to_dict(),
            label="JavaScript property apply receipt",
        )
    except PropertyApplyPolicyError as exc:
        raise PropertyCheckError(str(exc)) from exc


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
