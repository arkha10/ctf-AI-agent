"""File-backed shared memory and flag handling for local single-challenge runs."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

COMMON_FLAG_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9_.:-]{0,40}\{[^{}\s]{1,300}\})"
)
PLACEHOLDER_RE = re.compile(r"\{(?:flag|placeholder|test|example|todo|redacted)\}", re.I)


@dataclass(frozen=True)
class CandidateFlag:
    flag: str
    evidence: str
    source: str
    agent: str
    confidence: str
    timestamp: float


class LocalSharedMemory:
    """Append-only shared workspace that both local solvers can read and update."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.shared_dir = self.workspace / "shared"
        self.artifacts_dir = self.workspace / "artifacts"
        self.reports_dir = self.workspace / "reports"
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        for path in (self.shared_dir, self.artifacts_dir, self.reports_dir):
            path.mkdir(parents=True, exist_ok=True)
        for name, title in (
            ("findings.jsonl", ""),
            ("hypotheses.md", "# Hypotheses\n\n"),
            ("evidence.md", "# Evidence\n\n"),
            ("dead_ends.md", "# Dead Ends\n\n"),
            ("candidate_flags.md", "# Candidate Flags\n\n"),
            ("final_flag_evidence.md", "# Final Flag Evidence\n\n"),
        ):
            path = self.shared_dir / name
            if not path.exists():
                path.write_text(title, encoding="utf-8")
        install_log = self.artifacts_dir / "install_log.md"
        if not install_log.exists():
            install_log.write_text("# Install Commands\n\n", encoding="utf-8")
        docker_log = self.artifacts_dir / "docker_log.md"
        if not docker_log.exists():
            docker_log.write_text("# Docker Commands\n\n", encoding="utf-8")

    @property
    def final_flag_path(self) -> Path:
        return self.shared_dir / "final_flag.txt"

    def final_flag(self) -> str | None:
        if not self.final_flag_path.exists():
            return None
        flag = self.final_flag_path.read_text(encoding="utf-8", errors="replace").strip()
        return flag or None

    def has_prior_progress(self) -> bool:
        """Return True when a workspace contains useful state from an earlier run."""
        markers = (
            self.shared_dir / "findings.jsonl",
            self.shared_dir / "hypotheses.md",
            self.shared_dir / "evidence.md",
            self.shared_dir / "dead_ends.md",
            self.shared_dir / "candidate_flags.md",
            self.artifacts_dir / "install_log.md",
            self.artifacts_dir / "docker_log.md",
        )
        for path in markers:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text and text not in {
                "# Hypotheses",
                "# Evidence",
                "# Dead Ends",
                "# Candidate Flags",
                "# Install Commands",
                "# Docker Commands",
            }:
                return True
        return False

    def resume_summary(self, limit: int = 36_000) -> str:
        """Render previous workspace state for a fresh Codex thread."""
        sections: list[str] = []
        for title, path in (
            ("Previous hypotheses", self.shared_dir / "hypotheses.md"),
            ("Previous evidence", self.shared_dir / "evidence.md"),
            ("Previous dead ends", self.shared_dir / "dead_ends.md"),
            ("Previous candidate flags", self.shared_dir / "candidate_flags.md"),
            ("Previous final flag evidence", self.shared_dir / "final_flag_evidence.md"),
            ("Previous install commands", self.artifacts_dir / "install_log.md"),
            ("Previous Docker commands", self.artifacts_dir / "docker_log.md"),
            ("Previous run summary", self.reports_dir / "run_summary.md"),
        ):
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    sections.append(f"## {title}\n\n{text}")
        findings = self._recent_findings(80)
        if findings:
            sections.append("## Recent shared findings\n\n```jsonl\n" + findings + "\n```")
        final = self.final_flag()
        if final:
            sections.append(f"## Existing final flag\n\n{final}")
        text = "\n\n".join(sections)
        if len(text) > limit:
            return text[-limit:] + "\n\n[resume summary truncated to most recent content]"
        return text or "No prior workspace state found."

    async def append_finding(self, agent: str, content: str, kind: str = "finding") -> str:
        content = content.strip()
        if not content:
            return "Empty finding ignored."
        record = {
            "ts": time.time(),
            "agent": agent,
            "kind": kind,
            "content": content,
        }
        async with self._lock:
            with (self.shared_dir / "findings.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            target = {
                "hypothesis": "hypotheses.md",
                "evidence": "evidence.md",
                "dead_end": "dead_ends.md",
            }.get(kind)
            if target:
                with (self.shared_dir / target).open("a", encoding="utf-8") as f:
                    f.write(f"## {agent} - {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{content}\n\n")
        return f"Recorded {kind}."

    async def record_candidate_flag(
        self,
        flag: str,
        evidence: str,
        source: str,
        agent: str,
        confidence: str = "medium",
    ) -> str:
        flag = flag.strip()
        evidence = evidence.strip()
        if not flag:
            return "Empty flag candidate ignored."
        async with self._lock:
            with (self.shared_dir / "candidate_flags.md").open("a", encoding="utf-8") as f:
                f.write(
                    f"## {flag}\n\n"
                    f"- Agent: {agent}\n"
                    f"- Confidence: {confidence}\n"
                    f"- Source: {source or 'unknown'}\n"
                    f"- Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"- Evidence: {evidence or 'not provided'}\n\n"
                )
            with (self.shared_dir / "findings.jsonl").open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "agent": agent,
                            "kind": "candidate_flag",
                            "flag": flag,
                            "source": source,
                            "evidence": evidence,
                            "confidence": confidence,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        return f"Recorded candidate flag: {flag}"

    async def finalize_flag(self, flag: str, evidence: str, source: str, agent: str) -> str:
        verdict, reason = self.verify_candidate(flag, evidence)
        if not verdict:
            await self.record_candidate_flag(flag, evidence, source, agent, confidence="low")
            return f"Rejected final flag for now: {reason}. Candidate was recorded."
        async with self._lock:
            self.final_flag_path.write_text(flag.strip() + "\n", encoding="utf-8")
            with (self.shared_dir / "final_flag_evidence.md").open("a", encoding="utf-8") as f:
                f.write(
                    f"## {flag.strip()}\n\n"
                    f"- Agent: {agent}\n"
                    f"- Source: {source or 'unknown'}\n"
                    f"- Verification: {reason}\n\n"
                    f"{evidence.strip()}\n\n"
                )
        await self.record_candidate_flag(flag, evidence, source, agent, confidence="high")
        return f"Final flag accepted: {flag.strip()}"

    def verify_candidate(self, flag: str, evidence: str) -> tuple[bool, str]:
        flag = flag.strip()
        evidence = evidence.strip()
        if not flag:
            return False, "flag is empty"
        if PLACEHOLDER_RE.search(flag):
            return False, "candidate looks like a placeholder"
        if COMMON_FLAG_RE.fullmatch(flag):
            if evidence:
                return True, "candidate matches a flag wrapper and includes evidence"
            return False, "candidate matches a wrapper but has no evidence"
        if evidence and flag in evidence and len(flag) >= 8:
            return True, "candidate is explicitly present in the supplied evidence"
        return False, "candidate does not have enough local evidence"

    def extract_candidates(self, text: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for match in COMMON_FLAG_RE.finditer(text):
            flag = match.group(1)
            if flag not in seen and not PLACEHOLDER_RE.search(flag):
                seen.add(flag)
                out.append(flag)
        return out

    async def check_new_findings(self, agent: str, cursor: int) -> tuple[int, str]:
        path = self.shared_dir / "findings.jsonl"
        if not path.exists():
            return cursor, ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        unread: list[str] = []
        for line in lines[cursor:]:
            try:
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("agent") == agent:
                continue
            kind = record.get("kind", "finding")
            content = record.get("content") or record.get("flag") or ""
            if content:
                unread.append(f"[{record.get('agent', 'unknown')} / {kind}] {content}")
        return len(lines), "\n\n".join(unread)

    def render_shared_context(self, limit: int = 24_000) -> str:
        parts: list[str] = []
        for filename in (
            "hypotheses.md",
            "evidence.md",
            "dead_ends.md",
            "candidate_flags.md",
            "final_flag_evidence.md",
        ):
            path = self.shared_dir / filename
            if path.exists():
                parts.append(f"## {filename}\n{path.read_text(encoding='utf-8', errors='replace')}")
        final = self.final_flag()
        if final:
            parts.append(f"## final_flag.txt\n{final}")
        text = "\n\n".join(parts)
        if len(text) > limit:
            return text[-limit:] + "\n\n[shared memory truncated to most recent content]"
        return text or "Shared memory is empty."

    def _recent_findings(self, max_lines: int) -> str:
        path = self.shared_dir / "findings.jsonl"
        if not path.exists():
            return ""
        lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
        return "\n".join(lines[-max_lines:])
