"""Validate, normalize, apply, and restore model-proposed source edits."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from autofix.agent.context import CONFIG_NAMES, SOURCE_SUFFIXES
from autofix.agent.ollama import FileEdit, RepairProposal
from autofix.agent.textio import read_utf8_exact, write_utf8_exact


class UnsafePatchError(ValueError):
    """Raised when a proposal crosses a source-edit safety boundary."""

    pass


TEST_DIRECTORIES = {"__tests__", "spec", "specs", "test", "tests"}


def is_test_path(relative: Path) -> bool:
    """Recognize common test-file conventions across supported languages."""

    if any(part.lower() in TEST_DIRECTORIES for part in relative.parts[:-1]):
        return True
    name = relative.name.lower()
    stem = relative.stem
    lower_stem = stem.lower()
    return (
        name.startswith(("test_", "test-"))
        or lower_stem.endswith(("_test", "-test", ".test", "_spec", "-spec", ".spec"))
        or stem.endswith(("Test", "Tests", "Spec", "Specs"))
    )


def _merge_import_only_python(original: str, replacement: str) -> str:
    """Turn a destructive import-only replacement into a minimal import addition."""

    try:
        before = ast.parse(original)
        after = ast.parse(replacement)
    except SyntaxError:
        return replacement
    if not after.body or not all(isinstance(node, (ast.Import, ast.ImportFrom)) for node in after.body):
        return replacement
    if not any(not isinstance(node, (ast.Import, ast.ImportFrom)) for node in before.body):
        return replacement

    existing = {
        ast.dump(node, include_attributes=False)
        for node in before.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    additions = [
        ast.unparse(node)
        for node in after.body
        if ast.dump(node, include_attributes=False) not in existing
    ]
    if not additions:
        return original

    lines = original.splitlines(keepends=True)
    insertion_line = 0
    while insertion_line < min(2, len(lines)):
        stripped = lines[insertion_line].lstrip()
        if stripped.startswith("#!") or "coding" in stripped:
            insertion_line += 1
        else:
            break
    body_index = 0
    if (
        before.body
        and isinstance(before.body[0], ast.Expr)
        and isinstance(before.body[0].value, ast.Constant)
        and isinstance(before.body[0].value.value, str)
    ):
        insertion_line = max(insertion_line, before.body[0].end_lineno or before.body[0].lineno)
        body_index = 1
    while body_index < len(before.body):
        node = before.body[body_index]
        if not isinstance(node, ast.ImportFrom) or node.module != "__future__":
            break
        insertion_line = max(insertion_line, node.end_lineno or node.lineno)
        body_index += 1

    block = "".join(f"{statement}\n" for statement in additions)
    if insertion_line and insertion_line <= len(lines) and lines[insertion_line - 1].strip():
        block = "\n" + block
    if insertion_line < len(lines) and lines[insertion_line].strip():
        block += "\n"
    lines.insert(insertion_line, block)
    return "".join(lines)


def _validate_preservation(
    path: Path, original: str, replacement: str, *, allow_destructive_changes: bool
) -> None:
    if allow_destructive_changes:
        return
    original_lines = [line for line in original.splitlines() if line.strip()]
    replacement_lines = [line for line in replacement.splitlines() if line.strip()]
    if len(original_lines) >= 10 and len(replacement_lines) * 2 < len(original_lines):
        raise UnsafePatchError(
            f"Destructive replacement rejected for {path.name}: removes more than half of the non-empty lines"
        )
    # A passing test is not enough to justify silently deleting public Python
    # entry points. Compare only top-level definitions so nested refactors do
    # not create false positives.
    if path.suffix.lower() != ".py":
        return
    try:
        before = ast.parse(original)
        after = ast.parse(replacement)
    except SyntaxError:
        return
    definition_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    before_names = {node.name for node in before.body if isinstance(node, definition_types)}
    after_names = {node.name for node in after.body if isinstance(node, definition_types)}
    removed = sorted(before_names - after_names)
    if removed:
        raise UnsafePatchError(
            f"Destructive replacement rejected for {path.name}: removes top-level definitions: {', '.join(removed)}"
        )


def normalize_newline_style(original: str, proposed: str) -> str:
    """Normalize replacement text to the source file's newline convention."""

    newline = "\r\n" if "\r\n" in original else "\n"
    normalized = proposed.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline)


