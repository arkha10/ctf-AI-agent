"""ctf-skills repository discovery and relevance selection."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from backend.local_triage import TriageResult

CATEGORY_TO_SKILL = {
    "web": "ctf-web",
    "pwn": "ctf-pwn",
    "rev": "ctf-reverse",
    "reverse": "ctf-reverse",
    "reversing": "ctf-reverse",
    "crypto": "ctf-crypto",
    "forensics": "ctf-forensics",
    "misc": "ctf-misc",
    "osint": "ctf-osint",
    "malware": "ctf-malware",
    "ai/ml": "ctf-ai-ml",
    "ai": "ctf-ai-ml",
    "ml": "ctf-ai-ml",
}


@dataclass
class Skill:
    name: str
    path: Path
    description: str
    metadata: dict[str, Any]


def default_skills_path(start: Path | None = None) -> Path:
    env = os.environ.get("CTF_SKILLS_PATH")
    if env:
        return Path(env).expanduser().resolve()
    start = (start or Path.cwd()).resolve()
    candidates = [
        start / "../ctf-skills",
        start / "../ctf-skills-main",
        start / "../ctf-skills-main/ctf-skills-main",
        start / "../../ctf-skills-main/ctf-skills-main",
        Path(__file__).resolve().parents[2] / "../ctf-skills-main",
        Path(__file__).resolve().parents[2] / "../ctf-skills-main/ctf-skills-main",
        Path(__file__).resolve().parents[3] / "ctf-skills-main",
        Path(__file__).resolve().parents[3] / "ctf-skills-main/ctf-skills-main",
    ]
    for candidate in candidates:
        if (candidate / "solve-challenge" / "SKILL.md").exists():
            return candidate.resolve()
    return (start / "../ctf-skills").resolve()


class SkillLoader:
    def __init__(self, skills_path: Path) -> None:
        self.skills_path = skills_path.expanduser().resolve()
        self.skills = self._scan()

    def _scan(self) -> dict[str, Skill]:
        skills: dict[str, Skill] = {}
        if not self.skills_path.exists():
            return skills
        for path in sorted(self.skills_path.glob("*/SKILL.md")):
            raw = path.read_text(encoding="utf-8", errors="replace")
            metadata = _frontmatter(raw)
            name = str(metadata.get("name") or path.parent.name)
            description = str(metadata.get("description") or "")
            skills[name] = Skill(name=name, path=path, description=description, metadata=metadata)
        return skills

    def select(self, triage: TriageResult, max_category_skills: int = 3) -> list[Skill]:
        selected: list[str] = []
        for base in ("solve-challenge", "ctf-writeup"):
            if base in self.skills:
                selected.append(base)

        for category in triage.categories:
            skill_name = CATEGORY_TO_SKILL.get(category.lower())
            if skill_name and skill_name not in selected and skill_name in self.skills:
                selected.append(skill_name)

        selected.extend(self._keyword_matches(triage, selected))
        category_count = 0
        final: list[str] = []
        for name in selected:
            if name.startswith("ctf-") and name not in {"ctf-writeup"}:
                category_count += 1
                if category_count > max_category_skills:
                    continue
            if name not in final:
                final.append(name)

        if len(final) == 1 and "ctf-misc" in self.skills:
            final.append("ctf-misc")
        return [self.skills[name] for name in final if name in self.skills]

    def render_for_prompt(self, skills: list[Skill], per_skill_limit: int = 45_000) -> str:
        parts: list[str] = []
        for skill in skills:
            text = skill.path.read_text(encoding="utf-8", errors="replace")
            text = _strip_frontmatter(text).strip()
            if len(text) > per_skill_limit:
                text = text[:per_skill_limit] + "\n\n[skill content truncated; read referenced files from ctf-skills if needed]"
            parts.append(f"# Skill: {skill.name}\nPath: {skill.path}\n\n{text}")
        return "\n\n---\n\n".join(parts)

    def _keyword_matches(self, triage: TriageResult, already: list[str]) -> list[str]:
        corpus = triage.to_prompt(limit=60_000).lower()
        out: list[str] = []
        keyword_to_skill = {
            r"\b(elf|pe32|wasm|apk|bytecode|disassembl|decompil|ghidra|ida)\b": "ctf-reverse",
            r"\b(buffer overflow|rop|ret2|heap|format string|libc|shellcode|seccomp)\b": "ctf-pwn",
            r"\b(rsa|aes|xor|cipher|nonce|prime|modulus|lattice|ecdsa|prng|hash)\b": "ctf-crypto",
            r"\b(pcap|pcapng|png|jpg|jpeg|zip|disk|memory dump|exif|stego|wireshark|tshark)\b": "ctf-forensics",
            r"\b(flask|express|php|cookie|jwt|sql|xss|ssti|ssrf|csrf|http|web)\b": "ctf-web",
            r"\b(osint|geolocation|social media|username|wayback|dns record)\b": "ctf-osint",
            r"\b(malware|c2|beacon|packed|yara|obfuscated script)\b": "ctf-malware",
            r"\b(onnx|safetensors|torch|tensorflow|llm|prompt injection|adversarial)\b": "ctf-ai-ml",
        }
        for pattern, skill in keyword_to_skill.items():
            if skill not in already and skill in self.skills and re.search(pattern, corpus):
                out.append(skill)
        return out


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return {}
    data = yaml.safe_load(text[3:end]) or {}
    return data if isinstance(data, dict) else {}


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    try:
        end = text.index("\n---", 3)
        return text[end + 4 :]
    except ValueError:
        return text
