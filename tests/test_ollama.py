from __future__ import annotations

import json

import pytest

from autofix.agent.ollama import FileEdit, OllamaClient, OllamaError, validate_proposal


def test_validates_structured_line_edit_proposal() -> None:
    proposal = validate_proposal(
        {
            "summary": "add import",
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
    assert proposal.changes == ()


def test_rejects_invalid_line_coordinates() -> None:
    with pytest.raises(OllamaError, match="start_line"):
        validate_proposal(
            {
                "summary": "bad",
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


def test_model_schema_excludes_protected_test_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            proposal = {
                "summary": "fix",
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
    )

    schema = captured["format"]
    path_schema = schema["properties"]["edits"]["items"]["properties"]["path"]  # type: ignore[index]
    assert path_schema["enum"] == ["calculator.py"]  # type: ignore[index]
    prompt = captured["messages"][1]["content"]  # type: ignore[index]
    assert "READ-ONLY TEST: test_calculator.py" in prompt
