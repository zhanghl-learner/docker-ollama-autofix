from __future__ import annotations

from pathlib import Path

import pytest

from autofix.agent.ollama import FileChange, FileEdit, RepairProposal
from autofix.agent.patches import UnsafePatchError, apply_changes, validate_changes


def test_valid_change_creates_diff(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    proposal = RepairProposal("fix", (FileChange("main.py", "value = 2\n", "correct value"),))

    changes = validate_changes(tmp_path, proposal)

    assert len(changes) == 1
    assert "-value = 1" in changes[0].diff
    assert "+value = 2" in changes[0].diff


def test_rejects_path_traversal(tmp_path: Path) -> None:
    proposal = RepairProposal("bad", (FileChange("../outside.py", "bad", "escape"),))

    with pytest.raises(UnsafePatchError, match="Unsafe path"):
        validate_changes(tmp_path, proposal)


def test_rejects_new_file(tmp_path: Path) -> None:
    proposal = RepairProposal("bad", (FileChange("new.py", "pass\n", "create"),))

    with pytest.raises(UnsafePatchError, match="existing files"):
        validate_changes(tmp_path, proposal)


def test_rejects_test_file_changes_by_default(tmp_path: Path) -> None:
    test_file = tmp_path / "test_main.py"
    test_file.write_text("assert True\n", encoding="utf-8")
    proposal = RepairProposal(
        "change test", (FileChange("test_main.py", "assert False\n", "weaken test"),)
    )

    with pytest.raises(UnsafePatchError, match="protected by default"):
        validate_changes(tmp_path, proposal)


def test_allows_explicit_test_file_changes(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_main.py"
    test_file.parent.mkdir()
    test_file.write_text("assert True\n", encoding="utf-8")
    proposal = RepairProposal(
        "correct test", (FileChange("tests/test_main.py", "assert 1 == 1\n", "correct assertion"),)
    )

    changes = validate_changes(tmp_path, proposal, allow_test_changes=True)

    assert len(changes) == 1


def test_safely_merges_import_only_proposal_without_deleting_program(tmp_path: Path) -> None:
    source = tmp_path / "model.py"
    original = """import torch.nn as nn

class Model(nn.Module):
    def __init__(self):
        super().__init__()
        self.layer = nn.Linear(2, 1)

    def forward(self, value):
        return self.layer(value)

model = Model()
print(model)
"""
    source.write_text(original, encoding="utf-8")
    proposal = RepairProposal(
        "add missing import", (FileChange("model.py", "import torch\n", "import torch"),)
    )

    changes = validate_changes(tmp_path, proposal)

    assert changes[0].replacement.startswith("import torch\n\nimport torch.nn as nn")
    assert "class Model(nn.Module):" in changes[0].replacement
    assert "print(model)" in changes[0].replacement
    assert "-class Model" not in changes[0].diff


def test_rejects_destructive_non_import_replacement(tmp_path: Path) -> None:
    source = tmp_path / "model.py"
    source.write_text("\n".join(["class Model:", "    pass"] + [f"value_{n} = {n}" for n in range(10)]) + "\n", encoding="utf-8")
    proposal = RepairProposal("replace", (FileChange("model.py", "print('done')\n", "replace"),))

    with pytest.raises(UnsafePatchError, match="Destructive replacement rejected"):
        validate_changes(tmp_path, proposal)


def test_destructive_change_requires_explicit_override(tmp_path: Path) -> None:
    source = tmp_path / "model.py"
    source.write_text("class Model:\n    pass\n", encoding="utf-8")
    proposal = RepairProposal("replace", (FileChange("model.py", "import torch\n", "replace"),))

    changes = validate_changes(tmp_path, proposal, allow_destructive_changes=True)

    assert changes[0].replacement.replace("\r\n", "\n") == "import torch\n"


def test_line_edit_inserts_without_changing_unmentioned_code(tmp_path: Path) -> None:
    source = tmp_path / "model.py"
    original = "import torch.nn as nn\n\nclass Model(nn.Module):\n    pass\n"
    source.write_text(original, encoding="utf-8")
    proposal = RepairProposal(
        "import torch",
        edits=(FileEdit("model.py", 1, 0, "", "import torch\n", "import referenced module"),),
    )

    change = validate_changes(tmp_path, proposal)[0]

    normalized = change.replacement.replace("\r\n", "\n")
    assert normalized == "import torch\n" + original
    assert normalized[len("import torch\n") :] == original


def test_line_edit_removes_duplicated_adjacent_text_from_insertion(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = "print(torch.tensor([1, 2, 3]))\n"
    source.write_text(original, encoding="utf-8")
    proposal = RepairProposal(
        "import torch",
        edits=(
            FileEdit(
                "main.py",
                1,
                0,
                "",
                "import torch\n" + original.rstrip("\n"),
                "import referenced module",
            ),
        ),
    )

    change = validate_changes(tmp_path, proposal)[0]

    assert change.replacement.replace("\r\n", "\n") == "import torch\n" + original


def test_line_edit_replaces_only_exact_selected_line(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    original = "def add(left, right):\n    return left - right\n\nprint(add(2, 3))\n"
    source.write_text(original, encoding="utf-8")
    proposal = RepairProposal(
        "fix addition",
        edits=(
            FileEdit(
                "calculator.py",
                2,
                1,
                "    return left - right\n",
                "    return left + right\n",
                "use addition",
            ),
        ),
    )

    change = validate_changes(tmp_path, proposal)[0]

    assert change.replacement.replace("\r\n", "\n") == original.replace("left - right", "left + right")
    assert "print(add(2, 3))\n" in change.replacement.replace("\r\n", "\n")


def test_line_edit_normalizes_only_a_missing_final_newline(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    original = "def add(left, right):\n    return left - right\n\n"
    source.write_text(original, encoding="utf-8")
    proposal = RepairProposal(
        "fix addition",
        edits=(
            FileEdit(
                "calculator.py",
                1,
                2,
                "def add(left, right):\n    return left - right",
                "def add(left, right):\n    return left + right",
                "use addition",
            ),
        ),
    )

    change = validate_changes(tmp_path, proposal)[0]

    assert change.replacement.replace("\r\n", "\n") == original.replace("left - right", "left + right")


def test_line_edit_canonicalizes_mislabeled_block_to_minimal_replacement(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    original = "def add(left, right):\n    return left - right\n\nprint(add(2, 3))\n"
    source.write_text(original, encoding="utf-8")
    proposal = RepairProposal(
        "fix addition",
        edits=(
            FileEdit(
                "calculator.py",
                1,
                0,
                "def add(left, right):",
                "def add(left, right):\n    return left + right",
                "use addition",
            ),
        ),
    )

    change = validate_changes(tmp_path, proposal)[0]

    assert change.replacement.replace("\r\n", "\n") == original.replace("left - right", "left + right")
    assert change.diff.count("-    return left - right") == 1
    assert change.diff.count("+    return left + right") == 1


def test_line_edit_rejects_wrong_original_text(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    proposal = RepairProposal(
        "change",
        edits=(FileEdit("main.py", 1, 1, "value = 0\n", "value = 2\n", "change"),),
    )

    with pytest.raises(UnsafePatchError, match="old_text does not match"):
        validate_changes(tmp_path, proposal)


def test_line_edit_safely_reanchors_unique_exact_text(tmp_path: Path) -> None:
    source = tmp_path / "calculator.go"
    source.write_text(
        "package calculator\n\nfunc Add(left, right int) int {\n\treturn left - right\n}\n",
        encoding="utf-8",
    )
    proposal = RepairProposal(
        "fix addition",
        edits=(
            FileEdit(
                "calculator.go",
                3,
                1,
                "\treturn left - right\n",
                "\treturn left + right\n",
                "use addition",
            ),
        ),
    )

    change = validate_changes(tmp_path, proposal)[0]

    assert "\treturn left + right\n" in change.replacement.replace("\r\n", "\n")


def test_line_edit_does_not_reanchor_ambiguous_text(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("header = True\nvalue = 1\nvalue = 1\n", encoding="utf-8")
    proposal = RepairProposal(
        "change",
        edits=(FileEdit("main.py", 1, 1, "value = 1\n", "value = 2\n", "change"),),
    )

    with pytest.raises(UnsafePatchError, match="old_text does not match"):
        validate_changes(tmp_path, proposal)


def test_line_edit_reuses_unique_source_indentation(tmp_path: Path) -> None:
    source = tmp_path / "Calculator.java"
    source.write_text(
        "class Calculator {\n    int add(int left, int right) {\n"
        "        return left - right;\n    }\n}\n",
        encoding="utf-8",
    )
    proposal = RepairProposal(
        "fix addition",
        edits=(
            FileEdit(
                "Calculator.java",
                3,
                1,
                "    return left - right;",
                "    return left + right;",
                "use addition",
            ),
        ),
    )

    change = validate_changes(tmp_path, proposal)[0]

    assert "        return left + right;\n" in change.replacement.replace("\r\n", "\n")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_apply_changes_preserves_source_newlines(tmp_path: Path, newline: str) -> None:
    source = tmp_path / "main.py"
    source.write_bytes(f"value = 1{newline}print(value){newline}".encode("utf-8"))
    proposal = RepairProposal(
        "change value",
        edits=(
            FileEdit(
                "main.py",
                1,
                1,
                f"value = 1{newline}",
                f"value = 2{newline}",
                "change value",
            ),
        ),
    )

    changes = validate_changes(tmp_path, proposal)
    apply_changes(changes)

    assert source.read_bytes() == f"value = 2{newline}print(value){newline}".encode("utf-8")


def test_line_edits_reject_overlapping_ranges(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    proposal = RepairProposal(
        "overlap",
        edits=(
            FileEdit("main.py", 1, 2, "one\ntwo\n", "first\n", "first"),
            FileEdit("main.py", 2, 1, "two\n", "second\n", "second"),
        ),
    )

    with pytest.raises(UnsafePatchError, match="Overlapping edits"):
        validate_changes(tmp_path, proposal)

