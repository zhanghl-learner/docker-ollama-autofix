from autofix.agent.ollama import FileChange, FileEdit, OllamaClient, OllamaError, RepairProposal
from autofix.agent.diagnostics import RepairEnvironmentError, environment_failure
from autofix.agent.plans import PlanError, RepairPlan, load_repair_plan, save_repair_plan
from autofix.agent.patches import UnsafePatchError
from autofix.agent.repair import ProposalRejection, RepairResult, repair_project
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
    "PlanError",
    "RepairPlan",
    "RepairProposal",
    "RepairEnvironmentError",
    "RepairResult",
    "RollbackError",
    "RollbackResult",
    "ProposalRejection",
    "UnsafePatchError",
    "load_repair_plan",
    "latest_rollback_candidate",
    "repair_project",
    "environment_failure",
    "rollback_report",
    "rolled_back_reports",
    "save_repair_plan",
]

