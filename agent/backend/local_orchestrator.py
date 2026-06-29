"""Local single-challenge orchestration with local Codex solvers."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from backend.agents.local_codex_solver import LocalCodexSolver, check_codex_cli
from backend.local_shared import LocalSharedMemory
from backend.local_tools import LocalScope, LocalToolbox
from backend.local_triage import TriageResult, run_triage
from backend.skill_loader import SkillLoader

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteTarget:
    raw: str
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


async def solve_local_challenge(
    challenge_dir: Path,
    skills_path: Path,
    workspace: Path | None = None,
    remote: str | None = None,
    agents: int = 2,
    max_steps: int = 500,
    max_runtime_minutes: int | None = None,
    stop_on_flag: bool = True,
    continue_after_stuck: bool = True,
    model: str = "gpt-5.4",
    verbose: bool = False,
) -> dict[str, object]:
    if agents not in (1, 2):
        raise ValueError("Local mode supports 1 or 2 solver agents. Use --agents 1 or --agents 2.")
    challenge_dir = challenge_dir.expanduser().resolve()
    if not challenge_dir.exists() or not challenge_dir.is_dir():
        raise FileNotFoundError(f"Challenge directory not found: {challenge_dir}")

    codex_version = await check_codex_cli()
    triage = run_triage(challenge_dir)
    remote_target = parse_remote_target(remote)
    workspace = (workspace or Path("workspaces") / triage.name).expanduser().resolve()
    logger.info("Local challenge: %s", triage.name)
    logger.info("Workspace: %s", workspace)
    logger.info("Estimated categories: %s", ", ".join(triage.categories) or "unknown")
    if remote_target:
        logger.info("Remote target: %s:%d", remote_target.host, remote_target.port)
    logger.info("Triage files: %d", len(triage.files))
    memory = LocalSharedMemory(workspace)
    memory.initialize()
    is_resume = memory.has_prior_progress()
    if is_resume:
        logger.info("Resume mode: prior workspace progress detected")
    _write_initial_artifacts(memory, triage, skills_path, codex_version, remote_target)

    skill_loader = SkillLoader(skills_path)
    selected_skills = skill_loader.select(triage)
    logger.info("Selected skills: %s", ", ".join(skill.name for skill in selected_skills) or "none")
    skills_prompt = skill_loader.render_for_prompt(selected_skills)
    (memory.artifacts_dir / "selected_skills.md").write_text(
        "# Selected Skills\n\n"
        + "\n".join(f"- {skill.name}: {skill.path}" for skill in selected_skills)
        + "\n",
        encoding="utf-8",
    )

    stop_event = asyncio.Event()
    temp_root = Path(tempfile.mkdtemp(prefix=f"ctf-local-{triage.name}-"))
    prompt = build_local_solver_prompt(
        triage=triage,
        workspace=workspace,
        skills_path=skills_path,
        selected_skill_names=[skill.name for skill in selected_skills],
        skills_prompt=skills_prompt,
        max_steps=max_steps,
        continue_after_stuck=continue_after_stuck,
        agents=agents,
        remote_target=remote_target,
        resume_summary=memory.resume_summary() if is_resume else "",
    )

    solvers: list[LocalCodexSolver] = []
    agent_names = ("solver_a",) if agents == 1 else ("solver_a", "solver_b")
    for agent_name in agent_names:
        solver_dir = workspace / agent_name
        solver_dir.mkdir(parents=True, exist_ok=True)
        notes = solver_dir / "notes.md"
        if not notes.exists():
            notes.write_text(f"# {agent_name} Notes\n\n", encoding="utf-8")
        scope = LocalScope(
            challenge_dir=challenge_dir,
            workspace_dir=workspace,
            solver_dir=solver_dir,
            temp_dir=temp_root / agent_name,
            remote_target=remote_target.raw if remote_target else "",
            remote_host=remote_target.host if remote_target else "",
            remote_port=str(remote_target.port) if remote_target else "",
        )
        toolbox = LocalToolbox(agent_name, scope, memory, verbose=verbose)
        solvers.append(
            LocalCodexSolver(
                agent_name=agent_name,
                model=model,
                prompt=prompt.replace("{{AGENT_NAME}}", agent_name),
                toolbox=toolbox,
                memory=memory,
                max_steps=max_steps,
                stop_event=stop_event,
                verbose=verbose,
            )
        )

    started = time.monotonic()
    tasks = [asyncio.create_task(solver.run(), name=solver.agent_name) for solver in solvers]
    logger.info("Started %d solver(s): %s", len(solvers), ", ".join(solver.agent_name for solver in solvers))
    try:
        while tasks:
            timeout = 10.0
            if max_runtime_minutes is not None:
                remaining = max_runtime_minutes * 60 - (time.monotonic() - started)
                if remaining <= 0:
                    break
                timeout = min(timeout, max(0.1, remaining))
            done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc
            if verbose:
                elapsed = int(time.monotonic() - started)
                progress = ", ".join(f"{solver.agent_name}={solver.step_count}/{max_steps}" for solver in solvers)
                logger.info("Progress %ss: %s", elapsed, progress)
            final = memory.final_flag()
            final_is_remote = bool(remote_target and _final_has_remote_evidence(memory, remote_target))
            if final and stop_on_flag and (not remote_target or final_is_remote):
                stop_event.set()
                for task in pending:
                    task.cancel()
                tasks = list(done | pending)
                break
            tasks = list(pending)
            if done and not pending:
                break
    finally:
        stop_event.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(solver.stop() for solver in solvers), return_exceptions=True)

    final_flag = memory.final_flag()
    if remote_target and not _final_has_remote_evidence(memory, remote_target):
        final_flag = None
    if final_flag:
        writeup_path = generate_writeup(memory, triage, selected_skill_names=[skill.name for skill in selected_skills])
        logger.info("Writeup generated: %s", writeup_path)
    else:
        writeup_path = None
        summary_path = generate_run_summary(memory, triage, selected_skill_names=[skill.name for skill in selected_skills])
        logger.info("Run summary generated: %s", summary_path)

    return {
        "challenge": triage.name,
        "workspace": str(workspace),
        "final_flag": final_flag,
        "writeup": str(writeup_path) if writeup_path else None,
        "selected_skills": [skill.name for skill in selected_skills],
        "codex_version": codex_version,
        "remote": remote_target.raw if remote_target else None,
    }


def build_local_solver_prompt(
    triage: TriageResult,
    workspace: Path,
    skills_path: Path,
    selected_skill_names: list[str],
    skills_prompt: str,
    max_steps: int,
    continue_after_stuck: bool,
    agents: int,
    remote_target: RemoteTarget | None = None,
    resume_summary: str = "",
) -> str:
    coordination = (
        "- You are running as a single local solver. Still use shared memory as your durable notebook."
        if agents == 1
        else (
            "- solver_a and solver_b work on the same challenge.\n"
            "- Both can read and write /shared.\n"
            "- Treat the other solver's findings as live context, but independently verify before finalizing."
        )
    )
    resume_block = ""
    if resume_summary:
        resume_block = f"""
