"""CLI for local single-challenge Codex solving."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console

from backend.local_orchestrator import parse_remote_target, solve_local_challenge
from backend.skill_loader import default_skills_path

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%X"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("challenge_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--skills-path", type=click.Path(file_okay=False, path_type=Path), default=None, help="Path to ctf-skills repository.")
@click.option("--workspace", type=click.Path(file_okay=False, path_type=Path), default=None, help="Workspace directory for this challenge.")
@click.option("--remote", type=str, default=None, help="Remote target for web/pwn services in ip:port or host:port form.")
@click.option("--agents", type=click.IntRange(1, 2), default=2, show_default=True, help="Number of local Codex solvers.")
@click.option("--max-steps", type=int, default=500, show_default=True, help="Max tool calls per solver.")
@click.option("--max-runtime-minutes", type=int, default=None, help="Optional wall-clock runtime limit.")
@click.option("--stop-on-flag/--no-stop-on-flag", default=True, show_default=True, help="Stop both solvers after final flag verification.")
@click.option("--continue-after-stuck/--no-continue-after-stuck", default=True, show_default=True, help="Tell solvers to pivot rather than stop when stuck.")
@click.option("--model", default="gpt-5.4", show_default=True, help="Codex model id for both solvers.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
def main(
    challenge_dir: Path,
    skills_path: Path | None,
    workspace: Path | None,
    remote: str | None,
    agents: int,
    max_steps: int,
    max_runtime_minutes: int | None,
    stop_on_flag: bool,
    continue_after_stuck: bool,
    model: str,
    verbose: bool,
) -> None:
    """Solve one local challenge folder with local Codex solver(s)."""
    _setup_logging(verbose)
    try:
        remote_target = parse_remote_target(remote)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--remote") from exc
    resolved_skills = (skills_path or default_skills_path(Path.cwd())).expanduser().resolve()
    console.print("[bold]CTF Agent Local Solve[/bold]")
    console.print(f"  Challenge: {challenge_dir.resolve()}")
    console.print(f"  Skills:    {resolved_skills}")
    if remote_target:
        console.print(f"  Remote:    {remote_target.raw}")
    console.print(f"  Agents:    {agents}")
    console.print(f"  Model:     {model}")
    console.print("  Backend:   Codex CLI app-server (ChatGPT login, no API keys)")
    console.print()

    if not resolved_skills.exists():
        console.print(f"[red]ctf-skills path not found:[/red] {resolved_skills}")
        console.print("Pass --skills-path or set CTF_SKILLS_PATH.")
        sys.exit(2)

    try:
        result = asyncio.run(
            solve_local_challenge(
                challenge_dir=challenge_dir,
                skills_path=resolved_skills,
                workspace=workspace,
                remote=remote_target.raw if remote_target else None,
                agents=agents,
                max_steps=max_steps,
                max_runtime_minutes=max_runtime_minutes,
                stop_on_flag=stop_on_flag,
                continue_after_stuck=continue_after_stuck,
                model=model,
                verbose=verbose,
            )
        )
    except Exception as exc:
        console.print(f"[red]Local solve failed:[/red] {exc}")
        sys.exit(1)

    console.print("\n[bold]Result[/bold]")
    console.print(f"  Workspace: {result['workspace']}")
    console.print(f"  Selected skills: {', '.join(result['selected_skills'])}")
    if result.get("final_flag"):
        console.print(f"  [bold green]Final flag:[/bold green] {result['final_flag']}")
        console.print(f"  Writeup: {result['writeup']}")
    else:
        console.print("  [bold yellow]No final flag verified.[/bold yellow]")
        console.print(f"  Summary: {result['workspace']}/reports/run_summary.md")


if __name__ == "__main__":
    main()
