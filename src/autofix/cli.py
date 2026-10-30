"""Typer command-line interface for inspection, repair, audit, and bundles."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from time import monotonic

import typer
from rich.console import Console
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table

from autofix import __version__
from autofix.agent import (
    OllamaClient,
    OllamaError,
    PlanError,
    PlanProvenance,
    RepairArtifactError,
    RepairEnvironmentError,
    RollbackError,
    latest_rollback_candidate,
    UnsafePatchError,
    load_repair_plan,
    repair_project,
    rollback_report,
    rolled_back_reports,
    save_repair_plan,
)
from autofix.builders import (
    DockerConfig,
    DockerConfigurationError,
    build_docker_step,
    default_image,
    parse_command,
    run_build,
    run_build_docker,
)
from autofix.ci import CIExitCode, CIResult, result_from_build, save_ci_result
from autofix.detectors import inspect_project
from autofix.evaluation import (
    EvaluationError,
    evaluate_project,
    load_evaluation_manifest,
)
from autofix.evaluation_suite import (
    EvaluationSuiteError,
    EvaluationSuiteProgressEvent,
    evaluate_suite,
)
from autofix.models import BuildStep, ProjectInspection
from autofix.repository import (
    GitCommitError,
    GitInspectionError,
    GitWorktreeError,
    create_local_repair_commit,
    inspect_git_workspace,
    validate_git_repair_workspace,
)
from autofix.config import (
    AutoFixConfig,
    ConfigError,
    create_config,
    load_config,
    save_global_config,
)
from autofix.runtime import (
    BundleError,
    check_environment,
    check_offline_readiness,
    create_bundle,
    install_bundle,
    verify_bundle,
)
from autofix.runtime.tools import find_executable
from autofix.property_testing import (
    PropertyCheckError,
    apply_property_promotion,
    check_property_candidate,
    generate_property_candidate,
    promote_property_candidate,
)
from autofix.javascript_property import check_javascript_property_candidate
from autofix.static_analysis import inspect_static_analysis


app = typer.Typer(
    name="autofix",
    help="Local-first automated build and code repair tool.",
    no_args_is_help=True,
)
bundle_app = typer.Typer(help="Create, verify, and install offline runtime bundles.")
app.add_typer(bundle_app, name="bundle")
console = Console()


class _SuiteProgressRenderer:
    """Render long-running suite milestones while keeping a live wait spinner."""

    def __init__(self) -> None:
        self.status: Status = console.status("Preparing evaluation suite...", spinner="dots")
        self.active = False
        self.case_started_at: dict[str, float] = {}

    def start_wait(self, message: str) -> None:
        """Start or update the animated status shown during blocking work."""

        self.status.update(message)
        if not self.active:
            self.status.start()
            self.active = True

    def stop_wait(self) -> None:
        """Stop the live status before emitting persistent terminal lines."""

        if self.active:
            self.status.stop()
            self.active = False

    def close(self) -> None:
        """Guarantee that no spinner remains active after completion or failure."""

        self.stop_wait()

    def __call__(self, event: EvaluationSuiteProgressEvent) -> None:
        prefix = f"[{event.case_index}/{event.case_total}] {event.relative_root}"
        if event.phase == "case_started":
            self.stop_wait()
            self.case_started_at[event.relative_root] = monotonic()
            console.print(f"\n[cyan]{prefix}: reproducing the declared failure...[/cyan]")
            self.start_wait(f"[cyan]{prefix}: running baseline verification...[/cyan]")
            return
        if event.phase == "case_completed":
            self.stop_wait()
            color = "green" if event.status == "passed" else "red"
            console.print(
                f"[{color}]{prefix}: {event.status} in "
                f"{event.duration_seconds:.3f}s[/{color}]"
            )
            return
        repair = event.repair
        if event.phase != "repair_progress" or repair is None:
            return
        elapsed = event.duration_seconds
        if repair.phase == "initial_verification_started":
            self.start_wait(
                f"[cyan]{prefix}: preparing isolation and confirming baseline "
                f"({elapsed:.1f}s)...[/cyan]"
            )
        elif repair.phase == "context_selected":
            self.stop_wait()
            selected = ", ".join(
                f"{item.path} ({item.reason})"
                for item in repair.context_selections[:6]
            )
            remainder = len(repair.context_selections) - 6
            if remainder > 0:
                selected += f", +{remainder} more"
            console.print(
                f"[cyan]{prefix}: attempt {repair.attempt} context — "
                f"{selected or 'no readable project source'}[/cyan]"
            )
        elif repair.phase == "model_waiting":
            self.start_wait(
                f"[yellow]{prefix}: attempt {repair.attempt}/{repair.max_attempts}, "
                f"waiting for Ollama ({elapsed:.1f}s)...[/yellow]"
            )
        elif repair.phase == "proposal_received":
            self.stop_wait()
            proposal = repair.proposal
            if proposal is not None:
                console.print(
                    f"[yellow]{prefix}: attempt {repair.attempt} proposal — "
                    f"{proposal.summary}[/yellow]"
                )
                if proposal.diagnosis:
                    console.print(f"  Diagnosis: {proposal.diagnosis}")
                for edit in proposal.edits:
                    console.print(
                        f"  Edit: {edit.path}:{edit.start_line} "
                        f"(replace {edit.delete_lines} line(s)) — {edit.reason}"
                    )
                for change in proposal.changes:
                    console.print(f"  Replace: {change.path} — {change.reason}")
        elif repair.phase == "proposal_rejected":
            self.stop_wait()
            console.print(
                f"[red]{prefix}: attempt {repair.attempt} rejected — "
                f"{repair.reason}[/red]"
            )
        elif repair.phase == "verification_started":
            paths = ", ".join(change.relative_path for change in repair.changes)
            self.start_wait(
                f"[blue]{prefix}: attempt {repair.attempt}, verifying "
                f"{paths or 'candidate changes'} ({elapsed:.1f}s)...[/blue]"
            )
        elif repair.phase == "verification_failed":
            self.stop_wait()
            step = repair.build.steps[-1] if repair.build and repair.build.steps else None
            detail = (
                f"{step.label} exited {step.exit_code}" if step else "build failed"
            )
            console.print(
                f"[red]{prefix}: attempt {repair.attempt} failed verification — "
                f"{detail}[/red]"
            )
        elif repair.phase == "repair_succeeded":
            self.stop_wait()
            paths = ", ".join(change.relative_path for change in repair.changes)
            console.print(
                f"[green]{prefix}: attempt {repair.attempt} verified successfully — "
                f"{paths or 'no persisted changes'}[/green]"
            )
        elif repair.phase in {"environment_failure", "repair_exhausted"}:
            self.stop_wait()
            console.print(f"[red]{prefix}: {repair.reason or repair.phase}[/red]")


def _print_verified_suite_changes(result: object) -> None:
    """Print verified source diffs after the aggregate table for human review."""

    cases = getattr(result, "cases", ())
    verified = [
        case
        for case in cases
        if case.passed and case.result is not None and case.result.repair is not None
    ]
    if not verified:
        return
    console.print("\n[bold green]Verified repair changes:[/bold green]")
    for case in verified:
        repair = case.result.repair
        assert repair is not None
        proposal = repair.proposals[-1] if repair.proposals else None
        console.print(f"\n[bold]{case.relative_root}[/bold]")
        if proposal is not None:
            console.print(f"Summary: {proposal.summary}")
            if proposal.diagnosis:
                console.print(f"Diagnosis: {proposal.diagnosis}")
        for change in repair.changes:
            console.print(f"Changed: [cyan]{change.relative_path}[/cyan] — {change.reason}")
            console.print(Syntax(change.diff, "diff", word_wrap=False))


def _json(data: object) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False))


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"autofix {__version__}")
        raise typer.Exit()


def _docker_config(
    inspection: object,
    image: str | None,
    cpus: float,
    memory: str,
    pids_limit: int,
    allow_network: bool,
) -> DockerConfig:
    selected = getattr(inspection, "selected", None)
    selected_image = image or default_image(selected)
    if not selected_image:
        raise DockerConfigurationError("No default image is known; pass --docker-image")
    return DockerConfig(selected_image, cpus, memory, pids_limit, allow_network)


def _require_docker() -> None:
    if find_executable("docker") is None:
        raise DockerConfigurationError(
            "Docker is not installed or not available in PATH; run 'autofix doctor' for details"
        )


def _load_config_or_exit(root: Path) -> AutoFixConfig | None:
    try:
        return load_config(root)
    except ConfigError as exc:
        console.print(f"[red]Configuration error: {exc}[/red]")
        raise typer.Exit(code=2) from exc


def _selected_build_command(
    explicit: str | None, config: AutoFixConfig | None, inspection: object
) -> str | None:
    if explicit:
        return explicit
    configured = config.command if config else None
    selected = getattr(inspection, "selected", None)
    detected = shlex.join(selected.steps[0].argv) if selected and selected.steps else None
    # AutoFix <=0.5 generated compileall as a fallback for script projects.
    # Transparently prefer a newly detected executable entry point.
    if configured == "python -m compileall -q ." and detected and detected != configured:
        return detected
    return configured or detected


def _selected_build_steps(
    explicit_command: str | None,
    explicit_steps: list[str] | None,
    config: AutoFixConfig | None,
    inspection: ProjectInspection,
) -> tuple[BuildStep, ...]:
    requested_steps = tuple(explicit_steps or ())
    if explicit_command and requested_steps:
        raise ValueError("--command and --step cannot be used together")
    if requested_steps:
        commands = requested_steps
    elif explicit_command:
        commands = (explicit_command,)
    elif config and config.commands:
        commands = config.commands
    elif config and config.command:
        selected = _selected_build_command(None, config, inspection)
        commands = (selected,) if selected else ()
    else:
        return inspection.selected.steps if inspection.selected else ()

    parsed: list[BuildStep] = []
    for index, command in enumerate(commands, start=1):
        step = parse_command(command)
        label = "Custom command" if len(commands) == 1 else f"Custom command {index}"
        parsed.append(BuildStep(label, step.argv))
    return tuple(parsed)


def _append_static_analysis_steps(
    root: Path,
    steps: tuple[BuildStep, ...],
    *,
    enabled: bool,
    docker: bool,
    isolated_workspace: bool = False,
) -> tuple[BuildStep, ...]:
    """Append every declared local analyzer without silently skipping tools."""

    if not enabled:
        return steps
    if docker:
        raise ValueError(
            "--with-static-analysis currently supports only the local runner; "
            "use explicit --step commands for analyzers bundled in a Docker image"
        )
    inspection = inspect_static_analysis(root)
    if not inspection.analyzers:
        raise ValueError(
            "--with-static-analysis found no supported analyzer declared by the project"
        )
    unavailable = tuple(
        analyzer for analyzer in inspection.analyzers if not analyzer.available
    )
    if unavailable:
        details = "; ".join(
            f"{analyzer.name}: {analyzer.availability}" for analyzer in unavailable
        )
        raise ValueError(
            "--with-static-analysis requires every declared analyzer to be locally "
            f"available ({details})"
        )
    nonportable = tuple(
        analyzer
        for analyzer in inspection.analyzers
        if analyzer.availability in {"project-venv", "project node_modules"}
    )
    if isolated_workspace and nonportable:
        names = ", ".join(analyzer.name for analyzer in nonportable)
        raise ValueError(
            "repair verifies in a detached worktree where ignored project-local tool "
            f"directories are unavailable ({names}); activate Python tools on PATH or "
            "use explicit Docker steps with dependencies included in the image"
        )
    existing = {step.argv for step in steps}
    additions = tuple(
        analyzer.step
        for analyzer in inspection.analyzers
        if analyzer.step.argv not in existing
    )
    return (*steps, *additions)


def _select_inspection_or_exit(
    inspection: ProjectInspection,
    requested: str | None,
) -> ProjectInspection:
    try:
        return inspection.select(requested)
    except ValueError as exc:
        console.print(f"[red]Build-system selection error: {exc}[/red]")
        raise typer.Exit(code=2) from exc


def _announce_command_upgrade(config: AutoFixConfig | None, selected_command: str | None) -> None:
    if (
        config
        and config.command == "python -m compileall -q ."
        and selected_command
        and selected_command != config.command
    ):
        console.print(
            f"[cyan]Detected executable Python entry point; using: {selected_command}[/cyan]"
        )


def _same_step_commands(
    left: tuple[BuildStep, ...], right: tuple[BuildStep, ...]
) -> bool:
    """Compare executable arguments while allowing presentation labels to differ."""

    return tuple(step.argv for step in left) == tuple(step.argv for step in right)


def _commit_verified_result(result: object, message: str) -> str:
    """Create one local commit containing only the verified repair files."""

    if not getattr(result, "succeeded", False) or not getattr(result, "applied", False):
        raise GitCommitError("A local commit requires an applied, verified repair")
    repository_root = getattr(result, "repository_root", None)
    baseline_commit = getattr(result, "baseline_commit", None)
    repository_fingerprint = getattr(result, "repository_fingerprint", None)
    project_root = getattr(result, "project_root", None)
    diff_sha256 = getattr(result, "diff_sha256", None)
    if (
        not all(
            isinstance(value, str) and value
            for value in (baseline_commit, repository_fingerprint, diff_sha256)
        )
        or repository_root is None
        or project_root is None
    ):
        raise GitCommitError("--commit requires a provenance-bound Git repair")
    files = tuple(
        (
            change.relative_path,
            hashlib.sha256(change.replacement.encode("utf-8")).hexdigest(),
        )
        for change in getattr(result, "changes", ())
    )
    return create_local_repair_commit(
        project_root,
        files,
        message=message,
        baseline_commit=baseline_commit,
        repository_fingerprint=repository_fingerprint,
        diff_sha256=diff_sha256,
        artifacts=(
            ((result.patch_path, result.patch_sha256),)
            if getattr(result, "patch_path", None) is not None
            and isinstance(getattr(result, "patch_sha256", None), str)
            else ()
        ),
    )


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """Inspect and build local projects before automated repair."""


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    ollama_url: str = typer.Option("http://127.0.0.1:11434", help="Ollama API base URL."),
) -> None:
    """Check local tools and the Ollama service."""

    report = check_environment(ollama_url)
    if json_output:
        _json(report)
        return

    table = Table(title="AutoFix environment check")
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Details")
    for name in ("python", "git", "docker", "podman", "ollama"):
        item = report[name]
        status = "[green]ready[/green]" if item["available"] else "[yellow]missing[/yellow]"
        table.add_row(name, status, item["version"] or "not found")
    service = report["ollama_service"]
    status = "[green]ready[/green]" if service["available"] else "[yellow]unavailable[/yellow]"
    details = ", ".join(service["models"]) if service["models"] else service["error"] or "no models"
    table.add_row("ollama API", status, details)
    console.print(table)


@app.command()
def configure(
    model: str = typer.Option(..., help="Default installed Ollama model."),
    docker_image: str = typer.Option(..., help="Default Docker build image."),
    offline: bool = typer.Option(
        True, "--offline/--online", help="Disable container networking by default."
    ),
    dependencies_bundled: bool = typer.Option(
        True,
        "--dependencies-bundled/--dependencies-not-bundled",
        help="Declare whether the default image contains build dependencies.",
    ),
) -> None:
    """Save user-level defaults inherited automatically by new projects."""

    try:
        path = save_global_config(
            docker_image=docker_image,
            ollama_model=model,
            offline=offline,
            dependencies_bundled=dependencies_bundled,
        )
    except ConfigError as exc:
        console.print(f"[red]Configuration stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    console.print(f"[green]Saved AutoFix user defaults: {path}[/green]")
    console.print(f"Docker image: {docker_image}")
    console.print(f"Ollama model: {model}")
    console.print(f"Offline policy: {'enabled' if offline else 'disabled'}")


@app.command("offline-check")
def offline_check(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    docker_image: str | None = typer.Option(None, help="Override the image from autofix.yml."),
    model: str | None = typer.Option(None, help="Override the Ollama model from autofix.yml."),
    build_system: str | None = typer.Option(
        None, help="Select one of the detected build systems."
    ),
    ollama_url: str = typer.Option("http://127.0.0.1:11434", help="Ollama API base URL."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Check whether local resources are ready for a fully offline repair."""

    try:
        report = check_offline_readiness(
            path,
            image=docker_image,
            model=model,
            build_system=build_system,
            ollama_url=ollama_url,
        )
    except (ConfigError, ValueError) as exc:
        console.print(f"[red]Configuration error: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    if json_output:
        _json(report.to_dict())
    else:
        table = Table(title=f"Offline readiness: {report.root}")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Details")
        for item in report.checks:
            status = "[green]ready[/green]" if item.ready else "[red]missing[/red]"
            table.add_row(item.name, status, item.details)
        console.print(table)
        console.print(
            "[green]Ready for offline repair.[/green]"
            if report.ready
            else "[red]Not ready for offline repair.[/red]"
        )
    if not report.ready:
        raise typer.Exit(code=1)


def _print_bundle_report(action: str, report: object, json_output: bool) -> None:
    data = report.to_dict()  # type: ignore[attr-defined]
    if json_output:
        _json(data)
        return
    size_mb = data["total_size"] / (1024 * 1024)
    console.print(f"[green]Bundle {action}:[/green] {data['path']}")
    console.print(f"Docker image: {data['docker_image']}")
    console.print(f"Ollama model: {data['ollama_model']}")
    console.print(f"Payload: {size_mb:.1f} MiB ({len(data['files'])} files)")


@bundle_app.command("create")
def bundle_create(
    output: Path = typer.Argument(..., help="New directory to create."),
    path: Path = typer.Option(Path("."), "--project", exists=True, file_okay=False, resolve_path=True),
    docker_image: str | None = typer.Option(None, help="Override the image from autofix.yml."),
    model: str | None = typer.Option(None, help="Override the model from autofix.yml."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Export the Docker image, Ollama model, config, and checksums."""

    config = _load_config_or_exit(path)
    selected_image = docker_image or (config.docker_image if config else None)
    selected_model = model or (config.ollama_model if config else None)
    if not config or not selected_image or not selected_model:
        console.print("[red]Bundle creation requires autofix.yml, a Docker image, and an Ollama model.[/red]")
        raise typer.Exit(code=2)
    try:
        report = create_bundle(
            output,
            docker_image=selected_image,
            ollama_model=selected_model,
            config_path=config.path,
            project_root=path,
        )
    except BundleError as exc:
        console.print(f"[red]Bundle creation failed: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    _print_bundle_report("created", report, json_output)


@bundle_app.command("verify")
def bundle_verify(
    bundle: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Verify every payload file against the SHA-256 manifest."""

    try:
        report = verify_bundle(bundle)
    except BundleError as exc:
        console.print(f"[red]Bundle verification failed: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    _print_bundle_report("verified", report, json_output)


@bundle_app.command("install")
def bundle_install(
    bundle: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Verify and import the bundled Docker image and Ollama model."""

    try:
        report = install_bundle(bundle)
    except BundleError as exc:
        console.print(f"[red]Bundle installation failed: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    _print_bundle_report("installed", report, json_output)



@app.command("inspect")
def inspect_command(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    build_system: str | None = typer.Option(
        None, help="Select one of the detected build systems."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Detect project languages, build systems, and commands."""

    inspection = _select_inspection_or_exit(inspect_project(path), build_system)
    if json_output:
        _json(inspection.to_dict())
        return
    if not inspection.matches:
        console.print(f"[yellow]No supported project was detected in {inspection.root}[/yellow]")
        raise typer.Exit(code=2)

    table = Table(title=f"Detected project: {inspection.root}")
    table.add_column("Selected")
    table.add_column("Language")
    table.add_column("Build system")
    table.add_column("Markers")
    table.add_column("Commands")
    for index, match in enumerate(inspection.matches):
        commands = "\n".join(" ".join(step.argv) for step in match.steps)
        table.add_row("yes" if index == 0 else "", match.language, match.build_system, ", ".join(match.markers), commands)
    console.print(table)


@app.command("analyze")
def analyze_command(
    path: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, resolve_path=True
    ),
    run: bool = typer.Option(
        False,
        "--run",
        help="Execute declared analyzers that are already locally available.",
    ),
    timeout: float = typer.Option(300, min=0.1, help="Timeout for each analyzer."),
    no_save: bool = typer.Option(
        False, "--no-save", help="Do not save a build-style analysis report."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print machine-readable JSON."
    ),
) -> None:
    """Discover configured static analyzers and optionally run them."""

    inspection = inspect_static_analysis(path)
    result = None
    if run and inspection.available_steps:
        result = run_build(
            inspection.root,
            inspection.available_steps,
            timeout_seconds=timeout,
            save_report=not no_save,
        )

    payload = {
        "schema": "autofix-static-analysis",
        "version": 1,
        **inspection.to_dict(),
        "run_requested": run,
        "run": result.to_dict() if result else None,
    }
    if json_output:
        _json(payload)
    else:
        table = Table(title=f"Static analysis: {inspection.root}")
        table.add_column("Analyzer")
        table.add_column("Language")
        table.add_column("Declared by")
        table.add_column("Available")
        table.add_column("Command")
        table.add_column("Details")
        for analyzer in inspection.analyzers:
            table.add_row(
                analyzer.name,
                analyzer.language,
                ", ".join(analyzer.markers),
                "yes" if analyzer.available else "no",
                " ".join(analyzer.step.argv),
                analyzer.availability,
            )
        console.print(table)
        if not inspection.analyzers:
            console.print(
                "[yellow]No explicitly configured supported analyzer was found.[/yellow]"
            )
        elif run and not inspection.available_steps:
            console.print(
                "[yellow]No declared analyzer is locally available; nothing was run.[/yellow]"
            )
        if result:
            for step in result.steps:
                state = "[green]passed[/green]" if step.succeeded else "[red]failed[/red]"
                console.print(f"[{step.label}] {state} ({step.duration_seconds:.3f}s)")
                if step.stdout:
                    console.print(step.stdout.rstrip())
                if step.stderr:
                    console.print(step.stderr.rstrip(), style="yellow")
            if result.report_path:
                console.print(f"Report: {result.report_path}")

    if run and not inspection.available_steps:
        raise typer.Exit(code=2)
    if result is not None and not result.succeeded:
        raise typer.Exit(code=result.exit_code or 1)


@app.command("property-check")
def property_check_command(
    candidate: Path = typer.Argument(
        ..., exists=True, dir_okay=False, resolve_path=True
    ),
    path: Path = typer.Option(
        Path("."),
        "--project",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Project whose reviewed repair plan is being challenged.",
    ),
    plan: Path = typer.Option(
        ...,
        "--plan",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Reviewed repair plan containing the fixed version and verification matrix.",
    ),
    execute: bool = typer.Option(
        False,
        "--run",
        help="Execute the candidate twice against baseline and fixed isolated copies.",
    ),
    timeout: float = typer.Option(
        300, min=0.1, help="Timeout for each isolated step."
    ),
    no_save: bool = typer.Option(
        False, "--no-save", help="Do not save the property-check JSON report."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print machine-readable JSON."
    ),
) -> None:
    """Preview or differentially validate one Python/Hypothesis candidate."""

    try:
        if execute and not json_output:
            with console.status(
                "[cyan]Running isolated baseline/fixed property checks...[/cyan]",
                spinner="dots",
            ):
                result = check_property_candidate(
                    path,
                    candidate,
                    plan,
                    run=True,
                    timeout_seconds=timeout,
                    save_report=not no_save,
                )
        else:
            result = check_property_candidate(
                path,
                candidate,
                plan,
                run=execute,
                timeout_seconds=timeout,
                save_report=execute and not no_save,
            )
    except (OSError, PlanError, PropertyCheckError) as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "schema": "autofix-property-check-error",
                        "version": 1,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            console.print(f"[red]Property check stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if json_output:
        _json(result.to_dict())
    else:
        console.print(f"Candidate: [cyan]{result.candidate.path}[/cyan]")
        console.print(f"SHA-256: {result.candidate.sha256}")
        console.print(f"Plan: [cyan]{result.plan_path}[/cyan]")
        console.print(
            "Isolated target: "
            f"[cyan]{result.candidate.target_path}[/cyan] (original project unchanged)"
        )
        console.print(Syntax(result.candidate.source, "python", word_wrap=False))
        if not execute:
            console.print(
                "[yellow]Preview only. Re-run with --run after reviewing executable "
                "candidate code.[/yellow]"
            )
        else:
            table = Table(title="Property candidate differential evidence")
            table.add_column("Phase")
            table.add_column("Attempt")
            table.add_column("Result")
            table.add_column("Exit")
            for evidence in (
                *result.baseline_runs,
                *result.fixed_runs,
                *result.fixed_verification,
            ):
                table.add_row(
                    evidence.phase,
                    str(evidence.attempt),
                    "passed" if evidence.succeeded else "failed",
                    str(evidence.exit_code),
                )
            console.print(table)
            color = "green" if result.passed else "red"
            console.print(f"[{color}]Verdict: {result.verdict}[/{color}]")
            if result.report_path:
                console.print(f"Report: {result.report_path}")
    if execute and not result.passed:
        raise typer.Exit(code=1)


@app.command("property-js-check")
def property_javascript_check_command(
    candidate: Path = typer.Argument(
        ..., exists=True, dir_okay=False, resolve_path=True
    ),
    path: Path = typer.Option(
        Path("."),
        "--project",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="JavaScript project whose reviewed repair plan is being challenged.",
    ),
    plan: Path = typer.Option(
        ...,
        "--plan",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Reviewed local repair plan containing the fixed implementation.",
    ),
    execute: bool = typer.Option(
        False,
        "--run",
        help="Execute twice against isolated baseline and fixed project copies.",
    ),
    timeout: float = typer.Option(300, min=0.1, help="Timeout for each isolated step."),
    no_save: bool = typer.Option(
        False, "--no-save", help="Do not save the JavaScript property report."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print machine-readable JSON."
    ),
) -> None:
    """Preview or differentially validate one JavaScript/fast-check candidate."""

    try:
        if execute and not json_output:
            with console.status(
                "[cyan]Running isolated JavaScript baseline/fixed checks...[/cyan]",
                spinner="dots",
            ):
                result = check_javascript_property_candidate(
                    path,
                    candidate,
                    plan,
                    run=True,
                    timeout_seconds=timeout,
                    save_report=not no_save,
                )
        else:
            result = check_javascript_property_candidate(
                path,
                candidate,
                plan,
                run=execute,
                timeout_seconds=timeout,
                save_report=execute and not no_save,
            )
    except (OSError, PlanError, PropertyCheckError) as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "schema": "autofix-javascript-property-check-error",
                        "version": 1,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            console.print(f"[red]JavaScript property check stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if json_output:
        _json(result.to_dict())
    else:
        console.print(f"Candidate: [cyan]{result.candidate.path}[/cyan]")
        console.print(f"SHA-256: {result.candidate.sha256}")
        console.print(f"Plan: [cyan]{result.plan_path}[/cyan]")
        console.print(
            "Isolated target: "
            f"[cyan]{result.candidate.target_path}[/cyan] (original project unchanged)"
        )
        console.print(Syntax(result.candidate.source, "javascript", word_wrap=False))
        if not execute:
            console.print(
                "[yellow]Preview only. Re-run with --run after reviewing executable "
                "candidate code.[/yellow]"
            )
        else:
            table = Table(title="JavaScript property differential evidence")
            table.add_column("Phase")
            table.add_column("Attempt")
            table.add_column("Result")
            table.add_column("Exit")
            for evidence in (
                *result.baseline_runs,
                *result.fixed_runs,
                *result.fixed_verification,
            ):
                table.add_row(
                    evidence.phase,
                    str(evidence.attempt),
                    "passed" if evidence.succeeded else "failed",
                    str(evidence.exit_code),
                )
            console.print(table)
            color = "green" if result.passed else "red"
            console.print(f"[{color}]Verdict: {result.verdict}[/{color}]")
            if result.report_path:
                console.print(f"Report: {result.report_path}")
    if execute and not result.passed:
        raise typer.Exit(code=1)


@app.command("property-generate")
def property_generate_command(
    path: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, resolve_path=True
    ),
    plan: Path = typer.Option(
        ...,
        "--plan",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Reviewed repair plan used to derive a regression property.",
    ),
    model: str | None = typer.Option(
        None, help="Installed Ollama model; defaults to project config or first local model."
    ),
    ollama_url: str = typer.Option(
        "http://127.0.0.1:11434", help="Ollama API base URL."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print machine-readable JSON."
    ),
) -> None:
    """Generate a review-only Hypothesis candidate; never execute it."""

    config = _load_config_or_exit(path)
    client = OllamaClient(ollama_url)
    try:
        selected_model = model or (config.ollama_model if config else None)
        if selected_model is None:
            models = client.list_models()
            if not models:
                raise OllamaError("Ollama has no installed models")
            selected_model = models[0]
        if not json_output:
            console.print(f"Using local model: [bold]{selected_model}[/bold]")
            with console.status(
                "[yellow]Generating a review-only property candidate...[/yellow]",
                spinner="dots",
            ):
                result = generate_property_candidate(
                    path,
                    plan,
                    client,
                    model=selected_model,
                )
        else:
            result = generate_property_candidate(
                path,
                plan,
                client,
                model=selected_model,
            )
    except (OSError, OllamaError, PlanError, PropertyCheckError) as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "schema": "autofix-property-candidate-error",
                        "version": 1,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            console.print(f"[red]Property generation stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if json_output:
        _json(result.to_dict())
        return
    console.print(f"Rationale: {result.rationale}")
    console.print(f"Property: {result.property}")
    console.print(f"Candidate: [cyan]{result.candidate.path}[/cyan]")
    console.print(f"Metadata: [cyan]{result.metadata_path}[/cyan]")
    console.print(Syntax(result.candidate.source, "python", word_wrap=False))
    console.print(
        "[yellow]Candidate saved for review only; it was not executed and was not "
        "added to the project test tree.[/yellow]"
    )
    console.print("After review, run:")
    console.print(
        f'autofix property-check "{result.candidate.path}" '
        f'--project "{result.root}" --plan "{result.plan_path}" --run'
    )


@app.command("property-promote")
def property_promote_command(
    report: Path = typer.Argument(
        ..., exists=True, dir_okay=False, resolve_path=True
    ),
    path: Path = typer.Option(
        Path("."),
        "--project",
        exists=True,
        file_okay=False,
        resolve_path=True,
    ),
    target: str = typer.Option(
        "",
        "--target",
        help="New relative test path; defaults to tests/test_autofix_property_<sha>.py.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print machine-readable JSON."
    ),
) -> None:
    """Turn confirmed property evidence into an unapplied new-test patch."""

    try:
        result = promote_property_candidate(
            path,
            report,
            target=target,
        )
    except (OSError, PlanError, PropertyCheckError) as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "schema": "autofix-property-promotion-error",
                        "version": 1,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            console.print(f"[red]Property promotion stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if json_output:
        _json(result.to_dict())
        return
    console.print(f"Confirmed report: [cyan]{result.report_path}[/cyan]")
    console.print(f"New test target: [cyan]{result.target_path}[/cyan]")
    console.print(f"Review patch: [cyan]{result.patch_path}[/cyan]")
    console.print(f"Patch SHA-256: {result.patch_sha256}")
    console.print(f"Metadata: [cyan]{result.metadata_path}[/cyan]")
    console.print(
        Syntax(result.patch_path.read_text(encoding="utf-8"), "diff", word_wrap=False)
    )
    console.print(
        "[yellow]Patch created for review only. No test file was written and no patch "
        "was applied.[/yellow]"
    )
    console.print(f'Check with: git apply --check "{result.patch_path}"')


@app.command("property-apply")
def property_apply_command(
    promotion: Path = typer.Argument(
        ..., exists=True, dir_okay=False, resolve_path=True
    ),
    path: Path = typer.Option(
        Path("."),
        "--project",
        exists=True,
        file_okay=False,
        resolve_path=True,
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Create and verify the promoted test; preview is the default.",
    ),
    timeout: float = typer.Option(
        300, min=0.1, help="Timeout for each verification step."
    ),
    no_save: bool = typer.Option(
        False, "--no-save", help="Do not save an apply receipt under .autofix."
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        dir_okay=False,
        resolve_path=True,
        help="Publish the apply receipt at an exact, non-existing CI artifact path.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print machine-readable JSON."
    ),
) -> None:
    """Explicitly add a promoted test after the reviewed repair is committed."""

    if output is not None and (not apply or no_save):
        message = "--output requires --apply and cannot be combined with --no-save"
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "schema": "autofix-property-apply-error",
                        "version": 1,
                        "error": message,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            console.print(f"[red]Property apply stopped: {message}[/red]")
        raise typer.Exit(code=2)

    try:
        result = apply_property_promotion(
            path,
            promotion,
            apply=apply,
            timeout_seconds=timeout,
            save_receipt=apply and not no_save,
            receipt_output=output,
        )
    except (OSError, PropertyCheckError) as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "schema": "autofix-property-apply-error",
                        "version": 1,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            console.print(f"[red]Property apply stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if json_output:
        _json(result.to_dict())
    else:
        console.print(f"Promotion: [cyan]{result.promotion_path}[/cyan]")
        console.print(f"Target: [cyan]{result.target_path}[/cyan]")
        console.print(f"Committed repair baseline: {result.baseline_commit}")
        console.print(
            "Verification workspace: detached Git worktree (removed before write-back)"
        )
        console.print("Verification matrix:")
        for step in result.verification_steps:
            console.print(f"  - {' '.join(step.argv)}")
        if not apply:
            console.print(
                "[yellow]Preview only. Re-run with --apply to create and verify the new "
                "test file.[/yellow]"
            )
        elif result.applied:
            console.print(
                "[green]Promoted regression test created and fully verified.[/green]"
            )
            if result.receipt_path:
                console.print(f"Receipt: {result.receipt_path}")
            console.print("[cyan]No commit or push was performed.[/cyan]")
        else:
            console.print(
                "[red]Verification failed; the newly created test was removed.[/red]"
            )
            if result.receipt_path:
                console.print(f"Receipt: {result.receipt_path}")
    if apply and not result.applied:
        exit_code = result.verification.exit_code if result.verification else 1
        raise typer.Exit(code=exit_code or 1)


@app.command("git-status")
def git_status_command(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    require_clean: bool = typer.Option(
        False,
        "--require-clean",
        help="Exit 1 unless PATH belongs to a clean Git worktree.",
    ),
) -> None:
    """Inspect Git identity and changes without modifying the repository."""

    try:
        status = inspect_git_workspace(path)
    except GitInspectionError as exc:
        console.print(f"[red]Git inspection failed: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if json_output:
        _json(status.to_dict())
    else:
        table = Table(title=f"Git workspace: {status.requested_root}")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("state", status.state)
        table.add_row("repository root", str(status.repository_root or "not detected"))
        table.add_row("branch", status.branch or ("detached HEAD" if status.detached else "none"))
        table.add_row("HEAD", status.head_commit or "unborn/unavailable")
        table.add_row("direct apply policy", status.direct_apply_policy)
        table.add_row("changes", str(len(status.changes)))
        console.print(table)
        if status.changes:
            changes = Table(title="Tracked and untracked paths")
            changes.add_column("Status")
            changes.add_column("Path")
            changes.add_column("Original path")
            for change in status.changes:
                changes.add_row(change.status, change.path, change.original_path or "")
            console.print(changes)

    if require_clean and not status.clean:
        raise typer.Exit(code=1)


@app.command("init")
def init_project(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    command: str | None = typer.Option(None, help="Override the detected build command."),
    step: list[str] | None = typer.Option(
        None, "--step", help="Build command in an ordered matrix; repeat for multiple steps."
    ),
    build_system: str | None = typer.Option(
        None, help="Select one of the detected build systems."
    ),
    docker_image: str | None = typer.Option(None, help="Docker image containing build dependencies."),
    model: str | None = typer.Option(None, help="Installed Ollama model; auto-detected when omitted."),
    ollama_url: str = typer.Option("http://127.0.0.1:11434", help="Ollama API base URL."),
    offline: bool = typer.Option(True, "--offline/--online", help="Disable build-container networking."),
    dependencies_bundled: bool = typer.Option(
        False, help="Declare that the selected image contains all build dependencies."
    ),
) -> None:
    """Detect a project and create a safe autofix.yml starter configuration."""

    inspection = _select_inspection_or_exit(inspect_project(path), build_system)
    selected = inspection.selected
    selected_steps = tuple(step or ())
    if command and selected_steps:
        console.print("[red]--command and --step cannot be used together.[/red]")
        raise typer.Exit(code=2)
    if command:
        selected_command = command
        selected_commands: tuple[str, ...] = ()
    elif selected_steps:
        selected_command = None
        selected_commands = selected_steps
    elif selected:
        detected_commands = tuple(shlex.join(item.argv) for item in selected.steps)
        selected_command = detected_commands[0] if len(detected_commands) == 1 else None
        selected_commands = detected_commands if len(detected_commands) > 1 else ()
    else:
        selected_command = None
        selected_commands = ()
    if not selected_command and not selected_commands:
        console.print("[red]No build command was detected; pass --command or one or more --step options.[/red]")
        raise typer.Exit(code=2)
    try:
        for item in selected_commands:
            parse_command(item)
        if selected_command:
            parse_command(selected_command)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    selected_image = docker_image
    if selected_image is None and selected:
        selected_image = (
            "autofix-python:3.11"
            if selected.build_system == "python"
            else default_image(selected)
        )
    if not selected_image:
        console.print("[red]No Docker image is known; pass --docker-image.[/red]")
        raise typer.Exit(code=2)

    selected_model = model
    if selected_model is None:
        try:
            models = OllamaClient(ollama_url).list_models()
        except OllamaError as exc:
            console.print(f"[red]Unable to select an Ollama model: {exc}; pass --model.[/red]")
            raise typer.Exit(code=2) from exc
        if not models:
            console.print("[red]Ollama has no installed models; pass --model after installing one.[/red]")
            raise typer.Exit(code=2)
        selected_model = models[0]

    bundled = dependencies_bundled or selected_image == "autofix-python:3.11"
    try:
        config_path = create_config(
            inspection.root,
            command=selected_command,
            commands=selected_commands,
            build_system=selected.build_system if selected else build_system,
            docker_image=selected_image,
            ollama_model=selected_model,
            offline=offline,
            dependencies_bundled=bundled,
        )
    except ConfigError as exc:
        console.print(f"[red]Initialization stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    console.print(f"[green]Created AutoFix configuration: {config_path}[/green]")
    if selected_commands:
        console.print("Build matrix:")
        for index, item in enumerate(selected_commands, start=1):
            console.print(f"  {index}. {item}")
    else:
        console.print(f"Build command: {selected_command}")
    console.print(f"Docker image: {selected_image}")
    console.print(f"Ollama model: {selected_model}")
    if not bundled:
        console.print(
            "[yellow]Dependencies are not declared bundled; build a project image and update runtime.dependencies_bundled before offline use.[/yellow]"
        )


@app.command()
def history(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    limit: int = typer.Option(10, min=1, max=100, help="Maximum repair reports to show."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List recent repair attempts and their audit reports."""

    report_dir = path / ".autofix" / "repairs"
    undone_reports = rolled_back_reports(path)
    reports: list[dict[str, object]] = []
    if report_dir.is_dir():
        for report_path in sorted(report_dir.glob("*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                data = {"error": "unreadable report"}
            reports.append(
                {
                    "created_at": data.get("created_at"),
                    "succeeded": data.get("succeeded", False),
                    "applied": data.get("applied", False),
                    "rolled_back": data.get("rolled_back", False),
                    "undone": report_path.resolve() in undone_reports,
                    "proposals": len(data.get("proposals", []))
                    if isinstance(data.get("proposals"), list)
                    else 0,
                    "rejections": len(data.get("rejections", []))
                    if isinstance(data.get("rejections"), list)
                    else 0,
                    "error": data.get("error"),
                    "report": str(report_path),
                }
            )
    if json_output:
        _json({"root": str(path), "reports": reports})
        return
    if not reports:
        console.print(f"[yellow]No repair reports found under {report_dir}[/yellow]")
        return
    table = Table(title=f"AutoFix repair history: {path}")
    for column in ("Created", "Status", "Applied", "Proposals", "Rejected", "Report"):
        table.add_column(column)
    for item in reports:
        status = "passed" if item["succeeded"] else "failed"
        if item["error"]:
            status = "stopped"
        if item["undone"]:
            status = "undone"
        table.add_row(
            str(item["created_at"] or "unknown"),
            status,
            "yes" if item["applied"] else "no",
            str(item["proposals"]),
            str(item["rejections"]),
            str(item["report"]),
        )
    console.print(table)


@app.command()
def evaluate(
    path: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, resolve_path=True
    ),
    repair_mode: bool = typer.Option(
        False,
        "--repair",
        help="Ask Ollama for an isolated repair and score its changed-file scope.",
    ),
    model: str | None = typer.Option(
        None, help="Installed Ollama model; defaults to autofix.yml."
    ),
    ollama_url: str = typer.Option(
        "http://127.0.0.1:11434", help="Ollama API base URL."
    ),
    max_attempts: int = typer.Option(3, min=1, max=20),
    timeout: float = typer.Option(300, min=0.1, help="Timeout per verification step."),
    no_save: bool = typer.Option(False, help="Do not save evaluation or repair reports."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    docker: bool = typer.Option(False, "--docker", help="Run verification in Docker."),
    docker_image: str | None = typer.Option(None, help="Override the configured image."),
    docker_cpus: float = typer.Option(2.0, min=0.1, max=64),
    docker_memory: str = typer.Option("2g"),
    docker_pids: int = typer.Option(256, min=16, max=4096),
) -> None:
    """Validate an intentionally failing case and optionally score a repair."""

    try:
        manifest = load_evaluation_manifest(path)
        config = _load_config_or_exit(path)
        inspection = inspect_project(path)
        build_runner = run_build
        if docker:
            docker_config = _docker_config(
                inspection,
                docker_image or (config.docker_image if config else None),
                docker_cpus,
                docker_memory,
                docker_pids,
                False,
            )
            _require_docker()

            def build_runner(  # type: ignore[no-redef]
                root: Path,
                steps: tuple[BuildStep, ...],
                *,
                timeout_seconds: float,
                save_report: bool = True,
            ):
                """Bind the selected Docker limits to the evaluation runner."""

                return run_build_docker(
                    root,
                    steps,
                    config=docker_config,
                    timeout_seconds=timeout_seconds,
                    save_report=save_report,
                )

        selected_model = model or (config.ollama_model if config else None)
        client = OllamaClient(ollama_url) if repair_mode else None
        result = evaluate_project(
            path,
            manifest,
            client=client,
            model=selected_model,
            max_attempts=max_attempts,
            timeout_seconds=timeout,
            build_runner=build_runner,
            runner_name="docker" if docker else "local",
            save_report=not no_save,
        )
    except (
        DockerConfigurationError,
        EvaluationError,
        OllamaError,
        RepairEnvironmentError,
        UnsafePatchError,
    ) as exc:
        console.print(f"[red]Evaluation stopped: {exc}[/red]")
        report = getattr(exc, "report_path", None)
        if report:
            console.print(f"Repair report: {report}")
        raise typer.Exit(code=2) from exc

    if json_output:
        _json(result.to_dict())
    else:
        table = Table(title=f"AutoFix evaluation: {manifest.name}")
        table.add_column("Metric")
        table.add_column("Result")
        table.add_row(
            "initial failure",
            "confirmed" if result.initial_failure_confirmed else "not reproduced",
        )
        table.add_row(
            "failure contract",
            "matched" if result.failure_contract_matched else "mismatched",
        )
        if result.failure_contract_mismatches:
            table.add_row(
                "failure mismatch",
                "\n".join(result.failure_contract_mismatches),
            )
        if result.repair:
            table.add_row(
                "repair build", "passed" if result.repair.succeeded else "failed"
            )
            table.add_row("proposals", str(len(result.repair.proposals)))
            table.add_row(
                "verification failures",
                str(len(result.repair.verification_failures)),
            )
            table.add_row("changed paths", ", ".join(result.changed_paths) or "none")
            table.add_row(
                "unexpected paths",
                ", ".join(result.unexpected_change_paths) or "none",
            )
            table.add_row(
                "missing required paths",
                ", ".join(result.missing_required_paths) or "none",
            )
        elif repair_mode:
            table.add_row("repair build", "failed")
            table.add_row("proposals", str(result.proposal_count))
            table.add_row("rejections", str(result.rejection_count))
            table.add_row(
                "verification failures",
                str(result.verification_failure_count),
            )
            table.add_row("repair error", result.repair_error or "unknown failure")
            table.add_row(
                "missing required paths",
                ", ".join(result.missing_required_paths) or "none",
            )
        table.add_row("contract", "passed" if result.passed else "failed")
        console.print(table)
        if result.report_path:
            console.print(f"Evaluation report: {result.report_path}")
        if not repair_mode and result.passed:
            console.print(
                "[green]Baseline is valid. Re-run with --repair to score an isolated model repair.[/green]"
            )
    if not result.passed:
        raise typer.Exit(code=1)


@app.command("evaluate-suite")
def evaluate_suite_command(
    path: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, resolve_path=True
    ),
    repair_mode: bool = typer.Option(
        False,
        "--repair",
        help="Run isolated model repair for every valid baseline contract.",
    ),
    model: str | None = typer.Option(
        None, help="Installed Ollama model; defaults to user/project configuration."
    ),
    ollama_url: str = typer.Option(
        "http://127.0.0.1:11434", help="Ollama API base URL."
    ),
    max_attempts: int = typer.Option(3, min=1, max=20),
    timeout: float = typer.Option(300, min=0.1, help="Timeout per case step."),
    no_save: bool = typer.Option(
        False, "--no-save", help="Do not save aggregate or per-case reports."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print aggregate JSON."),
    progress: bool = typer.Option(
        True,
        "--progress/--no-progress",
        help="Show live case, model, proposal, and verification milestones.",
    ),
    docker: bool = typer.Option(False, "--docker", help="Run verification in Docker."),
    docker_image: str | None = typer.Option(
        None, help="Image shared by every suite case; required if not configured."
    ),
    docker_cpus: float = typer.Option(2.0, min=0.1, max=64),
    docker_memory: str = typer.Option("2g"),
    docker_pids: int = typer.Option(256, min=16, max=4096),
) -> None:
    """Aggregate baseline contracts or isolated model repairs below a root."""

    try:
        config = _load_config_or_exit(path) if repair_mode or docker else None
        build_runner = run_build
        if docker:
            docker_config = _docker_config(
                None,
                docker_image or (config.docker_image if config else None),
                docker_cpus,
                docker_memory,
                docker_pids,
                False,
            )
            _require_docker()

            def build_runner(  # type: ignore[no-redef]
                root: Path,
                steps: tuple[BuildStep, ...],
                *,
                timeout_seconds: float,
                save_report: bool = True,
            ):
                """Bind one explicit Docker policy to every suite case."""

                return run_build_docker(
                    root,
                    steps,
                    config=docker_config,
                    timeout_seconds=timeout_seconds,
                    save_report=save_report,
                )

        selected_model = model or (config.ollama_model if config else None)
        progress_renderer = (
            _SuiteProgressRenderer() if progress and not json_output else None
        )
        if progress_renderer is not None:
            progress_renderer.start_wait("Discovering evaluation cases...")
        try:
            result = evaluate_suite(
                path,
                timeout_seconds=timeout,
                save_report=not no_save,
                client=OllamaClient(ollama_url) if repair_mode else None,
                model=selected_model,
                max_attempts=max_attempts,
                build_runner=build_runner,
                runner_name="docker" if docker else "local",
                progress_callback=progress_renderer,
            )
        finally:
            if progress_renderer is not None:
                progress_renderer.close()
    except (DockerConfigurationError, EvaluationSuiteError) as exc:
        console.print(f"[red]Evaluation suite stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    if json_output:
        _json(result.to_dict())
    else:
        table = Table(title=f"AutoFix evaluation suite: {path}")
        table.add_column("Case")
        table.add_column("Language")
        table.add_column("Difficulty")
        table.add_column("Status")
        table.add_column("Attempts")
        table.add_column("Duration")
        table.add_column("Error")
        for case in result.cases:
            manifest = case.result.manifest if case.result else None
            attempts = (
                case.result.to_dict()["metrics"]["proposal_count"]
                if case.result and result.mode == "repair"
                else "-"
            )
            error = case.failure_summary
            table.add_row(
                case.relative_root,
                manifest.language if manifest else "unknown",
                manifest.difficulty if manifest else "unknown",
                "[green]passed[/green]" if case.passed else f"[red]{case.status}[/red]",
                str(attempts),
                f"{case.duration_seconds:.3f}s",
                error or "",
            )
        console.print(table)
        failed_cases = [case for case in result.cases if not case.passed]
        if failed_cases:
            console.print("[red]Failed case details:[/red]")
            for case in failed_cases:
                console.print(
                    f"- [bold]{case.relative_root}[/bold]: "
                    f"{case.failure_summary or 'unknown evaluation failure'}"
                )
        metrics = result.metrics
        console.print(
            f"Suite result: {metrics['passed']}/{metrics['total']} passed "
            f"({metrics['pass_rate']:.2%})"
        )
        if result.mode == "repair":
            console.print(
                f"Repair metrics: first-attempt pass "
                f"{metrics['first_attempt_pass_rate']:.2%}, "
                f"average attempts {metrics['average_attempts']:.3f}, "
                f"static rejection {metrics['static_rejection_rate']:.2%}, "
                f"verification failure {metrics['verification_failure_rate']:.2%}"
            )
            console.print(
                "Context metrics: required-path recall "
                f"{metrics['required_context_recall']:.2%}, "
                f"top-5 {metrics['required_context_top5_recall']:.2%}, "
                f"average rank {metrics['average_required_context_rank']:.3f}, "
                f"sent {metrics['context_files_sent']} files / "
                f"{metrics['context_chars_sent']} characters, "
                f"fallback {metrics['context_fallback_rate']:.2%}"
            )
            _print_verified_suite_changes(result)
        if result.json_report_path:
            console.print(f"Suite JSON: {result.json_report_path}")
        if result.markdown_report_path:
            console.print(f"Suite Markdown: {result.markdown_report_path}")
    if not result.passed:
        raise typer.Exit(code=1)


@app.command("ci-check")
def ci_check(
    path: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, resolve_path=True
    ),
    command: str | None = typer.Option(
        None, "--command", "-c", help="Override the detected build command."
    ),
    step: list[str] | None = typer.Option(
        None, "--step", help="Verification command; repeat for an ordered matrix."
    ),
    build_system: str | None = typer.Option(
        None, help="Select one of the detected build systems."
    ),
    timeout: float = typer.Option(300, min=0.1, help="Timeout for each step."),
    require_git: bool = typer.Option(
        True,
        "--require-git/--allow-unversioned",
        help="Require a repository with a committed HEAD baseline.",
    ),
    require_clean: bool = typer.Option(
        True,
        "--require-clean/--allow-dirty",
        help="Reject tracked or untracked Git changes before execution.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        dir_okay=False,
        help="Atomically write the same JSON result to this file.",
    ),
    save_run_report: bool = typer.Option(
        False,
        "--save-run-report/--no-save-run-report",
        help="Also save the ordinary .autofix/runs report.",
    ),
    docker: bool = typer.Option(False, "--docker", help="Run in constrained Docker."),
    docker_image: str | None = typer.Option(None, help="Override the configured image."),
    docker_cpus: float = typer.Option(2.0, min=0.1, max=64),
    docker_memory: str = typer.Option("2g"),
    docker_pids: int = typer.Option(256, min=16, max=4096),
    allow_network: bool = typer.Option(False),
) -> None:
    """Run a non-interactive verification with stable JSON and exit codes."""

    root = path.expanduser().resolve()
    repository = None
    runner_name: str | None = "docker" if docker else "local"
    result: CIResult
    try:
        repository = inspect_git_workspace(root)
        if require_git and not repository.is_repository:
            result = CIResult(
                "policy_rejected",
                CIExitCode.POLICY_REJECTED,
                root,
                runner_name,
                repository,
                error="CI policy requires a Git repository.",
            )
        elif require_git and repository.head_commit is None:
            result = CIResult(
                "policy_rejected",
                CIExitCode.POLICY_REJECTED,
                root,
                runner_name,
                repository,
                error="CI policy requires a committed Git HEAD baseline.",
            )
        elif require_clean and repository.is_repository and not repository.clean:
            result = CIResult(
                "policy_rejected",
                CIExitCode.POLICY_REJECTED,
                root,
                runner_name,
                repository,
                error="CI policy requires a clean Git worktree.",
            )
        else:
            config = load_config(root)
            inspection = inspect_project(root)
            inspection = inspection.select(
                build_system or (config.build_system if config else None)
            )
            steps = _selected_build_steps(command, step, config, inspection)
            if not steps:
                raise ConfigError(
                    "No supported build system detected; pass --command explicitly"
                )
            docker_config: DockerConfig | None = None
            if docker:
                if config and config.offline and allow_network:
                    raise ConfigError(
                        "--allow-network conflicts with runtime.offline: true in autofix.yml"
                    )
                docker_config = _docker_config(
                    inspection,
                    docker_image or (config.docker_image if config else None),
                    docker_cpus,
                    docker_memory,
                    docker_pids,
                    allow_network,
                )
            try:
                if docker_config:
                    _require_docker()
                    build = run_build_docker(
                        root,
                        steps,
                        config=docker_config,
                        timeout_seconds=timeout,
                        save_report=save_run_report,
                    )
                else:
                    build = run_build(
                        root,
                        steps,
                        timeout_seconds=timeout,
                        save_report=save_run_report,
                    )
            except DockerConfigurationError as exc:
                result = CIResult(
                    "environment_error",
                    CIExitCode.ENVIRONMENT_ERROR,
                    root,
                    runner_name,
                    repository,
                    error=str(exc),
                )
            else:
                result = result_from_build(
                    root, repository, build, runner=runner_name or "local"
                )
    except GitInspectionError as exc:
        result = CIResult(
            "policy_rejected",
            CIExitCode.POLICY_REJECTED,
            root,
            runner_name,
            repository,
            error=str(exc),
        )
    except (ConfigError, DockerConfigurationError, ValueError) as exc:
        result = CIResult(
            "configuration_error",
            CIExitCode.CONFIGURATION_ERROR,
            root,
            runner_name,
            repository,
            error=str(exc),
        )
    except Exception as exc:
        result = CIResult(
            "internal_error",
            CIExitCode.INTERNAL_ERROR,
            root,
            runner_name,
            repository,
            error=f"{type(exc).__name__}: {exc}",
        )

    if output is not None:
        try:
            save_ci_result(output, result)
        except OSError as exc:
            result = CIResult(
                "internal_error",
                CIExitCode.INTERNAL_ERROR,
                root,
                runner_name,
                repository,
                result.build,
                f"Unable to write CI result: {exc}",
            )
    typer.echo(json.dumps(result.to_dict(), ensure_ascii=False))
    if result.exit_code != CIExitCode.PASSED:
        raise typer.Exit(code=int(result.exit_code))


@app.command()
def run(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    command: str | None = typer.Option(None, "--command", "-c", help="Override the detected build command."),
    step: list[str] | None = typer.Option(
        None, "--step", help="Build command in an ordered matrix; repeat for multiple steps."
    ),
    build_system: str | None = typer.Option(
        None, help="Select one of the detected build systems."
    ),
    timeout: float = typer.Option(300, min=0.1, help="Timeout in seconds for each build step."),
    dry_run: bool = typer.Option(False, help="Show the plan without executing it."),
    no_save: bool = typer.Option(False, help="Do not save a JSON run report."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    with_static_analysis: bool = typer.Option(
        False,
        "--with-static-analysis",
        help="Append every project-declared, locally available static analyzer.",
    ),
    docker: bool = typer.Option(False, "--docker", help="Run the build in a constrained Docker container."),
    docker_image: str | None = typer.Option(None, help="Container image; defaults by detected build system."),
    docker_cpus: float = typer.Option(2.0, min=0.1, max=64, help="Container CPU limit."),
    docker_memory: str = typer.Option("2g", help="Container memory limit, such as 512m or 2g."),
    docker_pids: int = typer.Option(256, min=16, max=4096, help="Container process limit."),
    allow_network: bool = typer.Option(False, help="Allow container networking; disabled by default."),
) -> None:
    """Run the detected build or test command and capture its output."""

    inspection = inspect_project(path)
    config = _load_config_or_exit(inspection.root)
    inspection = _select_inspection_or_exit(
        inspection, build_system or (config.build_system if config else None)
    )
    selected_command = _selected_build_command(command, config, inspection) if not step else None
    _announce_command_upgrade(config, selected_command)
    try:
        steps = _selected_build_steps(command, step, config, inspection)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    if not steps:
        console.print("[red]No supported build system detected; pass --command explicitly.[/red]")
        raise typer.Exit(code=2)
    try:
        steps = _append_static_analysis_steps(
            inspection.root,
            steps,
            enabled=with_static_analysis,
            docker=docker,
        )
    except ValueError as exc:
        console.print(f"[red]Static analysis configuration error: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    docker_config: DockerConfig | None = None
    try:
        if docker:
            if config and config.offline and allow_network:
                raise DockerConfigurationError(
                    "--allow-network conflicts with runtime.offline: true in autofix.yml"
                )
            docker_config = _docker_config(
                inspection,
                docker_image or (config.docker_image if config else None),
                docker_cpus,
                docker_memory,
                docker_pids,
                allow_network,
            )
    except DockerConfigurationError as exc:
        console.print(f"[red]Docker configuration error: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if dry_run:
        planned_steps = (
            tuple(build_docker_step(inspection.root, step, docker_config) for step in steps)
            if docker_config
            else steps
        )
        plan = {
            "root": str(inspection.root),
            "docker_disposable_copy": bool(docker_config),
            "steps": [step.to_dict() for step in planned_steps],
        }
        _json(plan) if json_output else console.print(
            "\n".join(" ".join(step.argv) for step in planned_steps)
        )
        return

    try:
        if docker_config:
            _require_docker()
            result = run_build_docker(
                inspection.root,
                steps,
                config=docker_config,
                timeout_seconds=timeout,
                save_report=not no_save,
            )
        else:
            result = run_build(inspection.root, steps, timeout_seconds=timeout, save_report=not no_save)
    except DockerConfigurationError as exc:
        console.print(f"[red]Docker configuration error: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    if json_output:
        _json(result.to_dict())
    else:
        for step in result.steps:
            state = "[green]passed[/green]" if step.succeeded else "[red]failed[/red]"
            console.print(f"[{step.label}] {state} ({step.duration_seconds:.3f}s)")
            if step.stdout:
                console.print(step.stdout.rstrip())
            if step.stderr:
                console.print(step.stderr.rstrip(), style="yellow")
        if result.report_path:
            console.print(f"Report: {result.report_path}")
    if not result.succeeded:
        raise typer.Exit(code=result.exit_code or 1)


@app.command()
def rollback(
    report_path: Path = typer.Argument(..., exists=True, dir_okay=False, resolve_path=True),
    path: Path = typer.Option(
        Path("."), "--project", exists=True, file_okay=False, resolve_path=True
    ),
) -> None:
    """Restore project files by reversing the changes in a repair report."""

    try:
        result = rollback_report(report_path, path)
    except RollbackError as exc:
        console.print(f"[red]Rollback stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    for restored in result.restored_paths:
        console.print(f"Restored: {restored}")
    console.print(f"Backup of replaced files: {result.backup_dir}")
    console.print(f"Rollback receipt: {result.receipt_path}")
    console.print("[green]Rollback completed.[/green]")


@app.command()
def undo(
    path: Path = typer.Argument(
        Path("."), exists=True, file_okay=False, resolve_path=True
    ),
) -> None:
    """Undo the latest successful applied repair that has not already been undone."""

    try:
        report = latest_rollback_candidate(path)
        result = rollback_report(report, path)
    except RollbackError as exc:
        console.print(f"[red]Undo stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    console.print(f"Undid repair: {result.report_path}")
    for restored in result.restored_paths:
        console.print(f"Restored: {restored}")
    console.print(f"Backup of replaced files: {result.backup_dir}")
    console.print(f"Rollback receipt: {result.receipt_path}")
    console.print("[green]Undo completed.[/green]")


@app.command("apply-plan")
def apply_plan(
    plan_path: Path = typer.Argument(..., exists=True, dir_okay=False, resolve_path=True),
    path: Path = typer.Option(Path("."), "--project", exists=True, file_okay=False, resolve_path=True),
    timeout: float | None = typer.Option(
        None,
        min=0.1,
        help="Confirm or select timeout; plan v3 uses its bound timeout by default.",
    ),
    step: list[str] | None = typer.Option(
        None, "--step", help="Verification command; repeat for an ordered matrix."
    ),
    build_system: str | None = typer.Option(
        None, help="Select one of the detected build systems."
    ),
    isolate: bool = typer.Option(True, "--isolate/--no-isolate", help="Verify in a temporary copy."),
    allow_test_changes: bool = typer.Option(False, help="Allow the plan to modify test files."),
    allow_destructive_changes: bool = typer.Option(
        False, help="Allow a reviewed plan to remove most code or top-level definitions."
    ),
    docker: bool | None = typer.Option(
        None,
        "--docker/--local",
        help="Confirm or select the verification runner; plan v3 uses its bound runner by default.",
    ),
    docker_image: str | None = typer.Option(None, help="Override the image from autofix.yml."),
    docker_cpus: float | None = typer.Option(None, min=0.1, max=64),
    docker_memory: str | None = typer.Option(None),
    docker_pids: int | None = typer.Option(None, min=16, max=4096),
    patch_output: Path | None = typer.Option(
        None,
        "--patch-output",
        dir_okay=False,
        help="Write the verified standalone patch to this new file.",
    ),
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Create a local Git commit after verified application; never push or merge.",
    ),
    commit_message: str = typer.Option(
        "autofix: apply verified repair",
        help="Message for the optional local commit.",
    ),
) -> None:
    """Apply the exact reviewed repair plan without asking the model again."""

    inspection = inspect_project(path)
    config = _load_config_or_exit(inspection.root)
    inspection = _select_inspection_or_exit(
        inspection, build_system or (config.build_system if config else None)
    )
    try:
        if not commit and commit_message != "autofix: apply verified repair":
            raise PlanError("--commit-message requires --commit")
        plan, _ = load_repair_plan(
            plan_path,
            inspection.root,
            allow_test_changes=allow_test_changes,
            allow_destructive_changes=allow_destructive_changes,
        )
        if plan.verification_steps is not None:
            if step:
                requested_steps = _selected_build_steps(None, step, config, inspection)
                if not _same_step_commands(requested_steps, plan.verification_steps):
                    raise PlanError(
                        "Verification commands differ from the reviewed repair plan"
                    )
            steps = plan.verification_steps
            console.print(
                f"[cyan]Using {len(steps)} verification step(s) bound to repair plan v{plan.version}.[/cyan]"
            )
        else:
            selected_command = _selected_build_command(None, config, inspection) if not step else None
            _announce_command_upgrade(config, selected_command)
            steps = _selected_build_steps(None, step, config, inspection)
            console.print(
                "[yellow]Legacy plan v1 has no bound verification commands; using current project configuration.[/yellow]"
            )
        if not steps:
            raise PlanError(
                "No supported build system detected and no command is configured"
            )
        build_runner = run_build
        docker_runtime_config: DockerConfig | None = None
        if plan.version >= 3:
            use_docker = plan.verification_runner == "docker"
            if plan.verification_timeout_seconds is None:
                raise PlanError("Repair plan has no bound verification timeout")
            if timeout is not None and timeout != plan.verification_timeout_seconds:
                raise PlanError(
                    "Verification timeout differs from the reviewed repair plan"
                )
            effective_timeout = plan.verification_timeout_seconds
            if docker is not None and docker != use_docker:
                raise PlanError(
                    "Verification runner differs from the reviewed repair plan"
                )
            if use_docker:
                if plan.docker_config is None:
                    raise PlanError("Docker repair plan has no bound Docker settings")
                expected = plan.docker_config
                overrides = (
                    ("image", docker_image, expected.image),
                    ("CPU limit", docker_cpus, expected.cpus),
                    ("memory limit", docker_memory, expected.memory),
                    ("PID limit", docker_pids, expected.pids_limit),
                )
                for label, requested, bound in overrides:
                    if requested is not None and requested != bound:
                        raise PlanError(
                            f"Docker {label} differs from the reviewed repair plan"
                        )
                docker_runtime_config = expected
            elif any(
                value is not None
                for value in (docker_image, docker_cpus, docker_memory, docker_pids)
            ):
                raise PlanError("Local repair plan cannot accept Docker settings")
            console.print(
                f"[cyan]Using {plan.verification_runner} runner bound to repair plan v{plan.version}.[/cyan]"
            )
        else:
            use_docker = bool(docker)
            effective_timeout = timeout if timeout is not None else 300
            if plan.version == 2:
                console.print(
                    "[yellow]Legacy plan v2 has bound commands but no bound runner; using CLI runtime options.[/yellow]"
                )
            if use_docker:
                docker_runtime_config = _docker_config(
                    inspection,
                    docker_image or (config.docker_image if config else None),
                    docker_cpus if docker_cpus is not None else 2.0,
                    docker_memory or "2g",
                    docker_pids if docker_pids is not None else 256,
                    False,
                )
        if use_docker:
            _require_docker()
            assert docker_runtime_config is not None

            def build_runner(root: Path, build_steps: tuple, **kwargs: object):  # type: ignore[no-redef]
                """Execute plan-bound steps with the plan-bound Docker policy."""

                return run_build_docker(root, build_steps, config=docker_runtime_config, **kwargs)

        class PlanClient:
            """Replay reviewed plan content without a second model request."""

            def propose(self, **_: object):
                """Return the immutable proposal embedded in the plan."""

                return plan.to_proposal()

        result = repair_project(
            inspection.root,
            steps,
            PlanClient(),
            model=plan.model,
            max_attempts=1,
            timeout_seconds=effective_timeout,
            apply=True,
            allow_test_changes=allow_test_changes,
            allow_destructive_changes=allow_destructive_changes,
            isolate=isolate,
            build_runner=build_runner,
            patch_output=patch_output,
        )
    except (
        DockerConfigurationError,
        PlanError,
        RepairArtifactError,
        UnsafePatchError,
        ValueError,
    ) as exc:
        console.print(f"[red]Plan application stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    if result.report_path:
        console.print(f"Repair report: {result.report_path}")
    if result.patch_path:
        console.print(f"Standalone patch: {result.patch_path}")
        console.print(f"Patch SHA-256: {result.patch_sha256}")
    if result.initial_build.succeeded:
        console.print("[yellow]The project already passes; the plan was not applied.[/yellow]")
        return
    if result.succeeded:
        if commit:
            try:
                commit_id = _commit_verified_result(result, commit_message)
            except GitCommitError as exc:
                console.print(f"[red]Local commit stopped: {exc}[/red]")
                raise typer.Exit(code=2) from exc
            console.print(f"[green]Created local commit: {commit_id}[/green]")
            console.print("[cyan]No push or merge was performed.[/cyan]")
        console.print("[green]Reviewed plan applied and verified successfully.[/green]")
        return
    console.print("[red]Reviewed plan failed verification; the original project was unchanged.[/red]")
    raise typer.Exit(code=result.final_build.exit_code or 1)


@app.command()
def repair(
    path: Path = typer.Argument(Path("."), exists=True, file_okay=False, resolve_path=True),
    command: str | None = typer.Option(None, "--command", "-c", help="Override the detected build command."),
    step: list[str] | None = typer.Option(
        None, "--step", help="Build command in an ordered matrix; repeat for multiple steps."
    ),
    build_system: str | None = typer.Option(
        None, help="Select one of the detected build systems."
    ),
    model: str | None = typer.Option(None, help="Installed Ollama model; defaults to the first local model."),
    ollama_url: str = typer.Option("http://127.0.0.1:11434", help="Ollama API base URL."),
    max_attempts: int = typer.Option(1, min=1, max=5, help="Maximum model repair attempts."),
    timeout: float = typer.Option(300, min=0.1, help="Timeout for each build step."),
    apply: bool = typer.Option(False, "--apply", help="Apply validated changes and rebuild."),
    keep_failed: bool = typer.Option(False, help="Keep changes when all rebuild attempts fail."),
    isolate: bool = typer.Option(
        True,
        "--isolate/--no-isolate",
        help="Verify repairs in a temporary project copy before writing them back.",
    ),
    allow_test_changes: bool = typer.Option(
        False, help="Allow model-generated changes to test files; protected by default."
    ),
    allow_destructive_changes: bool = typer.Option(
        False,
        help="Allow proposals that remove most code or top-level definitions; unsafe by default.",
    ),
    with_static_analysis: bool = typer.Option(
        False,
        "--with-static-analysis",
        help="Append every project-declared, locally available static analyzer.",
    ),
    docker: bool = typer.Option(False, "--docker", help="Verify builds in a constrained Docker container."),
    docker_image: str | None = typer.Option(None, help="Container image; defaults by detected build system."),
    docker_cpus: float = typer.Option(2.0, min=0.1, max=64, help="Container CPU limit."),
    docker_memory: str = typer.Option("2g", help="Container memory limit."),
    docker_pids: int = typer.Option(256, min=16, max=4096, help="Container process limit."),
    allow_network: bool = typer.Option(False, help="Allow container networking; disabled by default."),
    patch_output: Path | None = typer.Option(
        None,
        "--patch-output",
        dir_okay=False,
        help="Write the verified standalone patch to this new file.",
    ),
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Create a local Git commit after --apply succeeds; never push or merge.",
    ),
    commit_message: str = typer.Option(
        "autofix: apply verified repair",
        help="Message for the optional local commit.",
    ),
) -> None:
    """Ask local Ollama for a repair; preview changes unless --apply is set."""

    if commit and not apply:
        console.print("[red]--commit requires --apply; preview never changes Git history.[/red]")
        raise typer.Exit(code=2)
    if not commit and commit_message != "autofix: apply verified repair":
        console.print("[red]--commit-message requires --commit.[/red]")
        raise typer.Exit(code=2)

    inspection = inspect_project(path)
    config = _load_config_or_exit(inspection.root)
    inspection = _select_inspection_or_exit(
        inspection, build_system or (config.build_system if config else None)
    )
    selected_command = _selected_build_command(command, config, inspection) if not step else None
    _announce_command_upgrade(config, selected_command)
    try:
        steps = _selected_build_steps(command, step, config, inspection)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    if not steps:
        console.print("[red]No supported build system detected; pass --command explicitly.[/red]")
        raise typer.Exit(code=2)
    try:
        steps = _append_static_analysis_steps(
            inspection.root,
            steps,
            enabled=with_static_analysis,
            docker=docker,
            isolated_workspace=True,
        )
    except ValueError as exc:
        console.print(f"[red]Static analysis configuration error: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    try:
        validate_git_repair_workspace(inspection.root)
    except (GitInspectionError, GitWorktreeError) as exc:
        console.print(f"[red]Repair stopped: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    client = OllamaClient(ollama_url)
    try:
        if isolate and keep_failed:
            raise DockerConfigurationError(
                "--keep-failed conflicts with --isolate because failed sandbox changes are discarded"
            )
        build_runner = run_build
        plan_docker_config: DockerConfig | None = None
        if docker:
            _require_docker()
            if config and config.offline and allow_network:
                raise DockerConfigurationError(
                    "--allow-network conflicts with runtime.offline: true in autofix.yml"
                )
            docker_runtime_config = _docker_config(
                inspection,
                docker_image or (config.docker_image if config else None),
                docker_cpus,
                docker_memory,
                docker_pids,
                allow_network,
            )
            plan_docker_config = docker_runtime_config

            def build_runner(root: Path, build_steps: tuple, **kwargs: object):  # type: ignore[no-redef]
                """Execute repair attempts with the selected Docker policy."""

                return run_build_docker(root, build_steps, config=docker_runtime_config, **kwargs)

        selected_model = model or (config.ollama_model if config else None)
        if selected_model is None:
            models = client.list_models()
            if not models:
                raise OllamaError("Ollama has no installed models")
            selected_model = models[0]
        console.print(f"Using local model: [bold]{selected_model}[/bold]")
        result = repair_project(
            inspection.root,
            steps,
            client,
            model=selected_model,
            max_attempts=max_attempts,
            timeout_seconds=timeout,
            apply=apply,
            keep_failed=keep_failed,
            allow_test_changes=allow_test_changes,
            allow_destructive_changes=allow_destructive_changes,
            isolate=isolate,
            build_runner=build_runner,
            patch_output=patch_output,
        )
    except (
        DockerConfigurationError,
        OllamaError,
        RepairEnvironmentError,
        RepairArtifactError,
        UnsafePatchError,
    ) as exc:
        console.print(f"[red]Repair stopped: {exc}[/red]")
        report_path = getattr(exc, "report_path", None)
        if report_path:
            console.print(f"Repair report: {report_path}")
        raise typer.Exit(code=2) from exc

    for rejection in result.rejections:
        console.print(
            f"[yellow]Rejected proposal {rejection.attempt}: {rejection.reason}[/yellow]"
        )
    if result.report_path:
        console.print(f"Repair report: {result.report_path}")
    if result.patch_path:
        console.print(f"Standalone patch: {result.patch_path}")
        console.print(f"Patch SHA-256: {result.patch_sha256}")
    if result.initial_build.succeeded:
        console.print("[green]The project already passes; no repair was requested.[/green]")
        return
    for index, proposal in enumerate(result.proposals, start=1):
        console.print(f"\n[bold]Proposal {index}:[/bold] {proposal.summary}")
    for change in result.changes:
        console.print(f"\n[bold]{change.relative_path}[/bold] — {change.reason}")
        console.print(change.diff or "(no textual diff)")

    if not apply:
        if not result.succeeded:
            console.print(
                "[red]No repair plan was created because no proposal passed verification.[/red]"
            )
            console.print("[green]The original project was unchanged.[/green]")
            raise typer.Exit(code=result.final_build.exit_code or 1)
        plan_path = save_repair_plan(
            inspection.root,
            model=selected_model,
            summary=result.proposals[-1].summary,
            changes=result.changes,
            verification_steps=steps,
            verification_runner="docker" if docker else "local",
            verification_timeout_seconds=timeout,
            docker_config=plan_docker_config,
            provenance=(
                PlanProvenance(
                    "git",
                    result.repository_fingerprint,
                    result.baseline_commit,
                    result.project_subpath,
                )
                if result.repository_root is not None
                else PlanProvenance("unversioned")
            ),
        )
        console.print(f"Repair plan: {plan_path}")
        location = (
            "a detached Git worktree"
            if result.workspace_kind == "git-worktree"
            else "an isolated project copy"
        )
        console.print(
            f"[green]Preview verified in {location}; the original project was unchanged.[/green]"
        )
        docker_option = " --docker" if docker else ""
        console.print(
            f"[yellow]Or apply this exact reviewed proposal with: autofix apply-plan \"{plan_path}\" --project \"{inspection.root}\"{docker_option}[/yellow]"
        )
        return
    if result.succeeded:
        if commit:
            try:
                commit_id = _commit_verified_result(result, commit_message)
            except GitCommitError as exc:
                console.print(f"[red]Local commit stopped: {exc}[/red]")
                raise typer.Exit(code=2) from exc
            console.print(f"[green]Created local commit: {commit_id}[/green]")
            console.print("[cyan]No push or merge was performed.[/cyan]")
        console.print("[green]Repair verified: the build now passes.[/green]")
        return
    if result.rolled_back:
        console.print("[yellow]Repair failed verification; all modified files were restored.[/yellow]")
    elif result.isolated and not result.applied:
        console.print("[yellow]Repair failed verification; the original project was unchanged.[/yellow]")
    else:
        console.print("[yellow]Repair failed verification; changes were kept as requested.[/yellow]")
    raise typer.Exit(code=result.final_build.exit_code or 1)


if __name__ == "__main__":
    app()