Resume mode:
- This workspace already contains progress from an earlier run.
- Your first action must be read_shared_memory.
- Then summarize the current state in /shared/hypotheses.md with append_finding(kind="hypothesis").
- Continue from existing evidence, scripts, artifacts, and candidate flags instead of restarting from scratch.
- Do not repeat approaches listed in /shared/dead_ends.md unless you have a new reason.
- Re-check previous candidate flags only when you can add stronger evidence.

# Previous Workspace State

{resume_summary}
"""
    remote_block = ""
    if remote_target:
        remote_block = f"""
Remote target:
- Operator provided --remote {remote_target.raw}
- Host: {remote_target.host}
- Port: {remote_target.port}
- Web base URL default: {remote_target.base_url}
- Environment variables available in bash: CTF_REMOTE={remote_target.raw}, CTF_REMOTE_HOST={remote_target.host}, CTF_REMOTE_PORT={remote_target.port}, TARGET_HOST={remote_target.host}, TARGET_PORT={remote_target.port}, BASE_URL={remote_target.base_url}

Remote solve policy for web/pwn/remote-service challenges:
- Treat the local source bundle as authoritative for the remote service unless direct remote behavior disproves it.
- Before broad fuzzing or port scanning, read the local source, identify the exact intended bug chain, and prove it locally with commands/scripts.
- Use previous dead ends to avoid repeating failed paths; the prior web_gridwatch remote run already showed local-minted SAML fails remotely with fingerprint mismatch, normal user login is not admin, direct Node-RED relay needs admin, and XXE attempts did not exfiltrate the IdP key.
- First develop and verify the solve locally when the challenge bundle supports a local service.
- After local success, run the same solve script against the provided remote target using editable host/port variables or CLI args.
- Do not call report_final_flag for a local-only flag when --remote is provided and the challenge is web, pwn, or another network service.
- Debug all remote-only failures: wrong URL, missing scheme, cookie/session mismatch, SAML/JWT/audience mismatch, SSRF host encoding, payload timing, bad libc/offsets, protocol prompts, network timeouts, and service restarts.
- Keep iterating until the remote service returns the real remote flag, then call report_final_flag with remote command output as evidence.
- If the remote service is unreachable or the provided target is malformed, record that exact error as a dead_end/evidence instead of finalizing a local flag.
"""
    return f"""You are {{{{AGENT_NAME}}}}, a local CTF solver.

