from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autofix.agent.textio import read_utf8_exact, write_utf8_exact


class RollbackError(ValueError):
    pass


@dataclass(frozen=True)
class RollbackResult:
    report_path: Path
    restored_paths: tuple[Path, ...]
    backup_dir: Path
    receipt_path: Path


_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?(?:\r?\n)?$"
)


def _reverse_unified_diff(current: str, diff: str, *, label: str) -> str:
    """Reconstruct the pre-change text while verifying the current file exactly."""

    patch_lines = diff.splitlines(keepends=True)
    current_lines = current.splitlines(keepends=True)
    hunk_indexes = [index for index, line in enumerate(patch_lines) if line.startswith("@@ ")]
    if not hunk_indexes:
        raise RollbackError(f"Repair report has no usable diff for {label}")

    restored: list[str] = []
    cursor = 0
    for position, hunk_index in enumerate(hunk_indexes):
        match = _HUNK_HEADER.match(patch_lines[hunk_index])
        if match is None:
            raise RollbackError(f"Malformed diff hunk for {label}")
        old_count = int(match.group(2) or "1")
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        target = new_start - 1
        if target < cursor or target > len(current_lines):
            raise RollbackError(f"Diff position does not match the current {label}")
        restored.extend(current_lines[cursor:target])
        cursor = target

        end = hunk_indexes[position + 1] if position + 1 < len(hunk_indexes) else len(patch_lines)
        consumed_old = 0
        consumed_new = 0
        for patch_line in patch_lines[hunk_index + 1 : end]:
            if patch_line.startswith("\\ No newline at end of file"):
                continue
            if not patch_line or patch_line[0] not in " +-":
                raise RollbackError(f"Malformed diff content for {label}")
            marker, payload = patch_line[0], patch_line[1:]
            if marker in " +":
                if cursor >= len(current_lines) or current_lines[cursor] != payload:
                    raise RollbackError(
                        f"Current file no longer matches the repair report: {label}"
                    )
                cursor += 1
                consumed_new += 1
            if marker in " -":
                restored.append(payload)
                consumed_old += 1
        if consumed_old != old_count or consumed_new != new_count:
            raise RollbackError(f"Diff line counts are invalid for {label}")

    restored.extend(current_lines[cursor:])
    return "".join(restored)


def _safe_project_file(root: Path, raw_path: Any) -> tuple[Path, Path]:
    if not isinstance(raw_path, str) or not raw_path:
        raise RollbackError("Repair report contains an invalid file path")
    relative = Path(raw_path.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise RollbackError(f"Unsafe path in repair report: {raw_path}")
    candidate = root / relative
    if candidate.is_symlink():
        raise RollbackError(f"File to restore is missing or unsafe: {raw_path}")
    path = candidate.resolve()
    try:
        safe_relative = path.relative_to(root)
    except ValueError as exc:
        raise RollbackError(f"Path escapes the project: {raw_path}") from exc
    if not path.is_file():
        raise RollbackError(f"File to restore is missing or unsafe: {raw_path}")
    return path, safe_relative


def rolled_back_reports(root: Path | str) -> set[Path]:
    project_root = Path(root).expanduser().resolve()
    receipt_dir = project_root / ".autofix" / "rollbacks"
    reports: set[Path] = set()
    if not receipt_dir.is_dir():
        return reports
    for receipt in receipt_dir.glob("*.json"):
        try:
            data = json.loads(receipt.read_text(encoding="utf-8"))
            source = data.get("source_report")
            if data.get("type") == "rollback" and isinstance(source, str):
                reports.add(Path(source).expanduser().resolve())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return reports


def latest_rollback_candidate(root: Path | str) -> Path:
    project_root = Path(root).expanduser().resolve()
    report_dir = project_root / ".autofix" / "repairs"
    already_rolled_back = rolled_back_reports(project_root)
    if report_dir.is_dir():
        for report in sorted(report_dir.glob("*.json"), reverse=True):
            resolved = report.resolve()
            if resolved in already_rolled_back:
                continue
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (
                data.get("type") == "repair"
                and data.get("succeeded") is True
                and data.get("applied") is True
                and isinstance(data.get("changes"), list)
                and data["changes"]
            ):
                return resolved
    raise RollbackError("No successful, applied, and not-yet-undone repair was found")


def rollback_report(report_path: Path | str, root: Path | str) -> RollbackResult:
    """Reverse all changes recorded by a successful repair report."""

    report = Path(report_path).expanduser().resolve()
    project_root = Path(root).expanduser().resolve()
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RollbackError(f"Unable to read repair report: {exc}") from exc
    if not isinstance(data, dict) or data.get("type") != "repair":
        raise RollbackError("Unsupported repair report")
    raw_changes = data.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise RollbackError("Repair report contains no changes to restore")

    contents: dict[Path, str] = {}
    relative_paths: dict[Path, Path] = {}
    for raw_change in reversed(raw_changes):
        if not isinstance(raw_change, dict) or not isinstance(raw_change.get("diff"), str):
            raise RollbackError("Repair report contains an invalid change")
        path, relative = _safe_project_file(project_root, raw_change.get("path"))
        relative_paths[path] = relative
        if path in contents:
            current = contents[path]
        else:
            try:
                current = read_utf8_exact(path)
            except (OSError, UnicodeDecodeError) as exc:
                raise RollbackError(f"Unable to read {relative.as_posix()}: {exc}") from exc
        contents[path] = _reverse_unified_diff(
            current, raw_change["diff"], label=relative.as_posix()
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = project_root / ".autofix" / "rollback-backups" / stamp
    receipt_path = project_root / ".autofix" / "rollbacks" / f"{stamp}.json"
    backups: dict[Path, bytes] = {}
    try:
        for path, relative in relative_paths.items():
            current_bytes = path.read_bytes()
            backups[path] = current_bytes
            backup = backup_dir / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(current_bytes)
        for path, restored in contents.items():
            write_utf8_exact(path, restored)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "type": "rollback",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "root": str(project_root),
                    "source_report": str(report),
                    "backup_dir": str(backup_dir),
                    "restored_paths": [
                        relative_paths[path].as_posix() for path in contents
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        for path, current_bytes in backups.items():
            try:
                path.write_bytes(current_bytes)
            except OSError:
                pass
        raise RollbackError(f"Rollback could not be completed: {exc}") from exc

    ordered = tuple(project_root / relative_paths[path] for path in contents)
    return RollbackResult(report, ordered, backup_dir, receipt_path)