@dataclass(frozen=True)
class ValidatedChange:
    """A safe, fully materialized file replacement derived from bounded edits."""

    path: Path
    relative_path: str
    original: str
    replacement: str
    reason: str

    @property
    def diff(self) -> str:
        """Render the reviewable unified diff for this file."""

        return "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                self.replacement.splitlines(keepends=True),
                fromfile=f"a/{self.relative_path}",
                tofile=f"b/{self.relative_path}",
            )
        )


def validated_diff_sha256(changes: tuple[ValidatedChange, ...]) -> str:
    """Hash a canonical ordered representation of the final reviewable diffs."""

    records = [
        {"path": change.relative_path, "diff": change.diff}
        for change in sorted(changes, key=lambda item: item.relative_path)
    ]
    payload = json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_line_edits(
    root: Path,
    edits: tuple[FileEdit, ...],
    *,
    max_files: int,
    max_total_chars: int,
    allow_test_changes: bool,
    allow_destructive_changes: bool,
) -> tuple[ValidatedChange, ...]:
    """Materialize exact, non-overlapping splices against original file text.

    Coordinates always refer to the original file. Edits are therefore checked
    for overlap first and applied from bottom to top so earlier replacements do
    not shift the line numbers of later ones.
    """

    grouped: dict[str, list[FileEdit]] = {}
    for edit in edits:
        grouped.setdefault(edit.path, []).append(edit)
    if len(grouped) > max_files:
        raise UnsafePatchError(f"The model proposed changes to more than {max_files} files")
    if sum(len(edit.new_text) + len(edit.old_text) for edit in edits) > max_total_chars:
        raise UnsafePatchError("The proposed edit content is too large")

    validated: list[ValidatedChange] = []
    seen_paths: set[Path] = set()
    for raw_path, file_edits in grouped.items():
        raw = Path(raw_path.replace("\\", "/"))
        if raw.is_absolute() or ".." in raw.parts:
            raise UnsafePatchError(f"Unsafe path: {raw_path}")
        path = (root / raw).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise UnsafePatchError(f"Path escapes the project: {raw_path}") from exc
        if path in seen_paths:
            raise UnsafePatchError(f"Duplicate path: {raw_path}")
        seen_paths.add(path)
        if not allow_test_changes and is_test_path(relative):
            raise UnsafePatchError(f"Test files are protected by default: {relative.as_posix()}")
        if not path.is_file():
            raise UnsafePatchError(f"Only existing files may be changed: {raw_path}")
        if path.suffix.lower() not in SOURCE_SUFFIXES and path.name not in CONFIG_NAMES:
            raise UnsafePatchError(f"Unsupported file type: {raw_path}")
        try:
            original = read_utf8_exact(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise UnsafePatchError(f"File is not editable UTF-8 text: {raw_path}") from exc

        lines = original.splitlines(keepends=True)
        normalized_edits: list[FileEdit] = []
        for edit in file_edits:
            edit = FileEdit(
                edit.path,
                edit.start_line,
                edit.delete_lines,
                normalize_newline_style(original, edit.old_text),
                normalize_newline_style(original, edit.new_text),
                edit.reason,
            )
            if edit.delete_lines == 0 and edit.old_text:
                start = edit.start_line - 1
                old_count = len(edit.old_text.splitlines())
                anchored = "".join(lines[start : start + old_count])
                if not old_count or anchored.rstrip("\r\n") != edit.old_text.rstrip("\r\n"):
                    raise UnsafePatchError(
                        f"Invalid edit for {relative.as_posix()} at line {edit.start_line}: "
                        "delete_lines is 0 and old_text does not anchor the proposed block"
                    )
                candidate_lines = edit.new_text.splitlines(keepends=True)
                segment_count = max(old_count, len(candidate_lines))
                if start + segment_count > len(lines):
                    raise UnsafePatchError(
                        f"Proposed block is outside {relative.as_posix()} at line {edit.start_line}"
                    )
                original_segment = lines[start : start + segment_count]
                if candidate_lines and not candidate_lines[-1].endswith(("\n", "\r")):
                    corresponding = min(len(candidate_lines), len(original_segment)) - 1
                    if corresponding >= 0 and original_segment[corresponding].endswith("\r\n"):
                        candidate_lines[-1] += "\r\n"
                    elif corresponding >= 0 and original_segment[corresponding].endswith("\n"):
                        candidate_lines[-1] += "\n"
                common_prefix = 0
                while (
                    common_prefix < len(original_segment)
                    and common_prefix < len(candidate_lines)
                    and original_segment[common_prefix] == candidate_lines[common_prefix]
                ):
                    common_prefix += 1
                common_suffix = 0
                while (
                    common_suffix < len(original_segment) - common_prefix
                    and common_suffix < len(candidate_lines) - common_prefix
                    and original_segment[-1 - common_suffix] == candidate_lines[-1 - common_suffix]
                ):
                    common_suffix += 1
                old_end = len(original_segment) - common_suffix
                new_end = len(candidate_lines) - common_suffix
                edit = FileEdit(
                    edit.path,
                    edit.start_line + common_prefix,
                    old_end - common_prefix,
                    "".join(original_segment[common_prefix:old_end]),
                    "".join(candidate_lines[common_prefix:new_end]),
                    edit.reason,
                )
            new_text = edit.new_text
            if edit.delete_lines == 0:
                start = edit.start_line - 1
                for count in range(len(lines) - start, 0, -1):
                    duplicated = "".join(lines[start : start + count])
                    candidates = (duplicated, duplicated.rstrip("\r\n"))
                    matched = next(
                        (candidate for candidate in candidates if candidate and new_text.endswith(candidate)),
                        None,
                    )
                    if matched is not None:
                        new_text = new_text[: -len(matched)]
                        break
                if new_text and start < len(lines) and not new_text.endswith(("\n", "\r")):
                    new_text += "\n"
            else:
                start = edit.start_line - 1
                end = start + edit.delete_lines
                selected = "".join(lines[start:end]) if 0 <= start <= end <= len(lines) else ""
                if selected.rstrip("\r\n") != edit.old_text.rstrip("\r\n"):
                    matches: list[tuple[int, str]] = []
                    for candidate_start in range(0, len(lines) - edit.delete_lines + 1):
                        candidate = "".join(
                            lines[candidate_start : candidate_start + edit.delete_lines]
                        )
                        if candidate.rstrip("\r\n") == edit.old_text.rstrip("\r\n"):
                            matches.append((candidate_start, candidate))
                    if len(matches) == 1:
                        start, selected = matches[0]
                        edit = FileEdit(
                            edit.path,
                            start + 1,
                            edit.delete_lines,
                            selected,
                            new_text,
                            edit.reason,
                        )
                    elif not matches and edit.delete_lines == 1:
                        indentation_matches: list[tuple[int, str]] = []
                        for candidate_start, candidate in enumerate(lines):
                            if candidate.strip() == edit.old_text.strip():
                                indentation_matches.append((candidate_start, candidate))
                        if len(indentation_matches) == 1:
                            start, selected = indentation_matches[0]
                            indentation = selected[: len(selected) - len(selected.lstrip(" \t"))]
                            replacement_body = new_text.rstrip("\r\n").lstrip(" \t")
                            newline = (
                                "\r\n"
                                if selected.endswith("\r\n")
                                else "\n"
                                if selected.endswith("\n")
                                else ""
                            )
                            new_text = indentation + replacement_body + newline
                            edit = FileEdit(
                                edit.path,
                                start + 1,
                                1,
                                selected,
                                new_text,
                                edit.reason,
                            )
                if (
                    selected
                    and selected != edit.old_text
                    and selected.rstrip("\r\n") == edit.old_text.rstrip("\r\n")
                ):
                    if selected.endswith("\r\n") and not new_text.endswith(("\n", "\r")):
                        new_text += "\r\n"
                    elif selected.endswith("\n") and not new_text.endswith(("\n", "\r")):
                        new_text += "\n"
                    edit = FileEdit(
                        edit.path,
                        edit.start_line,
                        edit.delete_lines,
                        selected,
                        new_text,
                        edit.reason,
                    )
            normalized_edits.append(
                FileEdit(
                    edit.path,
                    edit.start_line,
                    edit.delete_lines,
                    edit.old_text,
                    new_text,
                    edit.reason,
                )
            )
        file_edits = normalized_edits
        ranges: list[tuple[int, int]] = []
        for edit in file_edits:
            start = edit.start_line - 1
            end = start + edit.delete_lines
            if start > len(lines) or end > len(lines):
                raise UnsafePatchError(
                    f"Edit range is outside {relative.as_posix()}: line {edit.start_line}"
                )
            if "".join(lines[start:end]) != edit.old_text:
                raise UnsafePatchError(
                    f"Edit old_text does not match {relative.as_posix()} at line {edit.start_line}"
                )
            if edit.old_text == edit.new_text:
                raise UnsafePatchError(
                    f"Edit does not change {relative.as_posix()} at line {edit.start_line}"
                )
            for other_start, other_end in ranges:
                same_insertion = start == end == other_start == other_end
                overlaps = (
                    (start < end and other_start <= start < other_end)
                    or (other_start < other_end and start <= other_start < end)
                )
                if same_insertion or overlaps:
                    raise UnsafePatchError(f"Overlapping edits for {relative.as_posix()}")
            ranges.append((start, end))

        replacement_lines = list(lines)
        for edit in sorted(file_edits, key=lambda item: item.start_line, reverse=True):
            start = edit.start_line - 1
            end = start + edit.delete_lines
            replacement_lines[start:end] = edit.new_text.splitlines(keepends=True)
        replacement = "".join(replacement_lines)
        _validate_preservation(
            path,
            original,
            replacement,
            allow_destructive_changes=allow_destructive_changes,
        )
        validated.append(
            ValidatedChange(
                path,
                relative.as_posix(),
                original,
                replacement,
                "; ".join(edit.reason for edit in file_edits),
            )
        )
    return tuple(validated)


def validate_changes(
    root: Path,
    proposal: RepairProposal,
    *,
    max_changes: int = 5,
    max_total_chars: int = 200_000,
    allow_test_changes: bool = False,
    allow_destructive_changes: bool = False,
) -> tuple[ValidatedChange, ...]:
    """Validate a proposal and return only project-contained safe changes."""

    root = root.resolve()
    if proposal.edits and proposal.changes:
        raise UnsafePatchError("A proposal cannot mix line edits and full-file changes")
    if proposal.edits:
        if len(proposal.edits) > 20:
            raise UnsafePatchError("The model proposed more than 20 line edits")
        return _validate_line_edits(
            root,
            proposal.edits,
            max_files=max_changes,
            max_total_chars=max_total_chars,
            allow_test_changes=allow_test_changes,
            allow_destructive_changes=allow_destructive_changes,
        )
    if not proposal.changes:
        raise UnsafePatchError("The model proposed no file edits")
    if len(proposal.changes) > max_changes:
        raise UnsafePatchError(f"The model proposed more than {max_changes} file changes")
    if sum(len(change.content) for change in proposal.changes) > max_total_chars:
        raise UnsafePatchError("The proposed replacement content is too large")

    validated: list[ValidatedChange] = []
    seen: set[Path] = set()
    for change in proposal.changes:
        raw = Path(change.path.replace("\\", "/"))
        if raw.is_absolute() or ".." in raw.parts:
            raise UnsafePatchError(f"Unsafe path: {change.path}")
        path = (root / raw).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise UnsafePatchError(f"Path escapes the project: {change.path}") from exc
        if path in seen:
            raise UnsafePatchError(f"Duplicate path: {change.path}")
        if not allow_test_changes and is_test_path(relative):
            raise UnsafePatchError(f"Test files are protected by default: {relative.as_posix()}")
        if not path.is_file():
            raise UnsafePatchError(f"Only existing files may be changed: {change.path}")
        if path.suffix.lower() not in SOURCE_SUFFIXES and path.name not in CONFIG_NAMES:
            raise UnsafePatchError(f"Unsupported file type: {change.path}")
        try:
            original = read_utf8_exact(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise UnsafePatchError(f"File is not editable UTF-8 text: {change.path}") from exc
        replacement = normalize_newline_style(original, change.content)
        if path.suffix.lower() == ".py" and not allow_destructive_changes:
            replacement = _merge_import_only_python(original, replacement)
        if original == replacement:
            continue
        _validate_preservation(
            path,
            original,
            replacement,
            allow_destructive_changes=allow_destructive_changes,
        )
        validated.append(
            ValidatedChange(path, relative.as_posix(), original, replacement, change.reason)
        )
        seen.add(path)
    if not validated:
        raise UnsafePatchError("The proposal does not change any file content")
    return tuple(validated)


def apply_changes(changes: tuple[ValidatedChange, ...]) -> None:
    """Write validated replacements while preserving exact newline bytes."""

    for change in changes:
        write_utf8_exact(change.path, change.replacement)


def restore_files(backups: dict[Path, bytes]) -> None:
    """Restore byte-for-byte backups after a failed direct-mode attempt."""

    for path, content in backups.items():
        path.write_bytes(content)