You are solving one local challenge only. There is no CTFd, no submission API, no provider API key fallback, and no competition coordinator.

Paths:
- Challenge files: /challenge = {triage.challenge_path}
- Shared memory: /shared = {workspace / "shared"}
- Your private workspace: /solver = {workspace / "{{AGENT_NAME}}"}
- Artifacts: /artifacts = {workspace / "artifacts"}
- Reports: /reports = {workspace / "reports"}
- ctf-skills repo: {skills_path}

{resume_block}
{remote_block}

Local workflow:
1. Read shared memory before major pivots.
2. Triage and solve with local tools. Use bash freely; it runs on the host in the challenge directory.
3. Write scripts to /solver/scripts or /artifacts/scripts.
4. Record important findings immediately with append_finding so the other solver can see them.
5. Record candidate flags with record_candidate_flag, including exact evidence and source.
6. Use report_final_flag only when the flag is proven by local evidence from files, command output, script output, or challenge behavior.
7. If stuck, write a dead_end, read shared memory again, try another category/skill, and keep going.

Output requirements:
- Write solver notes, evidence, and final explanations in Bahasa Indonesia.
- Keep findings concise: record only observations, commands, outputs, payloads, and reasoning that are needed to reproduce the solve.
- Before calling report_final_flag, create a runnable solve script in /solver/scripts or /artifacts/scripts unless the challenge is purely manual. If no script is useful, record the reason as evidence.
- Prefer a single complete solve script named solve.py or solve.sh that starts from the provided challenge data or remote service and prints the final flag.
- For web, pwn, or any remote-service challenge, put target values in variables at the top of the script so the operator can change them easily. Do not hard-code an IP/host/port only inside payload logic.
- Recommended Python variable pattern:
  HOST = os.environ.get("HOST", "127.0.0.1")
  PORT = int(os.environ.get("PORT", "31337"))
  BASE_URL = os.environ.get("BASE_URL", f"http://{{HOST}}:{{PORT}}")
- Include the solve script path and a successful run/output in the evidence used for report_final_flag.
- If --remote is provided for a web/pwn/remote-service challenge, the successful run/output must come from the remote target, not only from local Docker or local files.

Tool/install policy:
- You may install missing tools with apt, pip, cargo, go install, gem, npm, or git clone.
- Install commands are automatically logged to /artifacts/install_log.md.
- Docker is optional only for challenge services/binaries when needed. Docker commands are logged to /artifacts/docker_log.md.
- Do not use Docker just to run yourself as an agent.
- Keep normal file reads/writes inside /challenge, /workspace, /shared, /artifacts, /reports, /solver, or TMPDIR. Do not inspect ~/.codex or other host-private files while solving.

