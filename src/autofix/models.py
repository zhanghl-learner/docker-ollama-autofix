from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BuildStep:
    """One command in a detected build plan."""

    label: str
    argv: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "argv": list(self.argv)}


@dataclass(frozen=True)
class ProjectMatch:
    """A build system detected from one or more project markers."""

    language: str
    build_system: str
    markers: tuple[str, ...]
    steps: tuple[BuildStep, ...]
    confidence: str = "high"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["steps"] = [step.to_dict() for step in self.steps]
        return data


@dataclass(frozen=True)
class ProjectInspection:
    """Detection result for a project directory."""

    root: Path
    matches: tuple[ProjectMatch, ...] = field(default_factory=tuple)

    @property
    def selected(self) -> ProjectMatch | None:
        return self.matches[0] if self.matches else None

    def select(self, build_system: str | None) -> "ProjectInspection":
        """Return an inspection with the requested detected system selected first."""

        if build_system is None:
            return self
        requested = build_system.strip().lower()
        selected = next(
            (match for match in self.matches if match.build_system.lower() == requested),
            None,
        )
        if selected is None:
            detected = ", ".join(match.build_system for match in self.matches) or "none"
            raise ValueError(
                f"Build system '{build_system}' was not detected; detected systems: {detected}"
            )
        return ProjectInspection(
            self.root,
            (selected, *(match for match in self.matches if match is not selected)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "selected": self.selected.to_dict() if self.selected else None,
            "matches": [match.to_dict() for match in self.matches],
        }

