from __future__ import annotations

import json

import pytest

from autofix.agent.ollama import FileEdit, OllamaClient, OllamaError, validate_proposal


def test_validates_structured_line_edit_proposal() -> None:
    proposal = validate_proposal(
        {
            "summary": "add import",
            "diagnosis": "main.py line 1 references torch before it is imported",
            "edits": [
                {
                    "path": "main.py",
                    "start_line": 1,
                    "incorrect_code": "",
                    "corrected_code": "import torch\n",
                    "reason": "torch is referenced",
                }
            ],
        }
    )

    assert proposal.edits == (
        FileEdit("main.py", 1, 0, "", "import torch\n", "torch is referenced"),
    )
    assert "line 1" in proposal.diagnosis
    assert proposal.changes == ()


def test_rejects_invalid_line_coordinates() -> None:
    with pytest.raises(OllamaError, match="start_line"):
        validate_proposal(
            {
                "summary": "bad",
                "diagnosis": "main.py requires an insertion at its first line",
                "edits": [
                    {
                        "path": "main.py",
                        "start_line": 0,
                        "incorrect_code": "",
                        "corrected_code": "pass\n",
                        "reason": "bad line",
                    }
                ],
            }
        )


def test_rejects_proposal_without_diagnosis() -> None:
    with pytest.raises(OllamaError, match="diagnosis"):
        validate_proposal({"summary": "guess", "edits": []})


def test_model_schema_excludes_protected_test_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            proposal = {
                "summary": "fix",
                "diagnosis": "calculator.py line 1 has the wrong value",
                "edits": [
                    {
                        "path": "calculator.py",
                        "start_line": 1,
                        "incorrect_code": "value = 1\n",
                        "corrected_code": "value = 2\n",
                        "reason": "fix",
                    }
                ],
            }
            return {"message": {"content": json.dumps(proposal)}}

    def fake_post(url: str, *, json: dict[str, object], timeout: float) -> Response:
        captured.update(json)
        return Response()

    monkeypatch.setattr("autofix.agent.ollama.httpx.post", fake_post)

    OllamaClient().propose(
        model="test",
        failure="assertion failed",
        files={"calculator.py": "value = 1\n", "test_calculator.py": "assert False\n"},
        context_reasons={
            "calculator.py": "local-import:test_calculator.py",
            "test_calculator.py": "build-output",
        },
    )

    schema = captured["format"]
    path_schema = schema["properties"]["edits"]["items"]["properties"]["path"]  # type: ignore[index]
    assert path_schema["enum"] == ["calculator.py"]  # type: ignore[index]
    prompt = captured["messages"][1]["content"]  # type: ignore[index]
    assert "READ-ONLY TEST: test_calculator.py" in prompt
    assert "guard excludes the failing input" in prompt
    assert "inner conditional whose predicate is already guaranteed" in prompt
    assert "CONTEXT SELECTION (metadata only" in prompt
    assert "calculator.py: local-import:test_calculator.py" in prompt
    assert "test_calculator.py: build-output" in prompt
    assert captured["options"] == {"temperature": 0}

    OllamaClient().propose(
        model="test",
        failure="assertion failed",
        files={"calculator.py": "value = 1\n"},
        previous_attempt="ATTEMPT 1 failed after changing line 1",
    )
    retry_prompt = captured["messages"][1]["content"]  # type: ignore[index]
    assert retry_prompt.rfind("Previous attempt result") > retry_prompt.rfind(
        "PROJECT FILES"
    )
    assert captured["options"] == {"temperature": 0.15}

    OllamaClient().propose(
        model="test",
        failure="assertion failed",
        files={"calculator.py": "value = 1\n"},
        previous_attempt=(
            "DUPLICATE RECOVERY REQUIREMENT: repeated an earlier edit byte-for-byte"
        ),
    )
    duplicate_retry_prompt = captured["messages"][1]["content"]  # type: ignore[index]
    assert "equivalent nested condition" in duplicate_retry_prompt
    assert captured["options"] == {"temperature": 0.25}
