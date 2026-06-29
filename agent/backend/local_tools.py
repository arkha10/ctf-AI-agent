"""Permissive local tools with filesystem scope checks for local Codex solvers."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from backend.local_shared import LocalSharedMemory
from backend.tools.core import IMAGE_EXTS_FOR_VISION, _has_valid_magic, _truncate

logger = logging.getLogger(__name__)

INSTALL_RE = re.compile(
    r"(^|\s)(sudo\s+)?(apt(-get)?\s+install|pip(x)?\s+install|uv\s+pip\s+install|"
    r"cargo\s+install|go\s+install|gem\s+install|npm\s+install\s+-g|git\s+clone)\b"
)
DOCKER_RE = re.compile(r"(^|\s)(docker|docker-compose)\b")


@dataclass
class LocalScope:
    challenge_dir: Path
    workspace_dir: Path
    solver_dir: Path
    temp_dir: Path
    remote_target: str = ""
    remote_host: str = ""
    remote_port: str = ""

    def __post_init__(self) -> None:
        self.challenge_dir = self.challenge_dir.resolve()
        self.workspace_dir = self.workspace_dir.resolve()
        self.solver_dir = self.solver_dir.resolve()
        self.temp_dir = self.temp_dir.resolve()
        self.solver_dir.mkdir(parents=True, exist_ok=True)
        (self.solver_dir / "scripts").mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return (self.challenge_dir, self.workspace_dir, self.temp_dir)

    def resolve(self, requested: str | Path, default_base: Path | None = None) -> Path:
        raw = str(requested or ".")
        mapped = self._map_virtual(raw)
        path = Path(mapped)
        if not path.is_absolute():
            path = (default_base or self.challenge_dir) / path
        resolved = path.expanduser().resolve()
        if not self.is_allowed(resolved):
            allowed = ", ".join(str(p) for p in self.allowed_roots)
            raise PermissionError(f"path outside local CTF scope: {resolved}. Allowed roots: {allowed}")
        return resolved

    def is_allowed(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except FileNotFoundError:
            resolved = path.parent.resolve() / path.name
        return any(resolved == root or root in resolved.parents for root in self.allowed_roots)

    def _map_virtual(self, raw: str) -> str:
        mappings = {
            "/challenge": self.challenge_dir,
            "/workspace": self.workspace_dir,
            "/shared": self.workspace_dir / "shared",
            "/artifacts": self.workspace_dir / "artifacts",
            "/reports": self.workspace_dir / "reports",
            "/solver": self.solver_dir,
            "/tmp/ctf-agent-local": self.temp_dir,
        }
        for prefix, target in mappings.items():
            if raw == prefix:
                return str(target)
            if raw.startswith(prefix + "/"):
                return str(target / raw[len(prefix) + 1 :])
        return raw


class LocalToolbox:
    def __init__(self, agent: str, scope: LocalScope, memory: LocalSharedMemory, verbose: bool = False) -> None:
        self.agent = agent
        self.scope = scope
        self.memory = memory
        self.verbose = verbose
        self.command_log = scope.solver_dir / "commands.log"

    async def bash(self, command: str, timeout_seconds: int = 120) -> str:
        command = command.strip()
        if not command:
            return "No command provided."
        await self._log_command(command)
        if self.verbose:
            logger.info("[%s] bash: %s", self.agent, _one_line(command, 500))
        env = os.environ.copy()
        env.update(
            {
                "CTF_CHALLENGE_DIR": str(self.scope.challenge_dir),
                "CTF_WORKSPACE": str(self.scope.workspace_dir),
                "CTF_SHARED": str(self.memory.shared_dir),
                "CTF_ARTIFACTS": str(self.memory.artifacts_dir),
                "CTF_REPORTS": str(self.memory.reports_dir),
                "CTF_SOLVER_DIR": str(self.scope.solver_dir),
                "TMPDIR": str(self.scope.temp_dir),
            }
        )
        if self.scope.remote_target:
            env.update(
                {
                    "CTF_REMOTE": self.scope.remote_target,
                    "CTF_REMOTE_HOST": self.scope.remote_host,
                    "CTF_REMOTE_PORT": self.scope.remote_port,
                    "TARGET_HOST": self.scope.remote_host,
                    "TARGET_PORT": self.scope.remote_port,
                    "HOST": self.scope.remote_host,
                    "PORT": self.scope.remote_port,
                    "BASE_URL": f"http://{self.scope.remote_host}:{self.scope.remote_port}",
                }
            )
        try:
            shell = shutil.which("bash")
            kwargs = {"executable": shell} if shell else {}
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self.scope.challenge_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                result = f"Command timed out after {timeout_seconds}s: {command}"
                if self.verbose:
                    logger.info("[%s] bash timeout after %ss", self.agent, timeout_seconds)
                return result
        except Exception as exc:
            result = f"Command failed to start: {exc}"
            if self.verbose:
                logger.info("[%s] bash failed to start: %s", self.agent, exc)
            return result

        parts: list[str] = []
        if stdout:
            parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            parts.append("[stderr]\n" + stderr.decode("utf-8", errors="replace"))
        if proc.returncode:
            parts.append(f"[exit {proc.returncode}]")
        text = "\n".join(parts).strip() or "(no output)"
        for flag in self.memory.extract_candidates(text):
            await self.memory.record_candidate_flag(
                flag=flag,
                evidence=f"Detected in command output:\n\n{_truncate(text, 4000)}",
                source=f"bash: {command[:200]}",
                agent=self.agent,
                confidence="medium",
            )
        result = _truncate(text)
        if self.verbose:
            logger.info("[%s] bash result: %s", self.agent, _summarize_tool_result(result))
        return result

    async def read_file(self, path: str) -> str:
        if self.verbose:
            logger.info("[%s] read_file: %s", self.agent, path)
        try:
            resolved = self.scope.resolve(path)
            data = resolved.read_bytes()
        except Exception as exc:
            return f"Error reading file: {exc}"
        if _looks_binary(data):
            return (
                f"Binary file ({len(data)} bytes) at {resolved}. Use bash tools such as:\n"
                f"file {shlex.quote(str(resolved))}\n"
                f"xxd {shlex.quote(str(resolved))} | head -80\n"
                f"strings -a {shlex.quote(str(resolved))} | head -200"
            )
        return _truncate(data.decode("utf-8", errors="replace"))

    async def write_file(self, path: str, content: str) -> str:
        if self.verbose:
            logger.info("[%s] write_file: %s (%d chars)", self.agent, path, len(content))
        try:
            resolved = self.scope.resolve(path, default_base=self.scope.solver_dir)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {resolved}"
        except Exception as exc:
            return f"Error writing file: {exc}"

    async def list_files(self, path: str = "/challenge") -> str:
        if self.verbose:
            logger.info("[%s] list_files: %s", self.agent, path)
        try:
            resolved = self.scope.resolve(path)
            if not resolved.exists():
                return f"Path does not exist: {resolved}"
            if resolved.is_file():
                return f"{resolved} is a file ({resolved.stat().st_size} bytes)"
            rows = []
            for child in sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                stat = child.stat()
                kind = "d" if child.is_dir() else "-"
                rows.append(f"{kind} {stat.st_size:>10} {child.name}")
            return "\n".join(rows) or f"{resolved} is empty."
        except Exception as exc:
            return f"Error listing files: {exc}"

    async def view_image(self, path: str) -> tuple[bytes, str] | str:
        if self.verbose:
            logger.info("[%s] view_image: %s", self.agent, path)
        try:
            resolved = self.scope.resolve(path)
            ext = resolved.suffix.lower()
            mime_type = IMAGE_EXTS_FOR_VISION.get(ext)
            if not mime_type:
                return f"Not a supported image type: {resolved}"
            data = resolved.read_bytes()
            if not _has_valid_magic(data, mime_type):
                return "Image has an invalid or corrupted magic header."
            if len(data) > 4 * 1024 * 1024:
                return f"Image too large for direct vision ({len(data)} bytes). Use local tools first."
            return data, mime_type
        except Exception as exc:
            return f"Error viewing image: {exc}"

    async def read_shared_memory(self) -> str:
        if self.verbose:
            logger.info("[%s] read_shared_memory", self.agent)
        return self.memory.render_shared_context()

    async def append_finding(self, content: str, kind: str = "finding") -> str:
        if self.verbose:
            logger.info("[%s] append_%s: %s", self.agent, kind, _one_line(content, 500))
        return await self.memory.append_finding(self.agent, content, kind)

    async def record_candidate_flag(
        self,
        flag: str,
        evidence: str,
        source: str = "",
        confidence: str = "medium",
    ) -> str:
        if self.verbose:
            logger.info("[%s] candidate flag: %s (%s)", self.agent, flag, confidence)
        return await self.memory.record_candidate_flag(flag, evidence, source, self.agent, confidence)

    async def report_final_flag(self, flag: str, evidence: str, source: str = "") -> str:
        if self.verbose:
            logger.info("[%s] report_final_flag: %s", self.agent, flag)
        if self.scope.remote_target and not _mentions_remote_target(
            evidence=evidence,
            source=source,
            remote_target=self.scope.remote_target,
            remote_host=self.scope.remote_host,
            remote_port=self.scope.remote_port,
        ):
            await self.memory.record_candidate_flag(
                flag=flag,
                evidence=evidence,
                source=source,
                agent=self.agent,
                confidence="low",
            )
            return (
                "Rejected final flag because --remote is configured, but the final evidence/source "
                f"does not mention the remote target {self.scope.remote_target}. Run the solve script "
                "against the remote target and include that command/output as evidence."
            )
        return await self.memory.finalize_flag(flag, evidence, source, self.agent)

    async def _log_command(self, command: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.command_log.parent.mkdir(parents=True, exist_ok=True)
        with self.command_log.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {command}\n")
        if INSTALL_RE.search(command):
            with (self.memory.artifacts_dir / "install_log.md").open("a", encoding="utf-8") as f:
                f.write(f"## {self.agent} - {stamp}\n\n```bash\n{command}\n```\n\n")
        if DOCKER_RE.search(command):
            with (self.memory.artifacts_dir / "docker_log.md").open("a", encoding="utf-8") as f:
                f.write(f"## {self.agent} - {stamp}\n\n```bash\n{command}\n```\n\n")


def image_tuple_to_content_item(result: tuple[bytes, str]) -> dict[str, str]:
    data, mime_type = result
    encoded = base64.b64encode(data).decode()
    return {"type": "inputImage", "imageUrl": f"data:{mime_type};base64,{encoded}"}


def _looks_binary(data: bytes) -> bool:
    sample = data[:4096]
    if not sample:
        return False
    non_text = sum(
        1
        for b in sample
        if b == 0 or (b < 9 and b not in (7, 8)) or (9 < b < 13) or (13 < b < 32 and b != 27)
    )
    return non_text / len(sample) > 0.05


def _one_line(text: str, limit: int) -> str:
    cleaned = text.replace("\x00", "\\0")
    compact = " ".join(cleaned.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + f"... [{len(compact)} chars]"


def _summarize_tool_result(text: str) -> str:
    lines = text.splitlines()
    first = _one_line(lines[0], 500) if lines else "(no output)"
    return f"{len(text)} chars, {len(lines)} lines; {first}"


def _mentions_remote_target(
    *,
    evidence: str,
    source: str,
    remote_target: str,
    remote_host: str,
    remote_port: str,
) -> bool:
    text = f"{evidence}\n{source}"
    needles = {
        remote_target,
        remote_host,
        f":{remote_port}",
        f"TARGET_HOST={remote_host}",
        f"TARGET_PORT={remote_port}",
        f"HOST={remote_host}",
        f"PORT={remote_port}",
        f"CTF_REMOTE={remote_target}",
    }
    return any(needle and needle in text for needle in needles)
