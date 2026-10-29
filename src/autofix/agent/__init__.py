"""Public repair-agent API exposed to the command-line layer."""

from autofix.agent.ollama import FileChange, FileEdit, OllamaClient, OllamaError, RepairProposal
from autofix.agent.diagnostics import RepairEnvironmentError, environment_failure
from autofix.agent.artifacts import (
    PatchArtifact,
    RepairArtifactError,
    render_standalone_patch,
    save_standalone_patch,
)
from autofix.agent.plans import (
    PlanError,
    PlanProvenance,
    RepairPlan,
    load_repair_plan,
    save_repair_plan,
)
from autofix.agent.patches import UnsafePatchError
from autofix.agent.repair import (
    ProposalRejection,
    RepairProgressCallback,
    RepairProgressEvent,
    RepairResult,
    VerificationFailure,
    repair_project,
)
from autofix.agent.rollback import (
    RollbackError,
    RollbackResult,
    latest_rollback_candidate,
    rollback_report,
    rolled_back_reports,
)

__all__ = [
    "FileChange",
    "FileEdit",
    "OllamaClient",
    "OllamaError",
    "PatchArtifact",
    "PlanError",
    "PlanProvenance",
    "RepairPlan",
    "RepairProposal",
    "RepairProgressCallback",
    "RepairProgressEvent",
    "RepairEnvironmentError",
    "RepairArtifactError",
    "RepairResult",
    "RollbackError",
    "RollbackResult",
    "ProposalRejection",
    "UnsafePatchError",
    "VerificationFailure",
    "load_repair_plan",
    "latest_rollback_candidate",
    "repair_project",
    "render_standalone_patch",
    "environment_failure",
    "rollback_report",
    "rolled_back_reports",
    "save_repair_plan",
    "save_standalone_patch",
]