Flag policy:
- There is no CTFd submit. Verification is local.
- Do not accept guessed flags or placeholders.
- Prefer candidates that appear in command output, decoded artifacts, service behavior, or source-derived proof.
- Common wrappers include flag{{...}}, CTF{{...}}, INS{{...}}, BSidesSF{{...}}, and any format hinted by description.

Solver coordination:
{coordination}

Max tool steps for this solver: {max_steps}
Continue after stuck: {continue_after_stuck}
Selected skills: {", ".join(selected_skill_names) or "none"}

{triage.to_prompt()}

---

# Relevant ctf-skills Context

Only relevant skill files are included below. If a referenced supporting file is needed, read it directly from the ctf-skills repo path using bash/read_file.

{skills_prompt}
"""


def _write_initial_artifacts(
    memory: LocalSharedMemory,
    triage: TriageResult,
    skills_path: Path,
    codex_version: str,
    remote_target: RemoteTarget | None,
) -> None:
    (memory.artifacts_dir / "triage.md").write_text(triage.to_prompt(limit=200_000), encoding="utf-8")
    remote_lines = ""
    if remote_target:
        remote_lines = (
            f"- Remote target: {remote_target.raw}\n"
            f"- Remote host: {remote_target.host}\n"
            f"- Remote port: {remote_target.port}\n"
            f"- Remote web base URL default: {remote_target.base_url}\n"
        )
    (memory.artifacts_dir / "run_config.md").write_text(
        "# Local Run Config\n\n"
        f"- Challenge: {triage.challenge_path}\n"
        f"- Workspace: {memory.workspace}\n"
        f"- Skills path: {skills_path}\n"
        f"- Codex: {codex_version}\n"
        f"{remote_lines}"
        f"- Mode: local single challenge, local Codex solver(s), no CTFd\n",
        encoding="utf-8",
    )


def parse_remote_target(remote: str | None) -> RemoteTarget | None:
    if not remote:
        return None
    raw = remote.strip()
    if not raw:
        return None
    if "://" in raw:
        raise ValueError("Remote target must use ip:port or host:port, without http:// or https://.")
    if ":" not in raw:
        raise ValueError("Remote target must use ip:port or host:port.")
    host, port_text = raw.rsplit(":", 1)
    host = host.strip().strip("[]")
    port_text = port_text.strip()
    if not host:
        raise ValueError("Remote target host is empty.")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"Remote target port is not an integer: {port_text}") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"Remote target port is outside 1-65535: {port}")
    return RemoteTarget(raw=f"{host}:{port}", host=host, port=port)


def _final_has_remote_evidence(memory: LocalSharedMemory, remote_target: RemoteTarget) -> bool:
    path = memory.shared_dir / "final_flag_evidence.md"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    needles = {
        remote_target.raw,
        remote_target.host,
        f":{remote_target.port}",
        f"TARGET_HOST={remote_target.host}",
        f"TARGET_PORT={remote_target.port}",
        f"HOST={remote_target.host}",
        f"PORT={remote_target.port}",
        f"CTF_REMOTE={remote_target.raw}",
    }
    return any(needle and needle in text for needle in needles)


def generate_writeup(memory: LocalSharedMemory, triage: TriageResult, selected_skill_names: list[str]) -> Path:
    final_flag = memory.final_flag() or ""
    files = "\n".join(f"- `{item.path}` ({item.file_type}, {item.size} bytes)" for item in triage.files[:80])
    if len(triage.files) > 80:
        files += f"\n- ... {len(triage.files) - 80} file lain disembunyikan agar writeup tetap ringkas."
    scripts = _render_script_section(memory.workspace)
    path = memory.reports_dir / "writeup.md"
    path.write_text(
        f"""# Writeup: {triage.name}

## Ringkasan

Challenge `{triage.name}` diselesaikan secara lokal. Writeup ini hanya memuat informasi yang diperlukan untuk memvalidasi ulang solve dan flag.

## Informasi Challenge

- Nama: `{triage.name}`
- Kategori: {", ".join(triage.categories) or "unknown"}
- Skill dipakai: {", ".join(selected_skill_names) or "none"}
- Path challenge: `{triage.challenge_path}`

## File Diberikan

{files or "Tidak ada file yang diberikan."}

## Solusi

{_read(memory.shared_dir / "evidence.md") or "Detail solusi tidak dicatat terpisah. Lihat bukti final dan skrip solve di bawah."}

## Skrip Solve

{scripts or "Tidak ada skrip solve yang tercatat."}

## Bukti Flag

{_read(memory.shared_dir / "final_flag_evidence.md")}

## Flag

```text
{final_flag}
```
""",
        encoding="utf-8",
    )
    return path


def generate_run_summary(memory: LocalSharedMemory, triage: TriageResult, selected_skill_names: list[str]) -> Path:
    path = memory.reports_dir / "run_summary.md"
    path.write_text(
        f"""# Ringkasan Run Lokal: {triage.name}

Belum ada flag final yang terverifikasi.

## Kategori

{", ".join(triage.categories)}

## Skill Dipilih

{", ".join(selected_skill_names)}

## Kandidat Flag

{_read(memory.shared_dir / "candidate_flags.md")}

## Bukti

{_read(memory.shared_dir / "evidence.md")}

## Dead End

{_read(memory.shared_dir / "dead_ends.md")}
""",
        encoding="utf-8",
    )
    return path


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _list_scripts(workspace: Path) -> str:
    rows: list[str] = []
    for root in (workspace / "solver_a" / "scripts", workspace / "solver_b" / "scripts", workspace / "artifacts" / "scripts"):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                rows.append(f"- `{path.relative_to(workspace)}`")
    return "\n".join(rows)


def _render_script_section(workspace: Path, max_files: int = 5, max_chars: int = 16_000) -> str:
    scripts: list[Path] = []
    for root in (workspace / "solver_a" / "scripts", workspace / "solver_b" / "scripts", workspace / "artifacts" / "scripts"):
        if not root.exists():
            continue
        scripts.extend(path for path in sorted(root.rglob("*")) if path.is_file())
    if not scripts:
        return ""

    preferred = sorted(
        scripts,
        key=lambda path: (
            0 if path.name.lower() in {"solve.py", "solve.sh", "exploit.py", "exploit.sh"} else 1,
            len(path.read_text(encoding="utf-8", errors="replace")) if _is_text_script(path) else 1_000_000,
            str(path),
        ),
    )[:max_files]

    chunks: list[str] = []
    remaining = max_chars
    for script in preferred:
        rel = script.relative_to(workspace)
        if not _is_text_script(script):
            chunks.append(f"- `{rel}` (file biner/non-teks)")
            continue
        text = script.read_text(encoding="utf-8", errors="replace")
        if remaining <= 0:
            chunks.append(f"- `{rel}` (konten tidak ditampilkan karena batas ringkas)")
            continue
        shown = text[:remaining]
        remaining -= len(shown)
        suffix = "\n\n[script dipotong agar writeup tetap ringkas]" if len(shown) < len(text) else ""
        chunks.append(f"### `{rel}`\n\n```{_code_fence_language(script)}\n{shown}{suffix}\n```")
    return "\n\n".join(chunks)


def _is_text_script(path: Path) -> bool:
    return path.suffix.lower() in {".py", ".sh", ".rb", ".pl", ".sage", ".js", ".ts", ".php", ".txt"} or path.name in {
        "solve",
        "exploit",
    }


def _code_fence_language(path: Path) -> str:
    return {
        ".py": "python",
        ".sh": "bash",
        ".rb": "ruby",
        ".pl": "perl",
        ".sage": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".php": "php",
    }.get(path.suffix.lower(), "text")
